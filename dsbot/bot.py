"""The Discord client: watches voice channels and serves the recordings."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks, voice_recv

from . import dave
from .config import Config
from .encoder import ffmpeg_available
from .format import file_stamp, human_duration
from .links import Links
from .recorder import ChannelRecorder
from .settings import Settings
from .store import MIX_TRACK, ChunkStore, now_ms
from .ui import recordings
from .web import Exporter, start_web

log = logging.getLogger(__name__)

RECONCILE_SECONDS = 20.0


class RecordingBot(commands.Bot):
    """Keeps at most one recorder per guild (Discord allows one voice connection)."""

    def __init__(self, cfg: Config) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True  # needed to count who sits in a voice channel
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.cfg = cfg
        self.store = ChunkStore(
            cfg.data_dir / "recordings", max_bytes=cfg.max_disk_bytes
        )
        self.settings = Settings(cfg.data_dir / "settings.json")
        self.links = Links(cfg.token, cfg.base_url)
        self.exporter = Exporter(
            self.store, cfg.data_dir / "exports", binary=cfg.ffmpeg, bitrate=cfg.audio_bitrate
        )
        self._web = None
        self.recorders: dict[int, ChannelRecorder] = {}  # guild_id -> recorder
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._eligible_since: dict[int, float] = {}  # channel_id -> monotonic
        self._leave_since: dict[int, float] = {}  # guild_id -> monotonic
        self._leave_tasks: dict[int, asyncio.Task] = {}
        self._sessions: dict[int, int] = {}
        self._left_at: dict[int, int] = {}
        self._announced: dict[int, set[int]] = {}
        self._presence: str | None = None

    # -- setup --------------------------------------------------------------

    async def setup_hook(self) -> None:
        for command in COMMANDS:
            self.tree.add_command(command)
        removed = self.store.cleanup_parts()
        if removed:
            log.info("removed %d stale .part file(s) from a previous run", removed)
        # Exports are throwaway; a crash mid-upload must not leak disk.
        shutil.rmtree(self.cfg.data_dir / "exports", ignore_errors=True)
        self._web = await start_web(self, self.cfg.web_port)
        await self.tree.sync()
        self.reconcile_loop.start()

    async def close(self) -> None:
        self.reconcile_loop.cancel()
        for task in list(self._leave_tasks.values()):
            task.cancel()
        for guild_id in list(self.recorders):
            with contextlib.suppress(Exception):
                await self._teardown(guild_id, reason="shutting down")
        if self._web is not None:
            await self._web.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s guilds)", self.user, len(self.guilds))
        for guild in self.guilds:
            await self.reconcile(guild)

    # -- eligibility --------------------------------------------------------

    def humans_in(self, channel: discord.VoiceChannel | discord.StageChannel) -> list:
        me = channel.guild.me
        return [m for m in channel.members if not m.bot and (me is None or m.id != me.id)]

    def voice_channels(self, guild: discord.Guild) -> list:
        return list(guild.voice_channels) + list(guild.stage_channels)

    def eligible_channels(self, guild: discord.Guild) -> list:
        """Channels holding enough humans, in the order they became eligible.

        First come, first served: the channel that filled up first is the one we
        follow.  Ranking by headcount instead would make a second, busier
        channel steal the connection away from a conversation already underway.
        """
        now = time.monotonic()
        candidates = []
        for channel in self.voice_channels(guild):
            enough = (
                self.cfg.channel_allowed(channel.id)
                and len(self.humans_in(channel)) >= self.cfg.min_speakers
                and bool(self.recorded_in(channel))
            )
            if enough:
                self._eligible_since.setdefault(channel.id, now)
                candidates.append(channel)
            else:
                self._eligible_since.pop(channel.id, None)
        candidates.sort(key=lambda c: (self._eligible_since.get(c.id, now), c.id))
        return candidates

    def occupancy(self, guild: discord.Guild) -> dict[int, int]:
        return {
            channel.id: len(self.humans_in(channel))
            for channel in self.voice_channels(guild)
        }

    def recorded_in(self, channel) -> list:
        opted = set(self.settings.opted_in(channel.guild.id))
        return [m for m in self.humans_in(channel) if m.id in opted]

    def _why_not(self, channel) -> str:
        if not self.recorded_in(channel):
            return "nobody here has opted in"
        return f"fewer than {self.cfg.min_speakers} people"

    # -- reconciliation -----------------------------------------------------

    async def on_voice_state_update(self, member, before, after) -> None:
        if member.guild is None:
            return
        if self.user is not None and member.id == self.user.id and after.channel is None:
            # We were disconnected (moved out, kicked, region change).
            await self._teardown(member.guild.id, reason="disconnected")
        await self.reconcile(member.guild)

    @tasks.loop(seconds=RECONCILE_SECONDS)
    async def reconcile_loop(self) -> None:
        for guild in self.guilds:
            with contextlib.suppress(Exception):
                await self.reconcile(guild)

    @reconcile_loop.before_loop
    async def _before_reconcile(self) -> None:
        await self.wait_until_ready()

    def _lock(self, guild_id: int) -> asyncio.Lock:
        return self._guild_locks.setdefault(guild_id, asyncio.Lock())

    def _grace_remaining(self, guild_id: int) -> float:
        """Seconds of leave grace left, starting the clock on the first call."""
        started = self._leave_since.get(guild_id)
        if started is None:
            self._leave_since[guild_id] = time.monotonic()
            return self.cfg.leave_grace
        return self.cfg.leave_grace - (time.monotonic() - started)

    def _cancel_grace(self, guild_id: int) -> None:
        self._leave_since.pop(guild_id, None)
        task = self._leave_tasks.pop(guild_id, None)
        if task is not None:
            task.cancel()

    def _schedule_recheck(self, guild: discord.Guild, delay: float) -> None:
        """Re-run reconciliation once the grace period has elapsed.

        The periodic loop is far slower than the grace, so without this a
        channel that emptied would keep its connection until the next sweep.
        """
        existing = self._leave_tasks.get(guild.id)
        if existing is not None and not existing.done():
            return

        async def later() -> None:
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(max(0.0, delay))
                self._leave_tasks.pop(guild.id, None)
                with contextlib.suppress(Exception):
                    await self.reconcile(guild)

        self._leave_tasks[guild.id] = asyncio.create_task(
            later(), name=f"leave-grace-{guild.id}"
        )

    async def reconcile(self, guild: discord.Guild) -> None:
        await self._reconcile(guild)
        await self._update_presence()

    def presence_text(self) -> str | None:
        people = 0
        for guild_id, recorder in self.recorders.items():
            guild = self.get_guild(guild_id)
            channel = guild.get_channel(recorder.channel_id) if guild else None
            if channel is not None:
                people += len(self.recorded_in(channel))
        if not self.recorders:
            return None
        return f"🔴 Recording {people} {'person' if people == 1 else 'people'}"

    async def _update_presence(self) -> None:
        text = self.presence_text()
        if text == self._presence:
            return
        self._presence = text
        with contextlib.suppress(Exception):
            await self.change_presence(activity=discord.CustomActivity(name=text) if text else None)

    async def _reconcile(self, guild: discord.Guild) -> None:
        """Make the voice connection match who is actually talking where.

        Stability beats optimality: while the channel we are recording is still
        eligible we stay in it, even if another channel gets busier.  Hopping
        would cut the conversation we are already capturing.
        """
        async with self._lock(guild.id):
            eligible = self.eligible_channels(guild)
            recorder = self.recorders.get(guild.id)
            voice = guild.voice_client

            if recorder is not None:
                current = guild.get_channel(recorder.channel_id)
                connected = voice is not None and voice.is_connected()
                if connected and current is not None and current in eligible:
                    self._cancel_grace(guild.id)
                    await self._announce_newcomers(current)
                    return
                if connected and current is not None:
                    # Hold on briefly: a reconnect, a channel move or someone
                    # reseating a headset should not sever the recording.
                    remaining = self._grace_remaining(guild.id)
                    if remaining > 0:
                        self._schedule_recheck(guild, remaining)
                        return
                await self._teardown(
                    guild.id,
                    reason=self._why_not(current) if connected and current else "connection lost",
                )

            if not eligible:
                if guild.voice_client is not None:
                    await self._teardown(guild.id, reason="nothing to record")
                return

            await self._setup(eligible[0])

    def session_for(self, guild_id: int) -> int:
        now = now_ms()
        current = self._sessions.get(guild_id)
        left = self._left_at.get(guild_id)
        if current is not None and left is not None and now - left < self.cfg.session_gap_ms:
            return current
        if current is None:
            latest = self.store.latest_session(guild_id)
            if latest is not None and now - latest.end_ms < self.cfg.session_gap_ms:
                return latest.session_ms
        return now

    async def _setup(self, channel) -> None:
        guild = channel.guild
        try:
            voice = guild.voice_client
            if voice is None:
                voice = await channel.connect(
                    cls=voice_recv.VoiceRecvClient, self_deaf=False, timeout=30.0
                )
            elif voice.channel != channel:
                await voice.move_to(channel)
        except Exception:
            log.exception("could not join %s", channel)
            return

        session_ms = self.session_for(guild.id)
        self._sessions[guild.id] = session_ms
        self._left_at.pop(guild.id, None)
        recorder = ChannelRecorder(
            self.cfg,
            self.store,
            guild_id=guild.id,
            channel_id=channel.id,
            session_ms=session_ms,
            on_speaker=lambda uid: self.remember_speaker(guild, uid, session_ms),
        )
        self.recorders[guild.id] = recorder
        self._cancel_grace(guild.id)
        await recorder.start()

        try:
            voice.listen(voice_recv.BasicSink(self.packet_handler(guild.id, recorder)))
            dave.install(voice)
        except Exception:
            log.exception("could not start listening in %s", channel)
            await self._teardown(guild.id, reason="listen failed")
            return

        people = self.recorded_in(channel)
        self._announced[guild.id] = {m.id for m in people}
        await self._announce(
            channel,
            f"🔴 Recording in **{channel.name}**: {_mentions(people)}. Nobody else "
            "in here is recorded. `/record-me` to be included, `/stop-recording-me` "
            "to stop, `/recordings` to listen.",
        )

    def packet_handler(self, guild_id: int, recorder: ChannelRecorder):
        def on_packet(user, data) -> None:
            if user is None or getattr(user, "bot", False) or not data.pcm:
                return
            if self.settings.is_opted_in(guild_id, user.id):
                recorder.feed(user.id, data.pcm)

        return on_packet

    async def _announce_newcomers(self, channel) -> None:
        announced = self._announced.setdefault(channel.guild.id, set())
        fresh = [m for m in self.recorded_in(channel) if m.id not in announced]
        if not fresh:
            return
        announced.update(m.id for m in fresh)
        await self._announce(channel, f"🔴 Also recording {_mentions(fresh)}.")

    async def _teardown(self, guild_id: int, *, reason: str) -> None:
        self._cancel_grace(guild_id)
        self._announced.pop(guild_id, None)
        recorder = self.recorders.pop(guild_id, None)
        guild = self.get_guild(guild_id)
        voice = guild.voice_client if guild is not None else None
        if voice is not None:
            with contextlib.suppress(Exception):
                voice.stop_listening()
            with contextlib.suppress(Exception):
                await voice.disconnect(force=True)
        if recorder is not None:
            await recorder.stop()
            self._left_at[guild_id] = now_ms()
            channel = guild.get_channel(recorder.channel_id) if guild else None
            session = self.store.session(guild_id, recorder.session_ms)
            if channel is not None:
                await self._announce(channel, self._stop_message(channel, session, reason))
        log.info("torn down guild %s: %s", guild_id, reason)

    def _stop_message(self, channel, session, reason: str) -> str:
        message = f"⏹️ Stopped recording **{channel.name}** ({reason})."
        if session is None or not session.mix:
            return f"{message} Nothing was said."
        url = self.links.session_url(session.guild_id, session.session_ms, MIX_TRACK)
        return (
            f"{message} This session: {human_duration(session.duration_ms)} of speech, "
            f"[listen](<{url}>). `/recordings` for each voice."
        )

    async def _announce(self, channel, message: str) -> None:
        if not self.cfg.announce:
            return
        target = channel if hasattr(channel, "send") else None
        if target is None:  # pragma: no cover
            target = channel.guild.system_channel
        if target is None:  # pragma: no cover
            return
        with contextlib.suppress(discord.HTTPException, discord.Forbidden, AttributeError):
            await target.send(message, allowed_mentions=discord.AllowedMentions.none())

    # -- speaker names ------------------------------------------------------

    def remember_speaker(
        self, guild: discord.Guild, user_id: int, session_ms: int | None = None
    ) -> None:
        """Snapshot a display name so it survives the member leaving."""
        if session_ms is not None:
            self.store.add_person(guild.id, session_ms, user_id)
        member = guild.get_member(user_id)
        if member is None:
            return
        self.settings.remember_name(user_id, _member_label(member))

    def speaker_label(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return _member_label(member)
        return self.settings.name_for(user_id) or f"user {user_id}"

    def short_name(self, guild: discord.Guild, user_id: int) -> str:
        return self.speaker_label(guild, user_id).split(" (@")[0].lstrip("@")

    def download_name(self, guild_id: int, session_ms: int, track: str) -> str:
        label = "everyone" if track == MIX_TRACK else self.short_name(
            self.get_guild(guild_id), int(track)
        )
        safe = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-") or "voice"
        return f"{file_stamp(session_ms, self.cfg.tz)}_{safe}.m4a"


def _member_label(member) -> str:
    display = getattr(member, "display_name", None) or member.name
    return f"{display} (@{member.name})" if display != member.name else f"@{member.name}"


def _mentions(members) -> str:
    return ", ".join(f"<@{m.id}>" for m in members)


def _voice_channel_id(member) -> int | None:
    state = getattr(member, "voice", None)
    channel = getattr(state, "channel", None)
    return getattr(channel, "id", None)


async def _reply(interaction: discord.Interaction, message: str, *, public: bool) -> None:
    await interaction.response.send_message(
        message, ephemeral=not public, allowed_mentions=discord.AllowedMentions.none()
    )


@app_commands.command(name="record-me", description="Let the bot record your voice")
@app_commands.guild_only()
async def record_me(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client
    guild, user = interaction.guild, interaction.user
    if not bot.settings.set_opted_in(guild.id, user.id, True):
        await _reply(
            interaction, "You're already recorded. `/stop-recording-me` to stop.", public=False
        )
        return
    recorder = bot.recorders.get(guild.id)
    here = recorder is not None and _voice_channel_id(user) == recorder.channel_id
    if here and interaction.channel_id == recorder.channel_id:
        bot._announced.setdefault(guild.id, set()).add(user.id)
    await _reply(
        interaction,
        f"🎙️ {user.mention} opted in: I record their voice whenever they're in a voice "
        "channel with someone else. `/record-me` to join them, `/stop-recording-me` "
        "to stop, `/recordings` to listen." + (" Recording them now." if here else ""),
        public=True,
    )
    await bot.reconcile(guild)


@app_commands.command(name="stop-recording-me", description="Stop the bot recording your voice")
@app_commands.guild_only()
async def stop_recording_me(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client
    guild, user = interaction.guild, interaction.user
    if not bot.settings.set_opted_in(guild.id, user.id, False):
        await _reply(
            interaction, "I'm not recording you. `/record-me` to opt in.", public=False
        )
        return
    bot._announced.get(guild.id, set()).discard(user.id)
    await _reply(
        interaction,
        f"🔕 {user.mention} opted out: I no longer record their voice. What was "
        "already recorded stays until it rotates out.",
        public=True,
    )
    await bot.reconcile(guild)


COMMANDS = [record_me, stop_recording_me, recordings]


def build_bot(cfg: Config) -> RecordingBot:
    if not ffmpeg_available(cfg.ffmpeg):
        raise RuntimeError(
            f"ffmpeg binary {cfg.ffmpeg!r} not found; install ffmpeg or set FFMPEG"
        )
    (cfg.data_dir / "recordings").mkdir(parents=True, exist_ok=True)
    return RecordingBot(cfg)

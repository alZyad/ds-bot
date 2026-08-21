"""The Discord client: watches voice channels and serves the recordings."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import time
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks, voice_recv

from .config import Config
from .encoder import export, ffmpeg_available, split_for_upload
from .format import discord_time, human_duration, human_size, slug
from .recorder import ChannelRecorder
from .settings import Settings
from .store import MIX_TRACK, ChunkStore, Segment, group_segments, merge_segments, now_ms

log = logging.getLogger(__name__)

MAX_ATTACHMENTS_PER_MESSAGE = 10
UPLOAD_HEADROOM = 256 * 1024  # leave room for multipart overhead
RECONCILE_SECONDS = 20.0
CONFIRM_ABOVE_FILES = 10
SELECT_LIMIT = 25  # Discord's cap on dropdown options


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
        self.recorders: dict[int, ChannelRecorder] = {}  # guild_id -> recorder
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._export_locks: dict[int, asyncio.Lock] = {}
        self._eligible_since: dict[int, float] = {}  # channel_id -> monotonic
        self._leave_since: dict[int, float] = {}  # guild_id -> monotonic
        self._leave_tasks: dict[int, asyncio.Task] = {}

    # -- setup --------------------------------------------------------------

    async def setup_hook(self) -> None:
        if not self.cfg.speaker_tracks_enabled:
            # Nothing writes isolated tracks, so the commands that serve them
            # could only ever answer "none found". Do not publish them at all.
            for name in ("get-all", "get-single"):
                rec_group.remove_command(name)
        self.tree.add_command(rec_group)
        removed = self.store.cleanup_parts()
        if removed:
            log.info("removed %d stale .part file(s) from a previous run", removed)
        # Exports are throwaway; a crash mid-upload must not leak disk.
        shutil.rmtree(self.cfg.data_dir / "exports", ignore_errors=True)
        await self.tree.sync()
        self.reconcile_loop.start()

    async def close(self) -> None:
        self.reconcile_loop.cancel()
        for task in list(self._leave_tasks.values()):
            task.cancel()
        for guild_id in list(self.recorders):
            with contextlib.suppress(Exception):
                await self._teardown(guild_id, reason="shutting down")
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

    def autojoin_enabled(self, guild_id: int) -> bool:
        return self.settings.autojoin(guild_id, self.cfg.autojoin_default)

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
        """Make the voice connection match who is actually talking where.

        Stability beats optimality: while the channel we are recording is still
        eligible we stay in it, even if another channel gets busier.  Hopping
        would cut the conversation we are already capturing.
        """
        async with self._lock(guild.id):
            if not self.autojoin_enabled(guild.id):
                if self.recorders.get(guild.id) is not None or guild.voice_client is not None:
                    await self._teardown(guild.id, reason="auto-join disabled")
                return

            eligible = self.eligible_channels(guild)
            recorder = self.recorders.get(guild.id)
            voice = guild.voice_client

            if recorder is not None:
                current = guild.get_channel(recorder.channel_id)
                connected = voice is not None and voice.is_connected()
                if connected and current is not None and current in eligible:
                    self._cancel_grace(guild.id)
                    return  # already doing the right thing
                if connected and current is not None:
                    # Hold on briefly: a reconnect, a channel move or someone
                    # reseating a headset should not sever the recording.
                    remaining = self._grace_remaining(guild.id)
                    if remaining > 0:
                        self._schedule_recheck(guild, remaining)
                        return
                await self._teardown(
                    guild.id,
                    reason="channel no longer eligible" if connected else "connection lost",
                )

            if not eligible:
                if guild.voice_client is not None:
                    await self._teardown(guild.id, reason="no eligible channel")
                return

            await self._setup(eligible[0])

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

        recorder = ChannelRecorder(
            self.cfg,
            self.store,
            guild_id=guild.id,
            channel_id=channel.id,
            silence_rms=self.settings.silence_rms(channel.id, self.cfg.silence_rms),
            on_speaker=lambda uid: self.remember_speaker(guild, uid),
        )
        self.recorders[guild.id] = recorder
        self._cancel_grace(guild.id)
        await recorder.start()

        def on_packet(user, data) -> None:
            if user is None or getattr(user, "bot", False) or not data.pcm:
                return
            recorder.feed(user.id, data.pcm)

        try:
            voice.listen(voice_recv.BasicSink(on_packet))
        except Exception:
            log.exception("could not start listening in %s", channel)
            await self._teardown(guild.id, reason="listen failed")
            return

        await self._announce(
            channel,
            f"🔴 Recording **{channel.name}** — {len(self.humans_in(channel))} people "
            f"connected. Buffer: up to {human_size(self.cfg.max_disk_bytes)} of audio. "
            f"Use `/rec get` to pull it.",
        )

    async def _teardown(self, guild_id: int, *, reason: str) -> None:
        self._cancel_grace(guild_id)
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
            channel = guild.get_channel(recorder.channel_id) if guild else None
            if channel is not None:
                await self._announce(
                    channel,
                    f"⏹️ Stopped recording **{channel.name}** ({reason}). "
                    f"{human_duration(self.store.total_duration_ms(guild_id, channel.id))}"
                    f" of audio is buffered.",
                )
        log.info("torn down guild %s: %s", guild_id, reason)

    async def _announce(self, channel, message: str) -> None:
        if not self.cfg.announce:
            return
        target = channel if hasattr(channel, "send") else None
        if target is None:  # pragma: no cover
            target = channel.guild.system_channel
        if target is None:  # pragma: no cover
            return
        with contextlib.suppress(discord.HTTPException, discord.Forbidden, AttributeError):
            await target.send(message)

    # -- speaker names ------------------------------------------------------

    def remember_speaker(self, guild: discord.Guild, user_id: int) -> None:
        """Snapshot a display name so it survives the member leaving."""
        member = guild.get_member(user_id)
        if member is None:
            return
        self.settings.remember_name(user_id, _member_label(member))

    def speaker_label(self, guild: discord.Guild, user_id: int) -> str:
        member = guild.get_member(user_id) if guild is not None else None
        if member is not None:
            return _member_label(member)
        return self.settings.name_for(user_id) or f"user {user_id}"

    # -- export -------------------------------------------------------------

    def upload_limit(self, guild: discord.Guild) -> int:
        configured = int(self.cfg.max_upload_mb * 1024 * 1024)
        guild_limit = max(1024 * 1024, guild.filesize_limit - UPLOAD_HEADROOM)
        return max(256 * 1024, min(configured, guild_limit))

    def export_lock(self, channel_id: int) -> asyncio.Lock:
        return self._export_locks.setdefault(channel_id, asyncio.Lock())

    def segments_for(
        self, guild_id: int, channel_id: int, *, track: str, since_ms: int | None
    ) -> list[Segment]:
        """The conversations of one track, merged into what will become files."""
        chunks = self.store.list_chunks(
            guild_id, channel_id, track=track, since_ms=since_ms
        )
        return merge_segments(group_segments(chunks), self.cfg.merge_gap_ms)


def _member_label(member) -> str:
    display = getattr(member, "display_name", None) or member.name
    return f"{display} (@{member.name})" if display != member.name else f"@{member.name}"


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------

rec_group = app_commands.Group(
    name="rec",
    description="Voice channel recording buffer",
    guild_only=True,
)


def _safe_name(name: str) -> str:
    """A filename-safe version of a name, for the exported attachments."""
    cleaned = re.sub(r"-{2,}", "-", "".join(
        c if c.isalnum() or c in "-_" else "-" for c in name
    )).strip("-_")
    return cleaned[:48] or "voice"


def _caption(part) -> str:
    size = f"{human_duration(part.duration_ms)}, {human_size(part.size_bytes)}"
    return size if part.total == 1 else f"part {part.index}/{part.total}: {size}"


def _resolve_channel(bot: RecordingBot, interaction, explicit):
    if explicit is not None:
        return explicit
    voice_state = getattr(interaction.user, "voice", None)
    if voice_state is not None and voice_state.channel is not None:
        return voice_state.channel
    recorder = bot.recorders.get(interaction.guild_id)
    if recorder is not None:
        channel = interaction.guild.get_channel(recorder.channel_id)
        if channel is not None:
            return channel
    # fall back to whichever channel holds the most buffered audio
    best, best_ms = None, 0
    for channel in interaction.guild.voice_channels:
        buffered = bot.store.total_duration_ms(interaction.guild_id, channel.id)
        if buffered > best_ms:
            best, best_ms = channel, buffered
    return best


def _since(bot: RecordingBot, minutes: float | None) -> int | None:
    return None if minutes is None else now_ms() - int(minutes * 60_000)


def _count_files(bot: RecordingBot, jobs, max_bytes: int) -> int:
    return sum(
        len(split_for_upload(segment.chunks, max_bytes))
        for _, segments in jobs
        for segment in segments
    )


async def _flush_live(bot: RecordingBot, guild_id: int, channel_id: int) -> None:
    recorder = bot.recorders.get(guild_id)
    if recorder is not None and recorder.channel_id == channel_id:
        await recorder.flush()  # make the in-progress chunk visible


async def _deliver(interaction, bot: RecordingBot, target, jobs, header: str) -> None:
    """Encode every (label, segments) job and upload the results."""
    workdir = bot.cfg.data_dir / "exports" / uuid.uuid4().hex
    base = _safe_name(target.name)
    try:
        parts = []
        for label, segments in jobs:
            prefix = f"{base}-{slug(segments[0].start_ms)}"
            if label:
                prefix = f"{prefix}-{_safe_name(label)}"
            parts.extend(
                await export(
                    segments,
                    workdir,
                    prefix=prefix,
                    max_bytes=bot.upload_limit(interaction.guild),
                    binary=bot.cfg.ffmpeg,
                    bitrate=bot.cfg.audio_bitrate,
                )
            )
    except Exception as exc:
        log.exception("export failed for channel %s", target.id)
        shutil.rmtree(workdir, ignore_errors=True)
        await interaction.followup.send(f"Export failed: `{exc}`")
        return

    try:
        if not parts:
            await interaction.followup.send("Nothing to send.")
            return
        first = True
        for offset in range(0, len(parts), MAX_ATTACHMENTS_PER_MESSAGE):
            batch = parts[offset : offset + MAX_ATTACHMENTS_PER_MESSAGE]
            files = [discord.File(p.path, filename=p.path.name) for p in batch]
            caption = " · ".join(_caption(p) for p in batch)
            if first:
                await interaction.followup.send(f"{header}\n{caption}", files=files)
                first = False
            elif interaction.channel is not None:
                await interaction.channel.send(content=caption, files=files)
            else:  # pragma: no cover - the command is guild-only
                await interaction.followup.send(caption, files=files)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


class _Confirm(discord.ui.View):
    """Guard against dumping dozens of attachments without being asked."""

    def __init__(self, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.requester_id = requester_id
        self.confirmed = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only whoever ran the command can confirm it.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Send them all", style=discord.ButtonStyle.primary)
    async def send(self, interaction: discord.Interaction, _button) -> None:
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()


class _SpeakerSelect(discord.ui.Select):
    def __init__(self, bot: RecordingBot, guild, target, speakers, minutes) -> None:
        options = []
        for user_id in speakers[:SELECT_LIMIT]:
            segments = bot.segments_for(
                guild.id, target.id, track=str(user_id), since_ms=_since(bot, minutes)
            )
            total = sum(s.duration_ms for s in segments)
            options.append(
                discord.SelectOption(
                    label=bot.speaker_label(guild, user_id)[:100],
                    value=str(user_id),
                    description=f"{human_duration(total)} of speech",
                )
            )
        super().__init__(placeholder="Whose voice do you want?", options=options)
        self.bot = bot
        self.target = target
        self.minutes = minutes

    async def callback(self, interaction: discord.Interaction) -> None:
        user_id = int(self.values[0])
        bot, target = self.bot, self.target
        await interaction.response.defer(thinking=True)
        label = bot.speaker_label(interaction.guild, user_id)
        async with bot.export_lock(target.id):
            await _flush_live(bot, interaction.guild_id, target.id)
            segments = bot.segments_for(
                interaction.guild_id, target.id, track=str(user_id),
                since_ms=_since(bot, self.minutes),
            )
            if not segments:
                await interaction.followup.send(f"Nothing buffered for {label}.")
                return
            header = (
                f"🎙️ **{label}** in **{target.name}** — "
                f"{human_duration(sum(s.duration_ms for s in segments))} across "
                f"{len(segments)} file(s)."
            )
            await _deliver(interaction, bot, target, [(label, segments)], header)


class _SpeakerView(discord.ui.View):
    def __init__(self, bot, guild, target, speakers, minutes, requester_id) -> None:
        super().__init__(timeout=180)
        self.requester_id = requester_id
        self.add_item(_SpeakerSelect(bot, guild, target, speakers, minutes))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only whoever ran the command can pick.", ephemeral=True
            )
            return False
        return True


@rec_group.command(name="get", description="Send the mixed recording of a voice channel")
@app_commands.describe(
    channel="Voice channel to export (default: the one you are in)",
    minutes="How far back to go, in minutes (default: the whole buffer)",
)
async def rec_get(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
    minutes: Optional[app_commands.Range[float, 0.1, 100_000.0]] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send(
            "I have no recording for this server yet. Join a voice channel with "
            f"{bot.cfg.min_speakers}+ people and I will start buffering."
        )
        return

    async with bot.export_lock(target.id):
        await _flush_live(bot, interaction.guild_id, target.id)
        segments = bot.segments_for(
            interaction.guild_id, target.id, track=MIX_TRACK,
            since_ms=_since(bot, minutes),
        )
        if not segments:
            await interaction.followup.send(f"Nothing buffered for {target.mention}.")
            return
        total = sum(s.duration_ms for s in segments)
        header = (
            f"🎙️ **{target.name}** — {human_duration(total)} of speech in "
            f"{len(segments)} conversation(s), from "
            f"{discord_time(segments[0].start_ms)} to "
            f"{discord_time(segments[-1].end_ms)}."
        )
        await _deliver(interaction, bot, target, [("", segments)], header)


@rec_group.command(
    name="get-all",
    description="Send the mixed recording plus every speaker's isolated track",
)
@app_commands.describe(
    channel="Voice channel to export (default: the one you are in)",
    minutes="How far back to go, in minutes (default: the whole buffer)",
)
async def rec_get_all(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
    minutes: Optional[app_commands.Range[float, 0.1, 100_000.0]] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send("No recordings for this server yet.")
        return

    async with bot.export_lock(target.id):
        await _flush_live(bot, interaction.guild_id, target.id)
        since_ms = _since(bot, minutes)
        jobs: list[tuple[str, list[Segment]]] = []
        mixed = bot.segments_for(
            interaction.guild_id, target.id, track=MIX_TRACK, since_ms=since_ms
        )
        if mixed:
            jobs.append(("mixed", mixed))
        for user_id in bot.store.speakers(interaction.guild_id, target.id):
            segments = bot.segments_for(
                interaction.guild_id, target.id, track=str(user_id), since_ms=since_ms
            )
            if segments:
                jobs.append((bot.speaker_label(interaction.guild, user_id), segments))

        if not jobs:
            await interaction.followup.send(f"Nothing buffered for {target.mention}.")
            return

        limit = bot.upload_limit(interaction.guild)
        count = _count_files(bot, jobs, limit)
        header = (
            f"🎚️ **{target.name}** — mixed plus {len(jobs) - 1} speaker track(s), "
            f"{count} file(s)."
        )
        if count > CONFIRM_ABOVE_FILES:
            view = _Confirm(interaction.user.id)
            await interaction.followup.send(
                f"That is **{count} files** across {len(jobs)} track(s). "
                "Send them all, or narrow it with the `minutes:` option?",
                view=view,
            )
            await view.wait()
            if not view.confirmed:
                return
        await _deliver(interaction, bot, target, jobs, header)


@rec_group.command(
    name="get-single", description="Pick one person and get only their voice"
)
@app_commands.describe(
    channel="Voice channel to export (default: the one you are in)",
    minutes="How far back to go, in minutes (default: the whole buffer)",
)
async def rec_get_single(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
    minutes: Optional[app_commands.Range[float, 0.1, 100_000.0]] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send("No recordings for this server yet.", ephemeral=True)
        return

    await _flush_live(bot, interaction.guild_id, target.id)
    speakers = bot.store.speakers(interaction.guild_id, target.id)
    if not speakers:
        await interaction.followup.send(
            f"No isolated tracks for {target.mention}. They are only written while "
            f"at most {bot.cfg.max_speaker_tracks} different people speak in a "
            "conversation.",
            ephemeral=True,
        )
        return

    view = _SpeakerView(
        bot, interaction.guild, target, speakers, minutes, interaction.user.id
    )
    extra = "" if len(speakers) <= SELECT_LIMIT else f" (showing {SELECT_LIMIT})"
    await interaction.followup.send(
        f"**{target.name}** has {len(speakers)} isolated track(s){extra}:",
        view=view, ephemeral=True,
    )


@rec_group.command(name="status", description="Show what is being recorded and how much is buffered")
async def rec_status(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    recorder = bot.recorders.get(interaction.guild_id)
    used = bot.store.total_bytes()
    header = (
        f"**Buffer:** {human_size(used)} of {human_size(bot.cfg.max_disk_bytes)} used"
        f" ({100 * used / max(1, bot.cfg.max_disk_bytes):.0f}%)"
        f" · chunks of {human_duration(bot.cfg.chunk_seconds * 1000)}"
        f" · oldest chunk goes first"
    )
    lines = [header]

    if recorder is None:
        if bot.autojoin_enabled(interaction.guild_id):
            lines.append(
                f"\n**Live:** idle — waiting for a channel with "
                f"{bot.cfg.min_speakers}+ people."
            )
        else:
            lines.append(
                "\n**Live:** off — auto-join is disabled. Turn it on with "
                "`/rec autojoin enabled:true`."
            )
    else:
        channel = interaction.guild.get_channel(recorder.channel_id)
        info = recorder.describe()
        lines.append(
            f"\n**Live:** {channel.mention if channel else recorder.channel_id} · "
            f"{'🗣️ capturing' if info['in_segment'] else '🤫 silent'} · "
            f"{info['segments']} conversation(s) this session · "
            f"{human_duration(info['recorded_seconds'] * 1000)} captured"
        )
        lines.append(
            f"**Gate:** speech above RMS {info['silence_rms']}"
            f" · right now {info['last_rms']}"
            f" · isolated tracks {'on' if info['tracks'] else 'off (too many speakers)'}"
        )

    buffered = []
    for vc in interaction.guild.voice_channels:
        total = bot.store.total_duration_ms(interaction.guild_id, vc.id)
        if total:
            buffered.append(
                (total, vc, bot.store.total_bytes(interaction.guild_id, vc.id),
                 len(bot.store.speakers(interaction.guild_id, vc.id)))
            )
    if buffered:
        lines.append("\n**Buffered:**")
        for total, vc, size, tracks in sorted(buffered, reverse=True, key=lambda i: i[0]):
            lines.append(
                f"· {vc.mention} — {human_duration(total)} "
                f"({human_size(size)}, {tracks} speaker track(s))"
            )
    else:
        lines.append("\n**Buffered:** nothing yet.")

    occupancy = {cid: n for cid, n in bot.occupancy(interaction.guild).items() if n}
    if occupancy:
        lines.append("\n**Occupancy:**")
        for cid, people in sorted(occupancy.items(), key=lambda i: -i[1]):
            channel = interaction.guild.get_channel(cid)
            mark = "✅" if people >= bot.cfg.min_speakers else "—"
            lines.append(f"· {mark} {channel.mention if channel else cid}: {people}")

    await interaction.followup.send("\n".join(lines), ephemeral=True)


@rec_group.command(
    name="autojoin", description="Turn automatic recording of busy channels on or off"
)
@app_commands.describe(
    enabled="On: I join busy channels and record. Off: I never join on my own.",
)
@app_commands.checks.has_permissions(manage_guild=True)
async def rec_autojoin(
    interaction: discord.Interaction,
    enabled: Optional[bool] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    if enabled is None:
        on = bot.autojoin_enabled(interaction.guild_id)
        detail = (
            f" I join any channel with {bot.cfg.min_speakers}+ people and record it."
            if on
            else " I stay out of every channel until you turn it on."
        )
        await interaction.followup.send(
            f"Auto-join is **{'on' if on else 'off'}** for this server.{detail}",
            ephemeral=True,
        )
        return

    bot.settings.set_autojoin(interaction.guild_id, bool(enabled))
    await bot.reconcile(interaction.guild)

    if enabled:
        await interaction.followup.send(
            f"✅ Auto-join **on**. I will record any channel with "
            f"{bot.cfg.min_speakers}+ people. Pull audio with `/rec get`, "
            "see what is buffered with `/rec status`.",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(
            "🛑 Auto-join **off**. I left any channel I was in and will not join "
            "again until you run `/rec autojoin enabled:true`. Buffered audio is "
            "kept — `/rec purge` deletes it.",
            ephemeral=True,
        )


@rec_autojoin.error
async def _autojoin_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to change auto-join."
    else:  # pragma: no cover
        log.exception("command error", exc_info=error)
        message = f"Something went wrong: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@rec_group.command(name="help", description="What the recorder does and every command")
async def rec_help(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    on = bot.autojoin_enabled(interaction.guild_id)
    lines = [
        "**ds-bot — voice recording buffer**",
        (
            f"Auto-join is **{'on' if on else 'off'}**. When on, I join any voice "
            f"channel with **{bot.cfg.min_speakers}+** people, record it to a rolling "
            f"buffer (up to {human_size(bot.cfg.max_disk_bytes)}), and stop after a "
            "spell of silence. When off, I never join on my own."
        ),
        (
            f"Turn it {'off' if on else 'on'} with "
            f"`/rec autojoin enabled:{'false' if on else 'true'}` (needs Manage Server)."
        ),
        "",
        "**Commands**",
    ]
    for command in sorted(rec_group.walk_commands(), key=lambda c: c.name):
        lines.append(f"· `/rec {command.name}` — {command.description}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@rec_group.command(name="list", description="List the conversations currently in the buffer")
@app_commands.describe(channel="Voice channel to inspect (default: the one you are in)")
async def rec_list(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send("No recordings for this server yet.", ephemeral=True)
        return

    segments = bot.segments_for(
        interaction.guild_id, target.id, track=MIX_TRACK, since_ms=None
    )
    if not segments:
        await interaction.followup.send(
            f"Nothing buffered for {target.mention}.", ephemeral=True
        )
        return

    header = (
        f"**{target.name}** — {len(segments)} conversation(s), "
        f"{human_duration(sum(s.duration_ms for s in segments))} total. "
        f"`/rec get` sends one file each."
    )
    lines = [header]
    for segment in segments[-25:]:
        lines.append(
            f"· {discord_time(segment.start_ms, 't')} → "
            f"{discord_time(segment.end_ms, 't')} "
            f"({human_duration(segment.duration_ms)}, {human_size(segment.size_bytes)})"
        )
    if len(segments) > 25:
        lines.insert(1, f"_showing the {min(25, len(segments))} most recent_")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


@rec_group.command(name="config", description="Read or set the silence gate of a channel")
@app_commands.describe(
    channel="Voice channel to configure (default: the one you are in)",
    silence_rms="Loudness below which audio counts as silence (0-32767)",
    reset="Go back to the server-wide default",
)
async def rec_config(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
    silence_rms: Optional[app_commands.Range[int, 0, 32767]] = None,
    reset: Optional[bool] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send(
            "Tell me which channel: `/rec config channel:<name>`.", ephemeral=True
        )
        return

    if reset:
        bot.settings.set_silence_rms(target.id, None)
    elif silence_rms is not None:
        bot.settings.set_silence_rms(target.id, int(silence_rms))

    effective = bot.settings.silence_rms(target.id, bot.cfg.silence_rms)
    recorder = bot.recorders.get(interaction.guild_id)
    live = recorder if recorder is not None and recorder.channel_id == target.id else None
    if live is not None:
        live.silence_rms = effective

    override = bot.settings.channel_option(target.id, "silence_rms")
    now = f" · **right now {live.last_rms}**" if live is not None else ""
    await interaction.followup.send(
        f"**{target.name}** silence gate: **{effective}**"
        f" ({'channel override' if override is not None else 'server default'})"
        f"{now}\n"
        "Audio quieter than this counts as silence, and "
        f"{human_duration(bot.cfg.silence_timeout * 1000)} of it ends a recording.\n"
        "· `0` — record everything, room noise included\n"
        f"· `{bot.cfg.silence_rms}` — the default\n"
        "· `400` — only clear speech; quiet talkers get cut\n"
        "Open mics in a noisy room want a higher number. Watch **right now** "
        "while nobody speaks and set the gate just above it.",
        ephemeral=True,
    )


@rec_group.command(name="purge", description="Delete the buffered recording of a voice channel")
@app_commands.describe(channel="Voice channel to purge (default: the one you are in)")
@app_commands.checks.has_permissions(manage_guild=True)
async def rec_purge(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True)

    target = _resolve_channel(bot, interaction, channel)
    if target is None:
        await interaction.followup.send("Nothing to purge.")
        return

    async with bot.export_lock(target.id):
        removed = bot.store.purge(interaction.guild_id, target.id)
    await interaction.followup.send(
        f"🗑️ Deleted {removed} file(s) for {target.mention}."
        + (" Recording continues." if bot.recorders.get(interaction.guild_id) else "")
    )


@rec_purge.error
async def _purge_error(interaction: discord.Interaction, error: Exception) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "You need the **Manage Server** permission to purge recordings."
    else:  # pragma: no cover
        log.exception("command error", exc_info=error)
        message = f"Something went wrong: `{error}`"
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


def build_bot(cfg: Config) -> RecordingBot:
    if not ffmpeg_available(cfg.ffmpeg):
        raise RuntimeError(
            f"ffmpeg binary {cfg.ffmpeg!r} not found; install ffmpeg or set FFMPEG"
        )
    (cfg.data_dir / "recordings").mkdir(parents=True, exist_ok=True)
    return RecordingBot(cfg)

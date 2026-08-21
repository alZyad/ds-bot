"""The Discord client: watches voice channels and serves the recordings."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import shutil
import uuid
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks, voice_recv

from .config import Config
from .encoder import export, ffmpeg_available
from .format import discord_time, human_duration, human_size, slug
from .recorder import ChannelRecorder
from .store import ChunkStore, now_ms

log = logging.getLogger(__name__)

MAX_ATTACHMENTS_PER_MESSAGE = 10
UPLOAD_HEADROOM = 256 * 1024  # leave room for multipart overhead
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
            cfg.data_dir / "recordings",
            retention_ms=cfg.retention_ms,
            strategy=cfg.retention_strategy,
            high_water_ms=int(cfg.high_water_slack * 1000),
            low_water_ms=int(cfg.low_water_slack * 1000),
        )
        self.recorders: dict[int, ChannelRecorder] = {}  # guild_id -> recorder
        self._guild_locks: dict[int, asyncio.Lock] = {}
        self._export_locks: dict[int, asyncio.Lock] = {}

    # -- setup --------------------------------------------------------------

    async def setup_hook(self) -> None:
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

    def eligible_channels(self, guild: discord.Guild) -> list:
        """Voice channels that currently hold enough humans, busiest first."""
        candidates = []
        for channel in list(guild.voice_channels) + list(guild.stage_channels):
            if not self.cfg.channel_allowed(channel.id):
                continue
            people = len(self.humans_in(channel))
            if people >= self.cfg.min_speakers:
                candidates.append((people, channel))
        candidates.sort(key=lambda item: (-item[0], item[1].id))
        return [channel for _, channel in candidates]

    def occupancy(self, guild: discord.Guild) -> dict[int, int]:
        return {
            channel.id: len(self.humans_in(channel))
            for channel in list(guild.voice_channels) + list(guild.stage_channels)
        }

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

    async def reconcile(self, guild: discord.Guild) -> None:
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
                    return  # already doing the right thing
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
            self.cfg, self.store, guild_id=guild.id, channel_id=channel.id
        )
        self.recorders[guild.id] = recorder
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
            f"connected. Rolling buffer: {human_duration(self.cfg.retention_ms)}. "
            f"Use `/rec get` to pull the audio.",
        )

    async def _teardown(self, guild_id: int, *, reason: str) -> None:
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

    # -- export -------------------------------------------------------------

    def upload_limit(self, guild: discord.Guild) -> int:
        configured = int(self.cfg.max_upload_mb * 1024 * 1024)
        guild_limit = max(1024 * 1024, guild.filesize_limit - UPLOAD_HEADROOM)
        return max(256 * 1024, min(configured, guild_limit))

    def export_lock(self, channel_id: int) -> asyncio.Lock:
        return self._export_locks.setdefault(channel_id, asyncio.Lock())


# ---------------------------------------------------------------------------
# slash commands
# ---------------------------------------------------------------------------

rec_group = app_commands.Group(
    name="rec",
    description="Voice channel recording buffer",
    guild_only=True,
)


def _safe_name(name: str) -> str:
    """A filename-safe version of a channel name, for the exported attachments."""
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


@rec_group.command(name="get", description="Send the buffered recording of a voice channel as mp3")
@app_commands.describe(
    channel="Voice channel to export (default: the one you are in)",
    minutes="How far back to go, in minutes (default: the whole buffer)",
)
async def rec_get(
    interaction: discord.Interaction,
    channel: Optional[discord.VoiceChannel] = None,
    minutes: Optional[app_commands.Range[float, 0.1, 600.0]] = None,
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

    window_ms = int((minutes or bot.cfg.retention_seconds / 60) * 60_000)
    since_ms = now_ms() - window_ms

    async with bot.export_lock(target.id):
        recorder = bot.recorders.get(interaction.guild_id)
        if recorder is not None and recorder.channel_id == target.id:
            await recorder.flush()  # make the in-progress chunk visible

        chunks = bot.store.list_chunks(
            interaction.guild_id, target.id, since_ms=since_ms
        )
        if not chunks:
            await interaction.followup.send(
                f"Nothing buffered for {target.mention} in the last "
                f"{human_duration(window_ms)}."
            )
            return

        workdir = bot.cfg.data_dir / "exports" / uuid.uuid4().hex
        try:
            parts = await export(
                chunks,
                workdir,
                prefix=f"{_safe_name(target.name)}-{slug(chunks[0].start_ms)}",
                max_bytes=bot.upload_limit(interaction.guild),
                binary=bot.cfg.ffmpeg,
                bitrate=bot.cfg.mp3_bitrate,
            )
        except Exception as exc:
            log.exception("export failed for channel %s", target.id)
            await interaction.followup.send(f"Export failed: `{exc}`")
            shutil.rmtree(workdir, ignore_errors=True)
            return

        try:
            total_ms = sum(c.duration_ms for c in chunks)
            segments = len({c.segment_ms for c in chunks})
            header = (
                f"🎙️ **{target.name}** — {human_duration(total_ms)} of speech in "
                f"{segments} conversation(s), from {discord_time(chunks[0].start_ms)} "
                f"to {discord_time(chunks[-1].end_ms)}."
            )
            if len(parts) > 1:
                header += f"\nSplit into {len(parts)} files to fit Discord's upload limit."
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


@rec_group.command(name="status", description="Show what is being recorded and how much is buffered")
async def rec_status(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client  # type: ignore[assignment]
    await interaction.response.defer(thinking=True, ephemeral=True)

    recorder = bot.recorders.get(interaction.guild_id)
    header = (
        f"**Rolling buffer:** {human_duration(bot.cfg.retention_ms)} of speech per channel"
        f" · strategy `{bot.cfg.retention_strategy}`"
        f" · chunks of {human_duration(bot.cfg.chunk_seconds * 1000)}"
    )
    lines = [header]

    if recorder is None:
        lines.append(
            f"\n**Live:** idle — waiting for a channel with "
            f"{bot.cfg.min_speakers}+ people."
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

    buffered = []
    for vc in interaction.guild.voice_channels:
        total = bot.store.total_duration_ms(interaction.guild_id, vc.id)
        if total:
            size = sum(
                c.size_bytes
                for c in bot.store.list_chunks(interaction.guild_id, vc.id)
            )
            buffered.append((total, vc, size))
    if buffered:
        lines.append("\n**Buffered:**")
        for total, vc, size in sorted(buffered, reverse=True, key=lambda i: i[0]):
            pct = 100 * total / max(1, bot.cfg.retention_ms)
            lines.append(
                f"· {vc.mention} — {human_duration(total)} "
                f"({pct:.0f}% of target, {human_size(size)})"
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

    segments = bot.store.segments(interaction.guild_id, target.id)
    if not segments:
        await interaction.followup.send(
            f"Nothing buffered for {target.mention}.", ephemeral=True
        )
        return

    header = (
        f"**{target.name}** — {len(segments)} conversation(s), "
        f"{human_duration(sum(s.duration_ms for s in segments))} total:"
    )
    lines = [header]
    for segment in segments[-25:]:
        lines.append(
            f"· {discord_time(segment.start_ms, 't')} → "
            f"{discord_time(segment.end_ms, 't')} "
            f"({human_duration(segment.duration_ms)}, {len(segment.chunks)} chunk(s))"
        )
    if len(segments) > 25:
        lines.insert(1, f"_showing the {min(25, len(segments))} most recent_")
    await interaction.followup.send("\n".join(lines), ephemeral=True)


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

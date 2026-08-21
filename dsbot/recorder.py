"""The per-channel recording state machine.

    voice packets ──▶ Mixer ──▶ pump (one frame every 20 ms) ──▶ segments
                                                                   │
                                                       chunks ─────┘──▶ ChunkStore

Two ideas do all the work:

*Segments* are stretches of actual conversation.  A segment opens on the first
voiced frame and closes after ``silence_timeout`` of silence.  The silence that
ends a segment is buffered, not written: if somebody speaks again before the
timeout the buffered frames are flushed (so a natural pause is preserved), and
if nobody does they are dropped (so the recording holds no dead air).

*Chunks* are fixed-size slices of a segment (``chunk_seconds`` of audio) written
as individual mp3 files.  They are the unit of retention: deleting one chunk
costs one ``unlink`` and one minute of the oldest audio.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from .audio import Mixer
from .config import Config
from .encoder import Mp3Encoder
from .store import Chunk, ChunkStore, now_ms

log = logging.getLogger(__name__)


@dataclass
class RecorderStats:
    started_at: float = field(default_factory=time.time)
    frames_seen: int = 0
    frames_written: int = 0
    segments: int = 0
    chunks: int = 0
    trimmed_chunks: int = 0
    late_resyncs: int = 0

    @property
    def recorded_seconds(self) -> float:
        return self.frames_written * 0.02


class ChannelRecorder:
    """Records one voice channel for as long as it is eligible."""

    def __init__(
        self,
        cfg: Config,
        store: ChunkStore,
        *,
        guild_id: int,
        channel_id: int,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.mixer = Mixer(frame_ms=cfg.frame_ms, sample_rate=cfg.sample_rate)
        self.stats = RecorderStats()

        self._frame_seconds = cfg.frame_ms / 1000
        self._task: asyncio.Task | None = None
        self._running = False

        # current segment / chunk
        self._segment_ms: int | None = None
        self._encoder: Mp3Encoder | None = None
        self._chunk_part: Path | None = None
        self._chunk_start_ms = 0
        self._chunk_frames = 0
        self._pending_silence: list[bytes] = []

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def recording(self) -> bool:
        """True while a segment is open, i.e. somebody is actually talking."""
        return self._segment_ms is not None

    def feed(self, user_id: int, pcm: bytes) -> None:
        """Called from the voice receive thread for every decoded packet."""
        if self._running:
            self.mixer.submit(user_id, pcm)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.store.ensure_dir(self.guild_id, self.channel_id)
        self._task = asyncio.create_task(
            self._pump(), name=f"recorder-{self.guild_id}-{self.channel_id}"
        )
        log.info("recording started for channel %s", self.channel_id)

    async def stop(self) -> None:
        """Stop the pump and flush whatever is still open. Safe to call twice."""
        was_running = self._running
        self._running = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._close_segment()
        self.mixer.reset()
        if was_running:
            log.info(
                "recording stopped for channel %s after %.0fs of speech",
                self.channel_id, self.stats.recorded_seconds,
            )

    # -- pump ---------------------------------------------------------------

    async def _pump(self) -> None:
        """Pull exactly one mixed frame per ``frame_ms`` of wall-clock time."""
        anchor = time.monotonic()
        ticks = 0
        try:
            while self._running:
                ticks += 1
                drift = anchor + ticks * self._frame_seconds - time.monotonic()
                if drift > 0:
                    await asyncio.sleep(drift)
                elif drift < -1.0:
                    # We fell badly behind (blocked loop, suspended host).
                    # Re-anchor instead of spinning to catch up.
                    self.stats.late_resyncs += 1
                    anchor, ticks = time.monotonic(), 0
                await self._handle_frame()
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - keep the bot alive
            log.exception("recorder pump crashed for channel %s", self.channel_id)
            self._running = False

    async def _handle_frame(self) -> None:
        frame = self.mixer.pull()
        self.stats.frames_seen += 1
        voiced = bool(frame.speakers) and frame.rms >= self.cfg.silence_rms

        if voiced:
            if self._segment_ms is None:
                await self._open_segment()
            # A short pause belongs to the conversation: replay it so speech
            # keeps its natural rhythm.
            if self._pending_silence:
                for pending in self._pending_silence:
                    await self._write(pending)
                self._pending_silence.clear()
            await self._write(frame.pcm)
            return

        if self._segment_ms is None:
            return

        self._pending_silence.append(frame.pcm)
        if len(self._pending_silence) >= self.cfg.silence_frames:
            self._pending_silence.clear()
            await self._close_segment()

    # -- segments and chunks ------------------------------------------------

    async def _open_segment(self) -> None:
        self._segment_ms = now_ms()
        self.stats.segments += 1
        log.debug("segment opened for channel %s", self.channel_id)
        await self._open_chunk()

    async def _close_segment(self) -> None:
        await self._close_chunk()
        if self._segment_ms is not None:
            log.debug("segment closed for channel %s", self.channel_id)
        self._segment_ms = None
        self._pending_silence.clear()

    async def _open_chunk(self) -> None:
        self._chunk_start_ms = now_ms()
        self._chunk_frames = 0
        self._chunk_part = self.store.part_path(
            self.guild_id, self.channel_id, self._chunk_start_ms
        )
        self._encoder = Mp3Encoder(
            self._chunk_part,
            binary=self.cfg.ffmpeg,
            sample_rate=self.cfg.sample_rate,
            bitrate=self.cfg.mp3_bitrate,
        )
        await self._encoder.start()

    async def _close_chunk(self) -> Chunk | None:
        encoder, self._encoder = self._encoder, None
        part, self._chunk_part = self._chunk_part, None
        frames, self._chunk_frames = self._chunk_frames, 0
        if encoder is None or part is None:
            return None
        await encoder.close()
        chunk = self.store.commit(
            part,
            start_ms=self._chunk_start_ms,
            duration_ms=int(frames * self.cfg.frame_ms),
            segment_ms=self._segment_ms or self._chunk_start_ms,
        )
        if chunk is not None:
            self.stats.chunks += 1
            victims = self.store.trim(self.guild_id, self.channel_id)
            self.stats.trimmed_chunks += len(victims)
        return chunk

    async def _write(self, pcm: bytes) -> None:
        if self._encoder is None:
            await self._open_chunk()
        assert self._encoder is not None
        self._encoder.write(pcm)
        self._chunk_frames += 1
        self.stats.frames_written += 1
        if self._chunk_frames >= self.cfg.chunk_frames:
            await self._rotate_chunk()

    async def _rotate_chunk(self) -> None:
        """Close the current chunk and continue the same segment in a new one."""
        await self._close_chunk()
        if self._segment_ms is not None:
            await self._open_chunk()

    async def flush(self) -> None:
        """Make everything recorded so far visible on disk (used before export)."""
        if self._chunk_frames > 0:
            await self._rotate_chunk()

    # -- introspection ------------------------------------------------------

    def describe(self) -> dict:
        return {
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "running": self._running,
            "in_segment": self.recording,
            "buffered_ms": self.store.total_duration_ms(self.guild_id, self.channel_id),
            "segments": self.stats.segments,
            "chunks": self.stats.chunks,
            "recorded_seconds": round(self.stats.recorded_seconds, 1),
            "trimmed_chunks": self.stats.trimmed_chunks,
            "dropped_bytes": self.mixer.dropped_bytes,
            "late_resyncs": self.stats.late_resyncs,
        }

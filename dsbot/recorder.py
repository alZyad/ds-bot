"""The per-channel recording state machine.

    voice packets ──▶ Mixer ──▶ pump (one frame every 20 ms) ──▶ segments
                                                                   │
                                                       chunks ─────┘──▶ ChunkStore

Three ideas do all the work:

*Segments* are stretches of actual conversation.  A segment opens on the first
voiced frame and closes after ``silence_timeout`` of silence.  The silence that
ends a segment is buffered, not written: if somebody speaks again before the
timeout the buffered frames are flushed (so a natural pause is preserved), and
if nobody does they are dropped (so the recording holds no dead air).

*Chunks* are fixed-size slices of a segment (``chunk_seconds`` of audio) written
as individual AAC files.  They are the unit of retention: deleting one costs a
handful of ``unlink`` calls and one minute of the oldest audio.

*Tracks* are the parallel recordings written for each chunk: always ``mix``, and
one per speaker while the segment has no more than ``max_speaker_tracks`` of
them.  Every track of a chunk is written frame-for-frame off the same pump, and
a speaker who joins mid-chunk gets silence back-filled to the chunk start, so
all tracks in a chunk are the same length and line up sample for sample.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .audio import MixedFrame, Mixer
from .config import Config
from .encoder import AudioEncoder
from .store import MIX_TRACK, Chunk, ChunkStore, now_ms

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
    tracks_given_up: int = 0

    @property
    def recorded_seconds(self) -> float:
        return self.frames_written * 0.02


@dataclass
class _Track:
    """One ffmpeg process writing one track of the current chunk.

    A *frozen* track has stopped taking real audio but still receives silence,
    so it stays exactly as long as the mix.  That is what keeps a chunk's tracks
    aligned when the speaker limit is hit part-way through it.
    """

    encoder: AudioEncoder
    part: Path
    frames: int = 0
    frozen: bool = False


class ChannelRecorder:
    """Records one voice channel for as long as it is eligible."""

    def __init__(
        self,
        cfg: Config,
        store: ChunkStore,
        *,
        guild_id: int,
        channel_id: int,
        silence_rms: int | None = None,
        on_speaker: Callable[[int], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.silence_rms = cfg.silence_rms if silence_rms is None else silence_rms
        self.on_speaker = on_speaker
        self.mixer = Mixer(frame_ms=cfg.frame_ms, sample_rate=cfg.sample_rate)
        self.stats = RecorderStats()

        self._frame_seconds = cfg.frame_ms / 1000
        self._task: asyncio.Task | None = None
        self._running = False
        self._last_rms = 0

        # current segment / chunk
        self._segment_ms: int | None = None
        self._chunk_start_ms = 0
        self._chunk_frames = 0
        self._tracks: dict[str, _Track] = {}
        self._segment_speakers: set[int] = set()
        self._tracks_enabled = cfg.max_speaker_tracks > 0
        self._pending_silence: list[MixedFrame] = []

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._running

    @property
    def recording(self) -> bool:
        """True while a segment is open, i.e. somebody is actually talking."""
        return self._segment_ms is not None

    @property
    def last_rms(self) -> int:
        """Loudness of the most recent frame, for tuning the silence gate."""
        return self._last_rms

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
        self._last_rms = frame.rms
        voiced = bool(frame.speakers) and frame.rms >= self.silence_rms

        if voiced:
            if self._segment_ms is None:
                await self._open_segment()
            # A short pause belongs to the conversation: replay it so speech
            # keeps its natural rhythm.
            if self._pending_silence:
                pending, self._pending_silence = self._pending_silence, []
                for held in pending:
                    await self._write_frame(held)
            await self._write_frame(frame)
            return

        if self._segment_ms is None:
            return

        self._pending_silence.append(frame)
        if len(self._pending_silence) >= self.cfg.silence_frames:
            self._pending_silence.clear()
            await self._close_segment()

    # -- segments -----------------------------------------------------------

    async def _open_segment(self) -> None:
        self._segment_ms = now_ms()
        self._segment_speakers = set()
        self._tracks_enabled = self.cfg.max_speaker_tracks > 0
        self.stats.segments += 1
        log.debug("segment opened for channel %s", self.channel_id)
        await self._open_chunk()

    async def _close_segment(self) -> None:
        await self._close_chunk()
        if self._segment_ms is not None:
            log.debug("segment closed for channel %s", self.channel_id)
        self._segment_ms = None
        self._segment_speakers = set()
        self._pending_silence.clear()

    # -- chunks and tracks --------------------------------------------------

    async def _open_chunk(self) -> None:
        self._chunk_start_ms = now_ms()
        self._chunk_frames = 0
        self._tracks = {}
        await self._open_track(MIX_TRACK)

    async def _open_track(self, name: str) -> _Track:
        """Start writing one track, back-filled to the start of the chunk."""
        part = self.store.part_path(
            self.guild_id, self.channel_id, self._chunk_start_ms, name
        )
        encoder = AudioEncoder(
            part,
            binary=self.cfg.ffmpeg,
            sample_rate=self.cfg.sample_rate,
            bitrate=self.cfg.audio_bitrate,
        )
        await encoder.start()
        track = _Track(encoder, part)
        # A speaker heard for the first time part-way through a chunk still has
        # to line up with the mix, so pad the time they were absent.
        if self._chunk_frames:
            silence = self.mixer.silence_frame
            for _ in range(self._chunk_frames):
                encoder.write(silence)
            track.frames = self._chunk_frames
        self._tracks[name] = track
        return track

    async def _close_chunk(self) -> list[Chunk]:
        tracks, self._tracks = self._tracks, {}
        self._chunk_frames = 0
        if not tracks:
            return []
        committed = await self._commit_tracks(tracks)
        if committed:
            self.stats.chunks += 1
            victims = self.store.trim()
            self.stats.trimmed_chunks += len(victims)
        return committed

    async def _commit_tracks(self, tracks: dict[str, _Track]) -> list[Chunk]:
        committed: list[Chunk] = []
        for name, track in tracks.items():
            await track.encoder.close()
            chunk = self.store.commit(
                track.part,
                start_ms=self._chunk_start_ms,
                duration_ms=int(track.frames * self.cfg.frame_ms),
                segment_ms=self._segment_ms or self._chunk_start_ms,
                track=name,
            )
            if chunk is not None:
                committed.append(chunk)
        return committed

    async def _give_up_tracks(self) -> None:
        """Too many speakers: keep what is written, stop capturing more.

        The open speaker tracks are *frozen* rather than closed here.  Closing
        them mid-chunk would leave them shorter than the mix, and since export
        can merge two segments into one file, a short chunk would shift every
        later frame of that speaker earlier than the mix it is supposed to line
        up with.  Feeding them silence to the end of the chunk keeps the group
        the same length, and no speaker track is opened for the rest of the
        segment.
        """
        for name, track in self._tracks.items():
            if name != MIX_TRACK:
                track.frozen = True
        self._tracks_enabled = False
        self.stats.tracks_given_up += 1
        log.info(
            "channel %s has %d distinct speakers (limit %d): keeping the mix only",
            self.channel_id, len(self._segment_speakers), self.cfg.max_speaker_tracks,
        )

    async def _note_speakers(self, speakers: Iterable[int]) -> None:
        fresh = [uid for uid in speakers if uid not in self._segment_speakers]
        if not fresh:
            return
        self._segment_speakers.update(fresh)
        if self.on_speaker is not None:
            for uid in fresh:
                self.on_speaker(uid)
        if len(self._segment_speakers) > self.cfg.max_speaker_tracks:
            await self._give_up_tracks()

    async def _write_frame(self, frame: MixedFrame) -> None:
        if MIX_TRACK not in self._tracks:
            await self._open_chunk()
        self._tracks[MIX_TRACK].encoder.write(frame.pcm)
        self._tracks[MIX_TRACK].frames += 1

        if self._tracks_enabled:
            await self._note_speakers(frame.speakers)
        if self._tracks_enabled:
            for user_id in frame.speakers:
                if str(user_id) not in self._tracks:
                    await self._open_track(str(user_id))

        silence = self.mixer.silence_frame
        for name, track in self._tracks.items():
            if name == MIX_TRACK:
                continue
            payload = silence if track.frozen else (frame.tracks.get(int(name)) or silence)
            track.encoder.write(payload)
            track.frames += 1

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
            "silence_rms": self.silence_rms,
            "last_rms": self._last_rms,
            "buffered_ms": self.store.total_duration_ms(self.guild_id, self.channel_id),
            "segments": self.stats.segments,
            "chunks": self.stats.chunks,
            "recorded_seconds": round(self.stats.recorded_seconds, 1),
            "trimmed_chunks": self.stats.trimmed_chunks,
            "speakers": sorted(self._segment_speakers),
            "tracks": self._tracks_enabled,
            "limited_frames": self.mixer.limiter.limited_frames,
            "dropped_bytes": self.mixer.dropped_bytes,
            "late_resyncs": self.stats.late_resyncs,
        }

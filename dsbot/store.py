"""On-disk ring buffer of AAC chunks, one directory per voice channel.

Everything needed to reason about a recording lives in the file *name*, so the
store survives restarts and crashes with no index to rebuild or corrupt:

    data/recordings/<guild_id>/<channel_id>/c<start>_d<duration>_s<segment>_t<track>.aac

* ``start``     wall-clock start of the chunk (unix epoch, ms)
* ``duration``  how much audio the chunk actually contains, in ms
* ``segment``   identifies the continuous conversation the chunk belongs to.
  Chunks sharing a segment are gapless; a new segment means there was more than
  ``SILENCE_TIMEOUT`` of silence in between.
* ``track``     ``mix`` for the mixed recording, or a Discord user id for one
  speaker's isolated track.

Chunks sharing a ``start`` form a *group*: the mixed chunk and the per-speaker
chunks covering the same moment.  Retention deletes whole groups, so a speaker
track can never outlive the mix it belongs to.

Files still being written carry a ``.part`` suffix and are ignored by readers,
so a chunk becomes visible atomically when it is renamed into place.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MIX_TRACK = "mix"
CHUNK_SUFFIX = ".aac"
CHUNK_RE = re.compile(
    r"^c(?P<start>\d+)_d(?P<dur>\d+)_s(?P<seg>\d+)_t(?P<track>mix|\d+)\.aac$"
)
CHUNK_GLOB = f"c*{CHUNK_SUFFIX}"


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, order=True)
class Chunk:
    """A finalised piece of recording on disk."""

    start_ms: int
    segment_ms: int
    duration_ms: int
    track: str
    path: Path

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms

    @property
    def is_mix(self) -> bool:
        return self.track == MIX_TRACK

    @property
    def user_id(self) -> int | None:
        return None if self.is_mix else int(self.track)

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    @classmethod
    def parse(cls, path: Path) -> Chunk | None:
        m = CHUNK_RE.match(path.name)
        if not m:
            return None
        return cls(
            start_ms=int(m["start"]),
            segment_ms=int(m["seg"]),
            duration_ms=int(m["dur"]),
            track=m["track"],
            path=path,
        )

    @staticmethod
    def filename(start_ms: int, duration_ms: int, segment_ms: int, track: str) -> str:
        return f"c{start_ms}_d{duration_ms}_s{segment_ms}_t{track}{CHUNK_SUFFIX}"


@dataclass(frozen=True)
class Segment:
    """A contiguous conversation, i.e. a group of chunks of one track."""

    segment_ms: int
    chunks: tuple[Chunk, ...]

    @property
    def duration_ms(self) -> int:
        return sum(c.duration_ms for c in self.chunks)

    @property
    def start_ms(self) -> int:
        return self.chunks[0].start_ms

    @property
    def end_ms(self) -> int:
        return self.chunks[-1].end_ms

    @property
    def size_bytes(self) -> int:
        return sum(c.size_bytes for c in self.chunks)


def group_segments(chunks: Sequence[Chunk]) -> list[Segment]:
    ordered: dict[int, list[Chunk]] = {}
    for chunk in sorted(chunks):
        ordered.setdefault(chunk.segment_ms, []).append(chunk)
    return [Segment(seg, tuple(items)) for seg, items in sorted(ordered.items())]


def merge_segments(segments: Sequence[Segment], gap_ms: int) -> list[Segment]:
    """Glue segments separated by no more than ``gap_ms`` into one.

    Export emits one file per segment, and a 5 s pause is enough to start a new
    one, so a chatty evening would otherwise arrive as dozens of attachments.
    The gap being merged over is silence that was never stored, so nothing is
    added to the audio — only the file boundary moves.
    """
    merged: list[Segment] = []
    for segment in sorted(segments, key=lambda s: s.start_ms):
        if merged and segment.start_ms - merged[-1].end_ms <= gap_ms:
            previous = merged[-1]
            merged[-1] = Segment(previous.segment_ms, previous.chunks + segment.chunks)
        else:
            merged.append(segment)
    return merged


class ChunkStore:
    """Reads, lists and trims the chunk directories."""

    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    # -- layout -------------------------------------------------------------

    def channel_dir(self, guild_id: int, channel_id: int) -> Path:
        return self.root / str(guild_id) / str(channel_id)

    def ensure_dir(self, guild_id: int, channel_id: int) -> Path:
        path = self.channel_dir(guild_id, channel_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def known_channels(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        if not self.root.is_dir():
            return out
        for guild_dir in self.root.iterdir():
            if not guild_dir.is_dir() or not guild_dir.name.isdigit():
                continue
            for channel_dir in guild_dir.iterdir():
                if channel_dir.is_dir() and channel_dir.name.isdigit():
                    out.append((int(guild_dir.name), int(channel_dir.name)))
        return sorted(out)

    # -- reading ------------------------------------------------------------

    def list_chunks(
        self,
        guild_id: int,
        channel_id: int,
        *,
        track: str | None = MIX_TRACK,
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[Chunk]:
        """Chunks of one channel. ``track=None`` returns every track."""
        directory = self.channel_dir(guild_id, channel_id)
        if not directory.is_dir():
            return []
        chunks: list[Chunk] = []
        for path in directory.glob(CHUNK_GLOB):
            chunk = Chunk.parse(path)
            if chunk is None:
                continue
            if track is not None and chunk.track != track:
                continue
            if since_ms is not None and chunk.end_ms <= since_ms:
                continue
            if until_ms is not None and chunk.start_ms >= until_ms:
                continue
            chunks.append(chunk)
        return sorted(chunks)

    def all_chunks(self) -> list[Chunk]:
        """Every chunk of every track in every channel, oldest first."""
        chunks: list[Chunk] = []
        for guild_id, channel_id in self.known_channels():
            chunks.extend(self.list_chunks(guild_id, channel_id, track=None))
        return sorted(chunks)

    def speakers(self, guild_id: int, channel_id: int) -> list[int]:
        """User ids that have an isolated track buffered for this channel."""
        seen = {
            chunk.user_id
            for chunk in self.list_chunks(guild_id, channel_id, track=None)
            if not chunk.is_mix
        }
        return sorted(uid for uid in seen if uid is not None)

    def total_duration_ms(self, guild_id: int, channel_id: int) -> int:
        return sum(c.duration_ms for c in self.list_chunks(guild_id, channel_id))

    def total_bytes(self, guild_id: int | None = None, channel_id: int | None = None) -> int:
        if guild_id is None or channel_id is None:
            return sum(c.size_bytes for c in self.all_chunks())
        return sum(
            c.size_bytes for c in self.list_chunks(guild_id, channel_id, track=None)
        )

    def segments(self, guild_id: int, channel_id: int, **kw) -> list[Segment]:
        return group_segments(self.list_chunks(guild_id, channel_id, **kw))

    # -- writing ------------------------------------------------------------

    def part_path(
        self, guild_id: int, channel_id: int, chunk_start_ms: int, track: str
    ) -> Path:
        directory = self.ensure_dir(guild_id, channel_id)
        return directory / f"c{chunk_start_ms}_t{track}.part"

    def commit(
        self,
        part: Path,
        *,
        start_ms: int,
        duration_ms: int,
        segment_ms: int,
        track: str,
    ) -> Chunk | None:
        """Rename a finished ``.part`` file to its final, self-describing name."""
        if not part.exists() or part.stat().st_size == 0 or duration_ms <= 0:
            part.unlink(missing_ok=True)
            return None
        final = part.with_name(Chunk.filename(start_ms, duration_ms, segment_ms, track))
        part.replace(final)
        return Chunk(start_ms, segment_ms, duration_ms, track, final)

    # -- retention ----------------------------------------------------------

    def trim(self) -> list[Chunk]:
        """Enforce the disk budget across the whole store. Returns what went."""
        victims = plan_trim(self.all_chunks(), max_bytes=self.max_bytes)
        for chunk in victims:
            try:
                chunk.path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - unlikely, keep the loop alive
                log.warning("could not delete %s", chunk.path, exc_info=True)
        if victims:
            freed = sum(c.size_bytes for c in victims)
            log.info(
                "trimmed %d file(s) (%.1f MiB) to stay under %.0f MiB",
                len(victims), freed / 1048576, self.max_bytes / 1048576,
            )
        return victims

    def purge(self, guild_id: int, channel_id: int) -> int:
        directory = self.channel_dir(guild_id, channel_id)
        removed = 0
        if not directory.is_dir():
            return 0
        for path in list(directory.glob(CHUNK_GLOB)) + list(directory.glob("*.part")):
            try:
                path.unlink()
                removed += 1
            except OSError:  # pragma: no cover
                log.warning("could not delete %s", path, exc_info=True)
        return removed

    def cleanup_parts(self, older_than_ms: int = 0) -> int:
        """Delete leftover ``.part`` files from a previous, crashed run."""
        removed = 0
        cutoff = now_ms() - older_than_ms
        for guild_id, channel_id in self.known_channels():
            for path in self.channel_dir(guild_id, channel_id).glob("*.part"):
                try:
                    if older_than_ms <= 0 or int(path.stat().st_mtime * 1000) <= cutoff:
                        path.unlink()
                        removed += 1
                except OSError:  # pragma: no cover
                    pass
        return removed


def plan_trim(chunks: Iterable[Chunk], *, max_bytes: int) -> list[Chunk]:
    """Decide which chunks to drop. Pure function, so it is easy to test.

    One rule: while the store is over budget, drop the oldest *group* — the
    mixed chunk for a moment plus every speaker track covering it.  Deleting a
    group costs a handful of ``unlink`` calls and one minute of the oldest
    audio, and it can never leave a speaker track orphaned from its mix.

    The last remaining group is always kept, so a budget smaller than a single
    chunk degrades to "hold one chunk" rather than to an empty buffer.
    """
    groups: dict[tuple[Path, int], list[Chunk]] = {}
    for chunk in chunks:
        groups.setdefault((chunk.path.parent, chunk.start_ms), []).append(chunk)

    ordered = sorted(groups.items(), key=lambda item: (item[0][1], str(item[0][0])))
    total = sum(c.size_bytes for group in groups.values() for c in group)

    victims: list[Chunk] = []
    for _, group in ordered[:-1]:  # never empty the buffer completely
        if total <= max_bytes:
            break
        victims.extend(sorted(group))
        total -= sum(c.size_bytes for c in group)
    return victims

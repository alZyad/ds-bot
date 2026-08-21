"""On-disk ring buffer of mp3 chunks, one directory per voice channel.

Everything needed to reason about a recording lives in the file *name*, so the
store survives restarts and crashes with no index to rebuild or corrupt:

    data/<guild_id>/<channel_id>/c<chunk_start_ms>_d<duration_ms>_s<segment_start_ms>.mp3

* ``chunk_start_ms``   wall-clock start of the chunk (unix epoch, ms)
* ``duration_ms``      how much audio the chunk actually contains
* ``segment_start_ms`` identifies the continuous conversation the chunk belongs
  to.  Chunks sharing a segment are gapless; a new segment means there was more
  than ``SILENCE_TIMEOUT`` of silence in between.

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

CHUNK_RE = re.compile(r"^c(?P<start>\d+)_d(?P<dur>\d+)_s(?P<seg>\d+)\.mp3$")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass(frozen=True, order=True)
class Chunk:
    """A finalised piece of recording on disk."""

    start_ms: int
    segment_ms: int
    duration_ms: int
    path: Path

    @property
    def end_ms(self) -> int:
        return self.start_ms + self.duration_ms

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
            path=path,
        )

    @staticmethod
    def filename(start_ms: int, duration_ms: int, segment_ms: int) -> str:
        return f"c{start_ms}_d{duration_ms}_s{segment_ms}.mp3"


@dataclass(frozen=True)
class Segment:
    """A contiguous conversation, i.e. a group of chunks."""

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


def group_segments(chunks: Sequence[Chunk]) -> list[Segment]:
    ordered: dict[int, list[Chunk]] = {}
    for chunk in sorted(chunks):
        ordered.setdefault(chunk.segment_ms, []).append(chunk)
    return [Segment(seg, tuple(items)) for seg, items in sorted(ordered.items())]


class ChunkStore:
    """Reads, lists and trims the chunk directories."""

    def __init__(
        self,
        root: Path,
        *,
        retention_ms: int,
        strategy: str = "oldest-chunk",
        high_water_ms: int = 0,
        low_water_ms: int = 0,
    ) -> None:
        self.root = Path(root)
        self.retention_ms = retention_ms
        self.strategy = strategy
        self.high_water_ms = high_water_ms
        self.low_water_ms = low_water_ms

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
        since_ms: int | None = None,
        until_ms: int | None = None,
    ) -> list[Chunk]:
        directory = self.channel_dir(guild_id, channel_id)
        if not directory.is_dir():
            return []
        chunks: list[Chunk] = []
        for path in directory.glob("c*.mp3"):
            chunk = Chunk.parse(path)
            if chunk is None:
                continue
            if since_ms is not None and chunk.end_ms <= since_ms:
                continue
            if until_ms is not None and chunk.start_ms >= until_ms:
                continue
            chunks.append(chunk)
        return sorted(chunks)

    def total_duration_ms(self, guild_id: int, channel_id: int) -> int:
        return sum(c.duration_ms for c in self.list_chunks(guild_id, channel_id))

    def segments(self, guild_id: int, channel_id: int, **kw) -> list[Segment]:
        return group_segments(self.list_chunks(guild_id, channel_id, **kw))

    # -- writing ------------------------------------------------------------

    def part_path(self, guild_id: int, channel_id: int, chunk_start_ms: int) -> Path:
        return self.ensure_dir(guild_id, channel_id) / f"c{chunk_start_ms}.part"

    def commit(
        self,
        part: Path,
        *,
        start_ms: int,
        duration_ms: int,
        segment_ms: int,
    ) -> Chunk | None:
        """Rename a finished ``.part`` file to its final, self-describing name."""
        if not part.exists() or part.stat().st_size == 0 or duration_ms <= 0:
            part.unlink(missing_ok=True)
            return None
        final = part.with_name(Chunk.filename(start_ms, duration_ms, segment_ms))
        part.replace(final)
        return Chunk(start_ms, segment_ms, duration_ms, final)

    # -- retention ----------------------------------------------------------

    def trim(self, guild_id: int, channel_id: int) -> list[Chunk]:
        """Apply the configured retention strategy. Returns deleted chunks."""
        chunks = self.list_chunks(guild_id, channel_id)
        victims = plan_trim(
            chunks,
            retention_ms=self.retention_ms,
            strategy=self.strategy,
            high_water_ms=self.high_water_ms,
            low_water_ms=self.low_water_ms,
        )
        for chunk in victims:
            try:
                chunk.path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - unlikely, keep the loop alive
                log.warning("could not delete %s", chunk.path, exc_info=True)
        if victims:
            dropped = sum(c.duration_ms for c in victims) / 1000
            log.info(
                "trimmed %d chunk(s) (%.0fs) from %s/%s using %s",
                len(victims), dropped, guild_id, channel_id, self.strategy,
            )
        return victims

    def purge(self, guild_id: int, channel_id: int) -> int:
        directory = self.channel_dir(guild_id, channel_id)
        removed = 0
        if not directory.is_dir():
            return 0
        for path in list(directory.glob("c*.mp3")) + list(directory.glob("*.part")):
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


def plan_trim(
    chunks: Iterable[Chunk],
    *,
    retention_ms: int,
    strategy: str = "oldest-chunk",
    high_water_ms: int = 0,
    low_water_ms: int = 0,
) -> list[Chunk]:
    """Decide which chunks to drop. Pure function, so it is easy to test.

    ``oldest-chunk``    drop the oldest chunk until we are back under the
                        retention target.  Overshoot is at most one chunk, but
                        the oldest surviving conversation may start mid-sentence.
    ``oldest-segment``  drop whole conversations, oldest first.  What is kept is
                        always a set of complete discussions; the buffer swings
                        by the size of one segment.
    ``high-water``      only trim once retention + ``high_water_ms`` is exceeded,
                        and then go down to retention - ``low_water_ms``.  Fewest
                        deletions and least IO churn, widest fluctuation.
    """
    ordered = sorted(chunks)
    total = sum(c.duration_ms for c in ordered)

    if strategy == "high-water":
        if total <= retention_ms + high_water_ms:
            return []
        target = max(0, retention_ms - low_water_ms)
    elif strategy in ("oldest-chunk", "oldest-segment"):
        if total <= retention_ms:
            return []
        target = retention_ms
    else:
        raise ValueError(f"unknown retention strategy {strategy!r}")

    victims: list[Chunk] = []

    if strategy == "oldest-segment":
        segments = group_segments(ordered)
        index = 0
        # Drop whole conversations, oldest first, while more than one remains.
        while total > target and len(segments) - index > 1:
            segment = segments[index]
            victims.extend(segment.chunks)
            total -= segment.duration_ms
            index += 1
        # A single segment can be longer than the whole target on its own (a
        # 4h meeting).  Fall back to chunk granularity inside it rather than
        # dropping the only thing we have.
        if total > target and index < len(segments):
            for chunk in segments[index].chunks[:-1]:
                if total <= target:
                    break
                victims.append(chunk)
                total -= chunk.duration_ms
        return victims

    keep_at_least_one = len(ordered) - 1
    for chunk in ordered[:keep_at_least_one]:
        if total <= target:
            break
        victims.append(chunk)
        total -= chunk.duration_ms
    return victims

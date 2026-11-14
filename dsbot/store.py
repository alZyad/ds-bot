"""On-disk store of AAC chunks, one directory per recording session.

Everything needed to reason about a recording lives in the file *name*, so the
store survives restarts and crashes with no index to rebuild or corrupt:

    data/recordings/<guild_id>/<session_ms>/c<start>_d<duration>_s<segment>_t<track>.aac
    data/recordings/<guild_id>/<session_ms>/people.txt

* ``session_ms`` wall-clock start of the session (unix epoch, ms)
* ``start``     wall-clock start of the chunk (unix epoch, ms)
* ``duration``  how much audio the chunk actually contains, in ms
* ``segment``   identifies the continuous conversation the chunk belongs to.
  Chunks sharing a segment are gapless; a new segment means there was more than
  ``SILENCE_TIMEOUT`` of silence in between.
* ``track``     ``mix`` for the mixed recording, or a Discord user id for one
  speaker's isolated track.

Retention deletes whole sessions, oldest first.

Files still being written carry a ``.part`` suffix and are ignored by readers,
so a chunk becomes visible atomically when it is renamed into place.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

log = logging.getLogger(__name__)

MIX_TRACK = "mix"
CHUNK_SUFFIX = ".aac"
CHUNK_RE = re.compile(
    r"^c(?P<start>\d+)_d(?P<dur>\d+)_s(?P<seg>\d+)_t(?P<track>mix|\d+)\.aac$"
)
CHUNK_GLOB = f"c*{CHUNK_SUFFIX}"
PEOPLE_FILE = "people.txt"


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
class Session:
    guild_id: int
    session_ms: int
    path: Path
    chunks: tuple[Chunk, ...]

    @property
    def mix(self) -> list[Chunk]:
        return [c for c in self.chunks if c.is_mix]

    @property
    def start_ms(self) -> int:
        return self.session_ms

    @property
    def end_ms(self) -> int:
        return max((c.end_ms for c in self.chunks), default=self.session_ms)

    @property
    def duration_ms(self) -> int:
        return sum(c.duration_ms for c in self.mix)

    @cached_property
    def size_bytes(self) -> int:
        return sum(c.size_bytes for c in self.chunks)

    @property
    def speakers(self) -> list[int]:
        return sorted({c.user_id for c in self.chunks if c.user_id is not None})


class ChunkStore:
    def __init__(self, root: Path, *, max_bytes: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def session_dir(self, guild_id: int, session_ms: int) -> Path:
        return self.root / str(guild_id) / str(session_ms)

    def _session_dirs(self, guild_id: int | None = None) -> list[tuple[int, int, Path]]:
        if not self.root.is_dir():
            return []
        guild_dirs = (
            [self.root / str(guild_id)] if guild_id is not None else self.root.iterdir()
        )
        out = []
        for guild_dir in guild_dirs:
            if not guild_dir.is_dir() or not guild_dir.name.isdigit():
                continue
            for path in guild_dir.iterdir():
                if path.is_dir() and path.name.isdigit():
                    out.append((int(guild_dir.name), int(path.name), path))
        return sorted(out, key=lambda item: (item[1], item[0]))

    def list_chunks(
        self, guild_id: int, session_ms: int, *, track: str | None = MIX_TRACK
    ) -> list[Chunk]:
        return _chunks_in(self.session_dir(guild_id, session_ms), track)

    def sessions(self, guild_id: int | None = None) -> list[Session]:
        return [
            Session(gid, sid, path, tuple(_chunks_in(path, None)))
            for gid, sid, path in self._session_dirs(guild_id)
        ]

    def session(self, guild_id: int, session_ms: int) -> Session | None:
        path = self.session_dir(guild_id, session_ms)
        if not path.is_dir():
            return None
        return Session(guild_id, session_ms, path, tuple(_chunks_in(path, None)))

    def latest_session(self, guild_id: int) -> Session | None:
        dirs = self._session_dirs(guild_id)
        if not dirs:
            return None
        gid, sid, path = dirs[-1]
        return Session(gid, sid, path, tuple(_chunks_in(path, None)))

    def total_bytes(self) -> int:
        return sum(s.size_bytes for s in self.sessions())

    def people(self, guild_id: int, session_ms: int) -> list[int]:
        try:
            raw = (self.session_dir(guild_id, session_ms) / PEOPLE_FILE).read_text()
        except OSError:
            return []
        return list(dict.fromkeys(int(line) for line in raw.split() if line.isdigit()))

    def add_person(self, guild_id: int, session_ms: int, user_id: int) -> None:
        if user_id in self.people(guild_id, session_ms):
            return
        directory = self.session_dir(guild_id, session_ms)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / PEOPLE_FILE).open("a") as handle:
            handle.write(f"{user_id}\n")

    def part_path(
        self, guild_id: int, session_ms: int, chunk_start_ms: int, track: str
    ) -> Path:
        directory = self.session_dir(guild_id, session_ms)
        directory.mkdir(parents=True, exist_ok=True)
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

    def delete_session(self, guild_id: int, session_ms: int) -> bool:
        path = self.session_dir(guild_id, session_ms)
        if not path.is_dir():
            return False
        shutil.rmtree(path, ignore_errors=True)
        return True

    def trim(self) -> list[Session]:
        victims = plan_trim(self.sessions(), max_bytes=self.max_bytes)
        for session in victims:
            shutil.rmtree(session.path, ignore_errors=True)
            log.info(
                "deleted session %s of guild %s (%.1f MiB) to stay under %.0f MiB",
                session.session_ms, session.guild_id,
                session.size_bytes / 1048576, self.max_bytes / 1048576,
            )
        return victims

    def cleanup_parts(self) -> int:
        removed = 0
        for _, _, path in self._session_dirs():
            for part in path.glob("*.part"):
                part.unlink(missing_ok=True)
                removed += 1
        return removed


def _chunks_in(directory: Path, track: str | None) -> list[Chunk]:
    if not directory.is_dir():
        return []
    chunks = (Chunk.parse(path) for path in directory.glob(CHUNK_GLOB))
    return sorted(c for c in chunks if c is not None and (track is None or c.track == track))


def plan_trim(sessions: Iterable[Session], *, max_bytes: int) -> list[Session]:
    ordered = sorted(sessions, key=lambda s: (s.session_ms, s.guild_id))
    newest = {s.guild_id: s for s in ordered}
    total = sum(s.size_bytes for s in ordered)
    victims: list[Session] = []
    for session in ordered:
        if total <= max_bytes:
            break
        if newest[session.guild_id] is session:
            continue
        victims.append(session)
        total -= session.size_bytes
    return victims

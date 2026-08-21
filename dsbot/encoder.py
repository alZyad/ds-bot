"""ffmpeg glue: streaming mp3 encoder and export/concatenation helpers.

Raw PCM is never stored.  Each chunk is encoded on the fly by piping the mixed
frames straight into an ffmpeg process (48 kHz mono s16le in, mp3 out), which
keeps 3 hours of speech at roughly 85 MB per channel (64 kbps mono) instead
of the 2 GB the equivalent 48 kHz stereo PCM would take.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .store import Chunk

log = logging.getLogger(__name__)


class FfmpegError(RuntimeError):
    pass


def ffmpeg_available(binary: str = "ffmpeg") -> bool:
    return shutil.which(binary) is not None


async def _run(binary: str, *args: str) -> tuple[int, bytes]:
    proc = await asyncio.create_subprocess_exec(
        binary,
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    return proc.returncode or 0, stderr


class Mp3Encoder:
    """A single ffmpeg process turning a PCM stream into one mp3 file."""

    def __init__(
        self,
        destination: Path,
        *,
        binary: str = "ffmpeg",
        sample_rate: int = 48000,
        bitrate: str = "64k",
    ) -> None:
        self.destination = destination
        self.binary = binary
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self._proc: asyncio.subprocess.Process | None = None
        self.bytes_written = 0

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            self.binary,
            "-hide_banner",
            "-loglevel", "error",
            "-f", "s16le",
            "-ar", str(self.sample_rate),
            "-ac", "1",
            "-i", "pipe:0",
            "-c:a", "libmp3lame",
            "-b:a", self.bitrate,
            # The destination is a ".part" file, so the container has to be
            # named explicitly: ffmpeg cannot guess it from the extension.
            "-f", "mp3",
            "-y", str(self.destination),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def write(self, pcm: bytes) -> None:
        """Feed one frame. Never blocks; ffmpeg drains far faster than realtime."""
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            return
        try:
            proc.stdin.write(pcm)
            self.bytes_written += len(pcm)
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            log.warning("ffmpeg pipe closed early for %s", self.destination)

    async def close(self, *, timeout: float = 30.0) -> Path | None:
        """Signal EOF and wait for ffmpeg to flush the file.

        ``communicate`` (rather than ``wait``) closes stdin and drains stderr at
        the same time, so a chatty ffmpeg cannot deadlock on a full pipe.  The
        empty payload is what makes it close stdin: with ``input=None`` it
        would leave the pipe open and ffmpeg would never see EOF.
        """
        proc, self._proc = self._proc, None
        if proc is None:
            return None
        stderr = b""
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(b""), timeout=timeout)
        except TimeoutError:  # pragma: no cover
            log.warning("ffmpeg did not exit in time, killing it")
            proc.kill()
            await proc.wait()
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover
            await proc.wait()
        if proc.returncode not in (0, None):
            log.error(
                "ffmpeg exited with %s for %s: %s",
                proc.returncode, self.destination,
                (stderr or b"").decode(errors="replace")[:500],
            )
            return None
        return self.destination if self.destination.exists() else None


@dataclass(frozen=True)
class ExportPart:
    """One mp3 file to upload."""

    path: Path
    start_ms: int
    end_ms: int
    duration_ms: int
    index: int
    total: int

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:  # pragma: no cover
            return 0


def split_for_upload(chunks: Sequence[Chunk], max_bytes: int) -> list[list[Chunk]]:
    """Group chunks into batches whose concatenation fits an attachment.

    Because every chunk is already a standalone mp3 we can size the parts from
    the files themselves instead of guessing at a bitrate.
    """
    batches: list[list[Chunk]] = []
    current: list[Chunk] = []
    size = 0
    for chunk in chunks:
        chunk_size = chunk.size_bytes
        if current and size + chunk_size > max_bytes:
            batches.append(current)
            current, size = [], 0
        current.append(chunk)
        size += chunk_size
    if current:
        batches.append(current)
    return batches


async def concat(
    chunks: Sequence[Chunk],
    destination: Path,
    *,
    binary: str = "ffmpeg",
    bitrate: str = "64k",
    workdir: Path | None = None,
) -> Path:
    """Concatenate mp3 chunks into one file, re-encoding only if needed."""
    if not chunks:
        raise FfmpegError("nothing to concatenate")

    workdir = workdir or destination.parent
    workdir.mkdir(parents=True, exist_ok=True)
    listing = workdir / f"{destination.stem}.concat.txt"
    listing.write_text(
        "".join(f"file '{c.path.resolve().as_posix()}'\n" for c in chunks),
        encoding="utf-8",
    )
    common = ("-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0",
              "-i", str(listing))
    try:
        code, stderr = await _run(
            binary, *common, "-c", "copy", "-y", str(destination)
        )
        if code != 0 or not destination.exists():
            # Mixed bitrates / a truncated chunk can defeat stream copy.
            log.info("stream copy failed (%s), re-encoding", code)
            code, stderr = await _run(
                binary, *common, "-c:a", "libmp3lame", "-b:a", bitrate,
                "-y", str(destination),
            )
        if code != 0 or not destination.exists():
            raise FfmpegError(stderr.decode(errors="replace")[:500] or "ffmpeg failed")
    finally:
        listing.unlink(missing_ok=True)
    return destination


async def export(
    chunks: Sequence[Chunk],
    workdir: Path,
    *,
    prefix: str,
    max_bytes: int,
    binary: str = "ffmpeg",
    bitrate: str = "64k",
) -> list[ExportPart]:
    """Build the uploadable mp3 parts for a set of chunks."""
    ordered = sorted(chunks)
    if not ordered:
        return []
    workdir.mkdir(parents=True, exist_ok=True)
    batches = split_for_upload(ordered, max_bytes)
    parts: list[ExportPart] = []
    for index, batch in enumerate(batches, start=1):
        suffix = "" if len(batches) == 1 else f"-part{index:02d}"
        destination = workdir / f"{prefix}{suffix}.mp3"
        await concat(batch, destination, binary=binary, bitrate=bitrate, workdir=workdir)
        parts.append(
            ExportPart(
                path=destination,
                start_ms=batch[0].start_ms,
                end_ms=batch[-1].end_ms,
                duration_ms=sum(c.duration_ms for c in batch),
                index=index,
                total=len(batches),
            )
        )
    return parts

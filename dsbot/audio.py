"""Real-time mixing of per-user Discord voice packets into a single timeline.

Discord delivers one RTP packet per speaking user every ``frame_ms``
milliseconds, decoded to 48 kHz 16-bit *stereo* PCM.  :class:`Mixer` collects
those packets (from the voice receive thread) and hands out one mixed *mono*
frame at a time (to the recorder's pump task), so that everything downstream
deals with a single, wall-clock-aligned audio stream.

Mono is deliberate: speech does not benefit from two channels and it halves
both the CPU of the encoder and the size of the rolling buffer.
"""

from __future__ import annotations

import array
import math
import sys
import threading
from dataclasses import dataclass

try:  # C implementation; stdlib <=3.12, or the audioop-lts backport
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - depends on interpreter version
    audioop = None

SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
INPUT_CHANNELS = 2  # Discord decodes opus to stereo
_NATIVE_LE = sys.byteorder == "little"


def _as_int16(data: bytes) -> array.array:
    buf = array.array("h")
    buf.frombytes(data)
    if not _NATIVE_LE:  # pragma: no cover - x86/arm are little endian
        buf.byteswap()
    return buf


def _to_bytes(buf: array.array) -> bytes:
    if not _NATIVE_LE:  # pragma: no cover
        buf = array.array("h", buf)
        buf.byteswap()
    return buf.tobytes()


def to_mono(data: bytes) -> bytes:
    """Downmix interleaved 16-bit stereo to 16-bit mono."""
    if audioop is not None:
        return audioop.tomono(data, SAMPLE_WIDTH, 0.5, 0.5)
    src = _as_int16(data)
    out = array.array("h", bytes(len(data) // 2))
    for i in range(len(out)):
        out[i] = (src[2 * i] + src[2 * i + 1]) // 2
    return _to_bytes(out)


def add(left: bytes, right: bytes) -> bytes:
    """Sum two equally sized 16-bit PCM fragments, clipping on overflow."""
    if audioop is not None:
        return audioop.add(left, right, SAMPLE_WIDTH)
    a = _as_int16(left)
    b = _as_int16(right)
    for i in range(len(a)):
        v = a[i] + b[i]
        a[i] = max(-32768, min(32767, v))
    return _to_bytes(a)


def rms(data: bytes) -> int:
    """Root-mean-square amplitude of a 16-bit PCM fragment (0..32767)."""
    if not data:
        return 0
    if audioop is not None:
        return audioop.rms(data, SAMPLE_WIDTH)
    buf = _as_int16(data)
    if not buf:
        return 0
    return int(math.sqrt(sum(v * v for v in buf) / len(buf)))


@dataclass(frozen=True)
class MixedFrame:
    """One ``frame_ms`` slice of the mixed conversation."""

    pcm: bytes
    rms: int
    speakers: frozenset[int]

    @property
    def silent(self) -> bool:
        return not self.speakers


class Mixer:
    """Thread-safe N-to-1 PCM mixer.

    ``submit`` is called from the voice receive thread, ``pull`` from the event
    loop.  Per-user buffers are bounded: a client that floods (or a pump task
    that stalls) drops its *oldest* audio rather than growing without limit.
    """

    def __init__(
        self,
        *,
        frame_ms: int = 20,
        sample_rate: int = 48000,
        max_buffer_ms: int = 400,
    ) -> None:
        self.frame_ms = frame_ms
        self.sample_rate = sample_rate
        samples = int(sample_rate * frame_ms / 1000)
        self.frame_size_in = samples * SAMPLE_WIDTH * INPUT_CHANNELS
        self.frame_size_out = samples * SAMPLE_WIDTH
        self._max_buffer = self.frame_size_in * max(1, max_buffer_ms // frame_ms)
        self._silence = bytes(self.frame_size_out)
        self._buffers: dict[int, bytearray] = {}
        self._lock = threading.Lock()
        self.dropped_bytes = 0

    # -- producer side (voice receive thread) --------------------------------

    def submit(self, user_id: int, pcm: bytes) -> None:
        if not pcm:
            return
        with self._lock:
            buf = self._buffers.get(user_id)
            if buf is None:
                buf = self._buffers[user_id] = bytearray()
            buf.extend(pcm)
            overflow = len(buf) - self._max_buffer
            if overflow > 0:
                del buf[:overflow]
                self.dropped_bytes += overflow

    # -- consumer side (event loop) -----------------------------------------

    def pull(self) -> MixedFrame:
        """Pop one frame worth of audio from every buffer and mix it."""
        with self._lock:
            takes: list[tuple[int, bytes]] = []
            for user_id, buf in list(self._buffers.items()):
                if not buf:
                    self._buffers.pop(user_id, None)
                    continue
                chunk = bytes(buf[: self.frame_size_in])
                del buf[: self.frame_size_in]
                takes.append((user_id, chunk))

        if not takes:
            return MixedFrame(self._silence, 0, frozenset())

        mixed: bytes | None = None
        speakers = []
        for user_id, chunk in takes:
            if len(chunk) < self.frame_size_in:  # partial packet, pad with silence
                chunk = chunk + bytes(self.frame_size_in - len(chunk))
            mono = to_mono(chunk)
            mixed = mono if mixed is None else add(mixed, mono)
            speakers.append(user_id)

        assert mixed is not None
        return MixedFrame(mixed, rms(mixed), frozenset(speakers))

    def reset(self) -> None:
        with self._lock:
            self._buffers.clear()

    @property
    def silence_frame(self) -> bytes:
        return self._silence

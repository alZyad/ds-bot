"""Real-time mixing of per-user Discord voice packets into aligned timelines.

Discord delivers one RTP packet per speaking user every ``frame_ms``
milliseconds, decoded to 48 kHz 16-bit *stereo* PCM.  :class:`Mixer` collects
those packets (from the voice receive thread) and hands out, one frame at a
time (to the recorder's pump task):

* the *mixed* mono frame, peak-limited, which is what everybody listens to;
* the individual mono frame of every speaker heard in that frame, untouched.

Because both come out of the same pull, driven by the same wall clock, the
mixed track and every per-speaker track share one sample-accurate timeline for
free.

Mono is deliberate and costs nothing: each user's opus stream is mono at the
source and merely upmixed to stereo for delivery, so a stereo buffer would be
twice the size for exactly the same information.
"""

from __future__ import annotations

import array
import math
import sys
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from types import MappingProxyType

try:  # C implementation; stdlib <=3.12, or the audioop-lts backport
    import audioop  # type: ignore
except ImportError:  # pragma: no cover - depends on interpreter version
    audioop = None

SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
INPUT_CHANNELS = 2  # Discord decodes opus to stereo
FULL_SCALE = 32767
_NATIVE_LE = sys.byteorder == "little"
_EMPTY_TRACKS: dict[int, bytes] = {}


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
    """Sum two equally sized 16-bit PCM fragments, clipping on overflow.

    Only safe when the sum is known to fit; :class:`Limiter` is what keeps the
    mixed frame inside full scale.
    """
    if audioop is not None:
        return audioop.add(left, right, SAMPLE_WIDTH)
    a = _as_int16(left)
    b = _as_int16(right)
    for i in range(len(a)):
        a[i] = max(-32768, min(FULL_SCALE, a[i] + b[i]))
    return _to_bytes(a)


def peak(data: bytes) -> int:
    """Largest absolute sample value in a 16-bit PCM fragment."""
    if not data:
        return 0
    if audioop is not None:
        return audioop.max(data, SAMPLE_WIDTH)
    return max((abs(v) for v in _as_int16(data)), default=0)


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


class Limiter:
    """Peak limiter for the mixed track.

    Adding several voices together can exceed full scale.  Chopping the peaks
    off — which is what a plain 16-bit sum does — sounds like harsh crackling,
    and it happens exactly during the crosstalk you most want to replay.  So
    instead we sum with 32-bit headroom and turn the gain down only for the
    frames that would have overflowed, easing it back up afterwards.

    There is no lookahead beyond the frame in hand, which is all we need: the
    whole frame is available before anything is written, so *attack* can apply
    the full required gain across it and guarantee it cannot clip.  The cost is
    a gain step at the frame boundary when crosstalk starts; at 20 ms that is
    inaudible next to the clipping it replaces.  *Release* ramps within the
    frame, which is safe because rising gain only ever happens while the peak
    is already below the ceiling.
    """

    def __init__(
        self,
        *,
        ceiling: int = FULL_SCALE,
        release_ms: float = 200.0,
        frame_ms: int = 20,
    ) -> None:
        self.ceiling = ceiling
        self._release_step = min(1.0, frame_ms / max(float(frame_ms), release_ms))
        self._gain = 1.0
        self.limited_frames = 0

    @property
    def gain(self) -> float:
        """Current attenuation. 1.0 means the limiter is doing nothing."""
        return self._gain

    def reset(self) -> None:
        self._gain = 1.0

    def process(self, samples: Sequence[int]) -> bytes:
        """Apply the limiter to one frame of 32-bit-headroom samples."""
        count = len(samples)
        if not count:
            return b""
        loudest = max(max(samples), -min(samples))
        target = 1.0 if loudest <= self.ceiling else self.ceiling / loudest
        start = self._gain

        if target <= start:  # attack: whole frame at the new gain, cannot clip
            gain_start = gain_end = target
        else:  # release: ramp back up, safe because the peak is already low
            gain_start = start
            gain_end = min(target, start + self._release_step)

        if gain_start < 1.0 or gain_end < 1.0:
            self.limited_frames += 1
        self._gain = gain_end

        out = array.array("h", bytes(count * SAMPLE_WIDTH))
        step = (gain_end - gain_start) / count
        for i in range(count):
            value = int(samples[i] * (gain_start + step * i))
            out[i] = -32768 if value < -32768 else min(value, FULL_SCALE)
        return _to_bytes(out)


def mix(tracks: Sequence[bytes], limiter: Limiter) -> bytes:
    """Sum mono frames into one, limiting instead of clipping."""
    if not tracks:
        return b""
    if len(tracks) == 1 and limiter.gain >= 1.0 and peak(tracks[0]) <= limiter.ceiling:
        return tracks[0]

    # Fast path: if the individual peaks cannot possibly add up past the
    # ceiling then no limiting is needed and the C summation is safe.
    if limiter.gain >= 1.0 and sum(peak(t) for t in tracks) <= limiter.ceiling:
        mixed = tracks[0]
        for other in tracks[1:]:
            mixed = add(mixed, other)
        return mixed

    total = _as_int16(tracks[0])
    accumulator = [int(v) for v in total]
    for other in tracks[1:]:
        for index, value in enumerate(_as_int16(other)):
            accumulator[index] += value
    return limiter.process(accumulator)


@dataclass(frozen=True)
class MixedFrame:
    """One ``frame_ms`` slice of the conversation, mixed and per speaker."""

    pcm: bytes
    rms: int
    speakers: frozenset[int]
    tracks: dict[int, bytes]

    @property
    def silent(self) -> bool:
        return not self.speakers


class Mixer:
    """Thread-safe N-to-1 PCM mixer that also keeps the individual tracks.

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
        self.limiter = Limiter(frame_ms=frame_ms)
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
            return MixedFrame(self._silence, 0, frozenset(), _EMPTY_TRACKS)

        tracks: dict[int, bytes] = {}
        for user_id, chunk in takes:
            if len(chunk) < self.frame_size_in:  # partial packet, pad with silence
                chunk = chunk + bytes(self.frame_size_in - len(chunk))
            tracks[user_id] = to_mono(chunk)

        mixed = mix(list(tracks.values()), self.limiter)
        return MixedFrame(
            mixed, rms(mixed), frozenset(tracks), MappingProxyType(tracks)  # type: ignore[arg-type]
        )

    def reset(self) -> None:
        with self._lock:
            self._buffers.clear()
        self.limiter.reset()

    @property
    def silence_frame(self) -> bytes:
        return self._silence

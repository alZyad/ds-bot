"""The same pipeline against the real ffmpeg, skipped when it is not installed.

The stubbed suite cannot catch encoder-invocation mistakes (a missing output
format, a bad flag order), so this runs the actual binary end to end.
"""

import math
import struct

import pytest

from dsbot.config import Config
from dsbot.encoder import export, ffmpeg_available
from dsbot.recorder import ChannelRecorder
from dsbot.store import ChunkStore

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg is not installed"
)


def tone(freq=440.0, amp=9000):
    """20 ms of 48 kHz stereo PCM, as Discord would decode it."""
    samples = []
    for i in range(960):
        value = int(amp * math.sin(2 * math.pi * freq * i / 48000))
        samples += [value, value]
    return struct.pack("<1920h", *samples)


@pytest.fixture
def real_cfg(tmp_path):
    return Config(
        data_dir=tmp_path,
        silence_timeout=0.2,     # 10 frames
        silence_rms=100,
        chunk_seconds=0.5,       # 25 frames
        retention_seconds=1.5,
    )


async def test_the_whole_pipeline_produces_playable_mp3(real_cfg, tmp_path):
    store = ChunkStore(
        real_cfg.data_dir / "recordings", retention_ms=real_cfg.retention_ms
    )
    recorder = ChannelRecorder(real_cfg, store, guild_id=1, channel_id=2)
    recorder._running = True

    async def speak(frames, users=(1,)):
        for _ in range(frames):
            for user in users:
                recorder.mixer.submit(user, tone(440 * user))
            await recorder._handle_frame()

    async def quiet(frames):
        for _ in range(frames):
            await recorder._handle_frame()

    await speak(50, users=(1, 2))  # 1s, two speakers
    await quiet(5)                 # short pause, same segment
    await speak(25)                # 0.5s
    await quiet(15)                # past the timeout, segment closes
    await speak(50)                # 1s, new segment, forces a trim
    await recorder.stop()

    chunks = store.list_chunks(1, 2)
    assert chunks, "ffmpeg should have produced chunks"
    assert all(c.size_bytes > 0 for c in chunks), "no chunk may be empty"
    assert sum(c.duration_ms for c in chunks) <= real_cfg.retention_ms
    assert recorder.stats.segments == 2

    parts = await export(
        chunks, tmp_path / "out", prefix="voice", max_bytes=10 * 1024 * 1024,
        binary=real_cfg.ffmpeg, bitrate=real_cfg.mp3_bitrate,
    )
    assert len(parts) == 1
    data = parts[0].path.read_bytes()
    assert len(data) > 1000
    # an mp3 frame header, possibly after an ID3 tag
    assert data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")


async def test_export_splits_real_files(real_cfg, tmp_path):
    store = ChunkStore(
        real_cfg.data_dir / "recordings", retention_ms=real_cfg.retention_ms
    )
    recorder = ChannelRecorder(real_cfg, store, guild_id=1, channel_id=3)
    recorder._running = True
    for _ in range(75):
        recorder.mixer.submit(1, tone())
        await recorder._handle_frame()
    await recorder.stop()

    chunks = store.list_chunks(1, 3)
    smallest = min(c.size_bytes for c in chunks)
    parts = await export(
        chunks, tmp_path / "out", prefix="voice", max_bytes=smallest + 1,
        binary=real_cfg.ffmpeg, bitrate=real_cfg.mp3_bitrate,
    )
    assert len(parts) == len(chunks) > 1
    assert all(p.path.exists() and p.size_bytes > 0 for p in parts)

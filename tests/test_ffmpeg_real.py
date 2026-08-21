"""Integration tests against the real ffmpeg binary.

The fake in ``fake_ffmpeg.py`` keeps the unit tests fast, but it cannot catch a
wrong ffmpeg invocation — and that has bitten this project once already, when
every chunk silently failed to encode because the output container was never
named. These tests exercise the actual binary, and are skipped when it is
absent.
"""

import asyncio
import json
import shutil
import struct

import pytest

from dsbot.encoder import AudioEncoder, concat, export, ffmpeg_available
from dsbot.store import MIX_TRACK, ChunkStore, group_segments

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg is not installed"
)

SAMPLE_RATE = 48000
FRAME_SAMPLES = SAMPLE_RATE // 50  # 20 ms of mono


def tone(frames, value=8000):
    """``frames`` * 20 ms of a square wave, as raw mono s16le."""
    half = FRAME_SAMPLES // 2
    one = struct.pack(f"<{half}h", *([value] * half)) + struct.pack(
        f"<{half}h", *([-value] * half)
    )
    return one * frames


async def probe(path):
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_format", "-show_streams",
        "-of", "json", str(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    assert proc.returncode == 0, err.decode()
    return json.loads(out)


async def test_a_chunk_really_encodes_to_aac(tmp_path):
    part = tmp_path / "c1_tmix.part"  # no extension ffmpeg recognises
    encoder = AudioEncoder(part, sample_rate=SAMPLE_RATE, bitrate="64k")
    await encoder.start()
    encoder.write(tone(50))  # one second
    assert await encoder.close() == part

    assert part.stat().st_size > 0
    info = await probe(part)
    stream = info["streams"][0]
    assert stream["codec_name"] == "aac"
    assert int(stream["sample_rate"]) == SAMPLE_RATE
    assert stream["channels"] == 1
    assert 0.9 < float(info["format"]["duration"]) < 1.2


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe is not installed")
async def test_chunks_concatenate_into_a_playable_m4a(tmp_path):
    store = ChunkStore(tmp_path / "rec", max_bytes=100_000_000)
    chunks = []
    for index in range(3):
        start = index * 1000
        part = store.part_path(1, 2, start, MIX_TRACK)
        encoder = AudioEncoder(part, sample_rate=SAMPLE_RATE, bitrate="64k")
        await encoder.start()
        encoder.write(tone(50, value=4000 * (index + 1)))
        await encoder.close()
        chunks.append(store.commit(
            part, start_ms=start, duration_ms=1000, segment_ms=0, track=MIX_TRACK
        ))

    destination = tmp_path / "joined.m4a"
    await concat(chunks, destination, workdir=tmp_path / "work")

    info = await probe(destination)
    assert info["streams"][0]["codec_name"] == "aac"
    assert "m4a" in info["format"]["format_name"]
    # three seconds of audio, plus the encoder padding each chunk carries
    duration = float(info["format"]["duration"])
    assert 3.0 <= duration < 3.3
    assert not list((tmp_path / "work").glob("*.joined.aac"))


async def test_export_writes_one_playable_file_per_segment(tmp_path):
    store = ChunkStore(tmp_path / "rec", max_bytes=100_000_000)
    chunks = []
    for index, segment in ((0, 0), (1, 0), (5, 5000)):
        start = index * 1000
        part = store.part_path(1, 2, start, MIX_TRACK)
        encoder = AudioEncoder(part, sample_rate=SAMPLE_RATE, bitrate="64k")
        await encoder.start()
        encoder.write(tone(50))
        await encoder.close()
        chunks.append(store.commit(
            part, start_ms=start, duration_ms=1000, segment_ms=segment,
            track=MIX_TRACK,
        ))

    parts = await export(
        group_segments(chunks), tmp_path / "out", prefix="general",
        max_bytes=10_000_000,
    )
    assert [p.path.name for p in parts] == ["general-01.m4a", "general-02.m4a"]
    for part in parts:
        info = await probe(part.path)
        assert info["streams"][0]["codec_name"] == "aac"
        assert part.size_bytes > 0


async def test_the_output_format_flag_is_load_bearing(tmp_path):
    """Without ``-f adts`` ffmpeg cannot guess the container of a .part file.

    This is the exact failure that once shipped: every chunk encode returned
    234 and the buffer stayed empty, while the test double happily pretended it
    had worked.
    """
    destination = tmp_path / "out.part"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1", "-i", "pipe:0",
        "-c:a", "aac", "-b:a", "64k", "-y", str(destination),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate(tone(10))
    assert proc.returncode != 0
    assert b"Unable to choose an output format" in stderr
    assert not destination.exists()

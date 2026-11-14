from pathlib import Path

import pytest

from dsbot.encoder import (
    AudioEncoder,
    FfmpegError,
    concat,
    ffmpeg_available,
)
from dsbot.store import MIX_TRACK, ChunkStore

FAKE_FFMPEG = Path(__file__).with_name("fake_ffmpeg.py")
MIN = 60_000


def store_at(tmp_path):
    return ChunkStore(tmp_path, max_bytes=100_000_000)


def write_chunk(store, index, payload, *, segment=0, track=MIX_TRACK):
    start = index * MIN
    part = store.part_path(1, 2, start, track)
    part.write_bytes(payload)
    return store.commit(
        part, start_ms=start, duration_ms=MIN, segment_ms=segment, track=track
    )


def test_ffmpeg_available_detects_a_missing_binary():
    assert not ffmpeg_available("definitely-not-a-real-binary-xyz")


async def test_encoder_streams_pcm_into_a_file(tmp_path):
    destination = tmp_path / "out.part"  # the real store writes to .part
    encoder = AudioEncoder(destination, binary=str(FAKE_FFMPEG))
    await encoder.start()
    assert encoder.running
    for _ in range(5):
        encoder.write(b"\x01\x02" * 960)
    assert await encoder.close() == destination
    assert destination.stat().st_size == 5 * 1920
    assert encoder.bytes_written == 5 * 1920
    # closing twice is harmless
    assert await encoder.close() is None


async def test_concat_joins_chunks_in_order(tmp_path):
    store = store_at(tmp_path / "rec")
    chunks = [write_chunk(store, i, bytes([65 + i]) * 10) for i in range(3)]
    destination = tmp_path / "joined.m4a"
    await concat(chunks, destination, binary=str(FAKE_FFMPEG))
    assert destination.read_bytes() == b"A" * 10 + b"B" * 10 + b"C" * 10
    # the intermediate joined stream is cleaned up
    assert list(tmp_path.glob("*.joined.aac")) == []


async def test_concat_survives_a_chunk_trimmed_mid_export(tmp_path):
    store = store_at(tmp_path / "rec")
    chunks = [write_chunk(store, i, bytes([65 + i]) * 10) for i in range(3)]
    chunks[1].path.unlink()  # retention got there first
    destination = tmp_path / "joined.m4a"
    await concat(chunks, destination, binary=str(FAKE_FFMPEG))
    assert destination.read_bytes() == b"A" * 10 + b"C" * 10


async def test_concat_requires_input(tmp_path):
    with pytest.raises(FfmpegError):
        await concat([], tmp_path / "x.m4a", binary=str(FAKE_FFMPEG))


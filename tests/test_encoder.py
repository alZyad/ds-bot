from pathlib import Path

import pytest

from dsbot.encoder import (
    FfmpegError,
    Mp3Encoder,
    concat,
    export,
    ffmpeg_available,
    split_for_upload,
)
from dsbot.store import ChunkStore

FAKE_FFMPEG = Path(__file__).with_name("fake_ffmpeg.py")


def write_chunk(store, index, payload):
    start = index * 60_000
    part = store.part_path(1, 2, start)
    part.write_bytes(payload)
    return store.commit(part, start_ms=start, duration_ms=60_000, segment_ms=0)


def test_ffmpeg_available_detects_a_missing_binary():
    assert not ffmpeg_available("definitely-not-a-real-binary-xyz")


def test_split_for_upload_groups_by_real_file_size(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=10_000_000)
    chunks = [write_chunk(store, i, b"x" * 400) for i in range(5)]
    batches = split_for_upload(chunks, max_bytes=1000)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_split_for_upload_never_drops_an_oversized_chunk(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=10_000_000)
    chunks = [write_chunk(store, 0, b"x" * 5000)]
    assert split_for_upload(chunks, max_bytes=100) == [chunks]
    assert split_for_upload([], max_bytes=100) == []


async def test_encoder_streams_pcm_into_a_file(tmp_path):
    destination = tmp_path / "out.mp3"
    encoder = Mp3Encoder(destination, binary=str(FAKE_FFMPEG))
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
    store = ChunkStore(tmp_path / "rec", retention_ms=10_000_000)
    chunks = [write_chunk(store, i, bytes([65 + i]) * 10) for i in range(3)]
    destination = tmp_path / "joined.mp3"
    await concat(chunks, destination, binary=str(FAKE_FFMPEG))
    assert destination.read_bytes() == b"A" * 10 + b"B" * 10 + b"C" * 10
    # the temporary concat listing is cleaned up
    assert list(tmp_path.glob("*.concat.txt")) == []


async def test_concat_requires_input(tmp_path):
    with pytest.raises(FfmpegError):
        await concat([], tmp_path / "x.mp3", binary=str(FAKE_FFMPEG))


async def test_export_produces_one_file_when_it_fits(tmp_path):
    store = ChunkStore(tmp_path / "rec", retention_ms=10_000_000)
    chunks = [write_chunk(store, i, b"x" * 100) for i in range(4)]
    parts = await export(
        chunks, tmp_path / "out", prefix="general", max_bytes=10_000,
        binary=str(FAKE_FFMPEG),
    )
    assert len(parts) == 1
    part = parts[0]
    assert part.path.name == "general.mp3"
    assert part.index == 1 and part.total == 1
    assert part.duration_ms == 4 * 60_000
    assert part.start_ms == 0 and part.end_ms == 4 * 60_000
    assert part.size_bytes == 400


async def test_export_splits_into_numbered_parts(tmp_path):
    store = ChunkStore(tmp_path / "rec", retention_ms=10_000_000)
    chunks = [write_chunk(store, i, b"x" * 400) for i in range(5)]
    parts = await export(
        chunks, tmp_path / "out", prefix="general", max_bytes=1000,
        binary=str(FAKE_FFMPEG),
    )
    assert [p.path.name for p in parts] == [
        "general-part01.mp3", "general-part02.mp3", "general-part03.mp3",
    ]
    assert [p.total for p in parts] == [3, 3, 3]
    assert sum(p.duration_ms for p in parts) == 5 * 60_000
    assert all(p.size_bytes <= 1000 for p in parts)


async def test_export_of_nothing_is_empty(tmp_path):
    assert await export([], tmp_path, prefix="x", max_bytes=100) == []

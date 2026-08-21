from pathlib import Path

import pytest

from dsbot.encoder import (
    AudioEncoder,
    FfmpegError,
    concat,
    export,
    ffmpeg_available,
    split_for_upload,
)
from dsbot.store import MIX_TRACK, ChunkStore, group_segments

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


def test_split_for_upload_groups_by_real_file_size(tmp_path):
    store = store_at(tmp_path)
    chunks = [write_chunk(store, i, b"x" * 400) for i in range(5)]
    batches = split_for_upload(chunks, max_bytes=1000)
    assert [len(b) for b in batches] == [2, 2, 1]


def test_split_for_upload_never_drops_an_oversized_chunk(tmp_path):
    store = store_at(tmp_path)
    chunks = [write_chunk(store, 0, b"x" * 5000)]
    assert split_for_upload(chunks, max_bytes=100) == [chunks]
    assert split_for_upload([], max_bytes=100) == []


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


async def test_export_writes_one_file_per_segment(tmp_path):
    store = store_at(tmp_path / "rec")
    chunks = [write_chunk(store, i, b"x" * 100, segment=0) for i in range(2)]
    chunks += [write_chunk(store, i, b"x" * 100, segment=10 * MIN) for i in (10, 11)]
    segments = group_segments(chunks)

    parts = await export(
        segments, tmp_path / "out", prefix="general", max_bytes=10_000,
        binary=str(FAKE_FFMPEG),
    )
    assert [p.path.name for p in parts] == ["general-01.m4a", "general-02.m4a"]
    assert [p.duration_ms for p in parts] == [2 * MIN, 2 * MIN]
    assert parts[0].start_ms == 0 and parts[1].start_ms == 10 * MIN


async def test_export_does_not_number_a_lone_segment(tmp_path):
    store = store_at(tmp_path / "rec")
    chunks = [write_chunk(store, i, b"x" * 100) for i in range(4)]
    parts = await export(
        group_segments(chunks), tmp_path / "out", prefix="general",
        max_bytes=10_000, binary=str(FAKE_FFMPEG),
    )
    assert len(parts) == 1
    part = parts[0]
    assert part.path.name == "general.m4a"
    assert part.index == 1 and part.total == 1
    assert part.duration_ms == 4 * MIN
    assert part.start_ms == 0 and part.end_ms == 4 * MIN
    assert part.size_bytes == 400


async def test_export_splits_only_an_oversized_segment(tmp_path):
    store = store_at(tmp_path / "rec")
    chunks = [write_chunk(store, i, b"x" * 400) for i in range(5)]
    parts = await export(
        group_segments(chunks), tmp_path / "out", prefix="general",
        max_bytes=1000, binary=str(FAKE_FFMPEG),
    )
    assert [p.path.name for p in parts] == [
        "general-part01.m4a", "general-part02.m4a", "general-part03.m4a",
    ]
    assert [p.total for p in parts] == [3, 3, 3]
    assert sum(p.duration_ms for p in parts) == 5 * MIN
    assert all(p.size_bytes <= 1000 for p in parts)


async def test_export_of_nothing_is_empty(tmp_path):
    assert await export([], tmp_path, prefix="x", max_bytes=100) == []

from pathlib import Path

import pytest

from dsbot.store import (
    MIX_TRACK,
    Chunk,
    ChunkStore,
    group_segments,
    merge_segments,
    plan_trim,
)

MIN = 60_000
KB = 1024


@pytest.fixture
def store(tmp_path):
    return ChunkStore(tmp_path, max_bytes=10 * KB)


def write(store, minute, *, size=KB, segment_min=None, track=MIX_TRACK, channel=2):
    """Commit a chunk of a known size so byte-based trimming is testable."""
    start = minute * MIN
    segment = start if segment_min is None else segment_min * MIN
    part = store.part_path(1, channel, start, track)
    part.write_bytes(b"x" * size)
    chunk = store.commit(
        part, start_ms=start, duration_ms=MIN, segment_ms=segment, track=track
    )
    assert chunk is not None
    return chunk


def minutes(chunks):
    return [c.start_ms // MIN for c in chunks]


def test_filename_roundtrip():
    name = Chunk.filename(1700000000000, 60000, 1699999000000, MIX_TRACK)
    parsed = Chunk.parse(Path(name))
    assert parsed == Chunk(1700000000000, 1699999000000, 60000, MIX_TRACK, Path(name))
    assert parsed.is_mix and parsed.user_id is None

    speaker = Chunk.parse(Path(Chunk.filename(1, 2, 3, "424242")))
    assert not speaker.is_mix and speaker.user_id == 424242

    assert Chunk.parse(Path("not-a-chunk.aac")) is None
    assert Chunk.parse(Path("c1_d2_s3_tmix.mp3")) is None  # old layout
    assert Chunk.parse(Path("c1_d2_s3_tbogus.aac")) is None
    assert Chunk.parse(Path("c1_t mix.part")) is None


def test_group_segments():
    def chunk(minute, segment_min):
        return Chunk(minute * MIN, segment_min * MIN, MIN, MIX_TRACK, Path("x"))

    chunks = [chunk(i, 0) for i in range(3)] + [chunk(i, 10) for i in (10, 11)]
    segments = group_segments(chunks)
    assert [s.segment_ms // MIN for s in segments] == [0, 10]
    assert segments[0].duration_ms == 3 * MIN
    assert segments[1].start_ms // MIN == 10 and segments[1].end_ms // MIN == 12


def test_merge_segments_glues_short_gaps_only():
    def segment(start_min, length=1):
        chunks = tuple(
            Chunk((start_min + i) * MIN, start_min * MIN, MIN, MIX_TRACK, Path("x"))
            for i in range(length)
        )
        return group_segments(chunks)[0]

    # 0..1, then 1..2 (touching), then 30..31 (far away)
    segments = [segment(0), segment(1), segment(30)]
    merged = merge_segments(segments, gap_ms=2 * MIN)
    assert len(merged) == 2
    assert merged[0].duration_ms == 2 * MIN
    assert merged[1].start_ms // MIN == 30
    # with no tolerance the touching pair still merges (gap is zero), the rest not
    assert len(merge_segments(segments, gap_ms=0)) == 2
    assert len(merge_segments(segments, gap_ms=29 * MIN)) == 1
    assert merge_segments([], gap_ms=MIN) == []


def test_trim_drops_the_oldest_chunks_until_it_fits(store):
    for i in range(12):
        write(store, i)
    victims = store.trim()
    assert minutes(victims) == [0, 1]
    assert store.total_bytes() == 10 * KB


def test_trim_takes_speaker_tracks_with_their_mix(store):
    for i in range(6):
        write(store, i, size=KB)
        write(store, i, size=KB, track="777")
    # 12 KiB over a 10 KiB budget: the oldest group (mix + speaker) has to go
    victims = store.trim()
    assert [(c.start_ms // MIN, c.track) for c in victims] == [(0, "777"), (0, MIX_TRACK)]
    assert store.speakers(1, 2) == [777]
    # no speaker track is ever left without the mix covering the same moment
    mixes = {c.start_ms for c in store.list_chunks(1, 2, track=MIX_TRACK)}
    tracks = {c.start_ms for c in store.list_chunks(1, 2, track="777")}
    assert tracks <= mixes


def test_trim_is_global_across_channels(store):
    write(store, 0, size=6 * KB, channel=2)
    write(store, 5, size=6 * KB, channel=3)
    victims = store.trim()
    assert [c.path.parent.name for c in victims] == ["2"]
    assert store.total_bytes() == 6 * KB


def test_trim_never_empties_the_buffer(store):
    write(store, 0, size=50 * KB)
    assert store.trim() == []
    assert store.total_bytes() == 50 * KB


def test_plan_trim_is_a_pure_function(tmp_path):
    store = ChunkStore(tmp_path, max_bytes=1)
    kept = [write(store, i) for i in range(3)]
    victims = plan_trim(kept, max_bytes=2 * KB)
    assert minutes(victims) == [0]
    assert all(c.path.exists() for c in kept)  # planning deletes nothing


def test_store_roundtrip(store):
    for i in range(3):
        write(store, i)
    assert store.total_duration_ms(1, 2) == 3 * MIN
    assert store.known_channels() == [(1, 2)]
    assert store.total_bytes(1, 2) == 3 * KB


def test_listing_separates_the_tracks(store):
    write(store, 0)
    write(store, 0, track="12")
    write(store, 0, track="34")
    assert len(store.list_chunks(1, 2)) == 1  # mix by default
    assert len(store.list_chunks(1, 2, track=None)) == 3
    assert len(store.list_chunks(1, 2, track="12")) == 1
    assert store.speakers(1, 2) == [12, 34]
    # duration counts the timeline once, not once per track
    assert store.total_duration_ms(1, 2) == MIN


def test_commit_discards_an_empty_chunk(store):
    part = store.part_path(1, 2, 0, MIX_TRACK)
    part.write_bytes(b"")
    assert store.commit(part, start_ms=0, duration_ms=1000, segment_ms=0, track=MIX_TRACK) is None
    assert not part.exists()

    part = store.part_path(1, 2, 1, MIX_TRACK)
    part.write_bytes(b"data")
    assert store.commit(part, start_ms=1, duration_ms=0, segment_ms=0, track=MIX_TRACK) is None
    assert not part.exists()


def test_window_selection(store):
    for i in range(5):
        write(store, i)
    assert minutes(store.list_chunks(1, 2, since_ms=int(2.5 * MIN))) == [2, 3, 4]
    assert minutes(store.list_chunks(1, 2, until_ms=int(2.5 * MIN))) == [0, 1, 2]


def test_purge_and_cleanup_parts(store):
    write(store, 0)
    write(store, 0, track="9")
    stale = store.part_path(1, 2, 999, MIX_TRACK)
    stale.write_bytes(b"leftover")

    assert store.cleanup_parts() == 1
    assert not stale.exists()
    assert store.purge(1, 2) == 2
    assert store.list_chunks(1, 2, track=None) == []

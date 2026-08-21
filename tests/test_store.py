from pathlib import Path

from dsbot.store import Chunk, ChunkStore, group_segments, plan_trim

MIN = 60_000


def make(start_min, dur_ms=MIN, segment_min=None):
    start = start_min * MIN
    segment = start if segment_min is None else segment_min * MIN
    return Chunk(start, segment, dur_ms, Path(Chunk.filename(start, dur_ms, segment)))


def minutes(chunks):
    return [c.start_ms // MIN for c in chunks]


def test_filename_roundtrip():
    name = Chunk.filename(1700000000000, 60000, 1699999000000)
    parsed = Chunk.parse(Path(name))
    assert parsed == Chunk(1700000000000, 1699999000000, 60000, Path(name))
    assert Chunk.parse(Path("not-a-chunk.mp3")) is None
    assert Chunk.parse(Path("c1_d2.part")) is None


def test_group_segments():
    chunks = [make(i, segment_min=0) for i in range(3)] + [make(i, segment_min=10) for i in (10, 11)]
    segments = group_segments(chunks)
    assert [s.segment_ms // MIN for s in segments] == [0, 10]
    assert segments[0].duration_ms == 3 * MIN
    assert segments[1].start_ms // MIN == 10 and segments[1].end_ms // MIN == 12


def test_oldest_chunk_overshoots_by_at_most_one_chunk():
    chunks = [make(i, segment_min=0) for i in range(10)]
    victims = plan_trim(chunks, retention_ms=5 * MIN)
    assert minutes(victims) == [0, 1, 2, 3, 4]
    kept = sum(c.duration_ms for c in chunks if c not in victims)
    assert kept == 5 * MIN


def test_oldest_segment_never_leaves_half_a_conversation():
    chunks = (
        [make(i, segment_min=0) for i in range(3)]
        + [make(i, segment_min=10) for i in range(10, 14)]
        + [make(i, segment_min=20) for i in range(20, 23)]
    )
    victims = plan_trim(chunks, retention_ms=5 * MIN, strategy="oldest-segment")
    assert minutes(victims) == [0, 1, 2, 10, 11, 12, 13]
    # what remains is exactly one whole conversation
    assert {c.segment_ms for c in chunks if c not in victims} == {20 * MIN}


def test_oldest_segment_falls_back_to_chunks_for_one_long_meeting():
    chunks = [make(i, segment_min=0) for i in range(10)]
    victims = plan_trim(chunks, retention_ms=5 * MIN, strategy="oldest-segment")
    assert minutes(victims) == [0, 1, 2, 3, 4]


def test_high_water_waits_before_trimming_then_goes_low():
    chunks = [make(i, segment_min=0) for i in range(12)]
    assert plan_trim(
        chunks, retention_ms=10 * MIN, strategy="high-water",
        high_water_ms=3 * MIN, low_water_ms=2 * MIN,
    ) == []
    victims = plan_trim(
        chunks, retention_ms=10 * MIN, strategy="high-water",
        high_water_ms=1 * MIN, low_water_ms=2 * MIN,
    )
    assert minutes(victims) == [0, 1, 2, 3]  # down to 8 minutes


def test_trim_never_empties_the_buffer():
    chunks = [make(0, dur_ms=10 * MIN, segment_min=0)]
    assert plan_trim(chunks, retention_ms=MIN) == []


def test_unknown_strategy_is_rejected():
    try:
        plan_trim([], retention_ms=1, strategy="nope")
    except ValueError as exc:
        assert "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_store_roundtrip_and_trim(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=3 * MIN)
    for i in range(6):
        part = store.part_path(1, 2, i * MIN)
        part.write_bytes(b"x" * 100)
        assert store.commit(part, start_ms=i * MIN, duration_ms=MIN, segment_ms=0)
    assert store.total_duration_ms(1, 2) == 6 * MIN
    assert minutes(store.trim(1, 2)) == [0, 1, 2]
    assert store.total_duration_ms(1, 2) == 3 * MIN
    assert store.known_channels() == [(1, 2)]


def test_commit_discards_an_empty_chunk(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=MIN)
    part = store.part_path(1, 2, 0)
    part.write_bytes(b"")
    assert store.commit(part, start_ms=0, duration_ms=1000, segment_ms=0) is None
    assert not part.exists()

    part = store.part_path(1, 2, 1)
    part.write_bytes(b"data")
    assert store.commit(part, start_ms=1, duration_ms=0, segment_ms=0) is None
    assert not part.exists()


def test_window_selection(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=100 * MIN)
    for i in range(5):
        part = store.part_path(1, 2, i * MIN)
        part.write_bytes(b"x")
        store.commit(part, start_ms=i * MIN, duration_ms=MIN, segment_ms=0)
    assert minutes(store.list_chunks(1, 2, since_ms=int(2.5 * MIN))) == [2, 3, 4]
    assert minutes(store.list_chunks(1, 2, until_ms=int(2.5 * MIN))) == [0, 1, 2]


def test_purge_and_cleanup_parts(tmp_path):
    store = ChunkStore(tmp_path, retention_ms=MIN)
    part = store.part_path(1, 2, 0)
    part.write_bytes(b"x")
    store.commit(part, start_ms=0, duration_ms=MIN, segment_ms=0)
    stale = store.part_path(1, 2, 999)
    stale.write_bytes(b"leftover")

    assert store.cleanup_parts() == 1
    assert not stale.exists()
    assert store.purge(1, 2) == 1
    assert store.list_chunks(1, 2) == []

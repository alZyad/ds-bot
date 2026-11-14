from pathlib import Path

import pytest

from dsbot.store import MIX_TRACK, Chunk, ChunkStore, plan_trim

MIN = 60_000
KB = 1024


@pytest.fixture
def store(tmp_path):
    return ChunkStore(tmp_path, max_bytes=10 * KB)


def write(store, minute, *, size=KB, session=0, track=MIX_TRACK, guild=1):
    start = minute * MIN
    part = store.part_path(guild, session, start, track)
    part.write_bytes(b"x" * size)
    chunk = store.commit(
        part, start_ms=start, duration_ms=MIN, segment_ms=start, track=track
    )
    assert chunk is not None
    return chunk


def test_filename_roundtrip():
    name = Chunk.filename(1700000000000, 60000, 1699999000000, MIX_TRACK)
    parsed = Chunk.parse(Path(name))
    assert parsed == Chunk(1700000000000, 1699999000000, 60000, MIX_TRACK, Path(name))
    assert parsed.is_mix and parsed.user_id is None

    speaker = Chunk.parse(Path(Chunk.filename(1, 2, 3, "424242")))
    assert not speaker.is_mix and speaker.user_id == 424242

    assert Chunk.parse(Path("not-a-chunk.aac")) is None
    assert Chunk.parse(Path("c1_d2_s3_tmix.mp3")) is None
    assert Chunk.parse(Path("c1_d2_s3_tbogus.aac")) is None
    assert Chunk.parse(Path("c1_t mix.part")) is None


def test_chunks_land_in_their_session_directory(store, tmp_path):
    chunk = write(store, 3, session=1000)
    assert chunk.path.parent == tmp_path / "1" / "1000"


def test_a_session_summarises_its_chunks(store):
    write(store, 0, session=5)
    write(store, 1, session=5)
    write(store, 1, session=5, track="42")
    session = store.session(1, 5)
    assert session.duration_ms == 2 * MIN
    assert session.end_ms == 2 * MIN
    assert session.size_bytes == 3 * KB
    assert session.speakers == [42]
    assert len(session.mix) == 2
    assert store.session(1, 6) is None


def test_sessions_are_listed_oldest_first(store):
    write(store, 0, session=20)
    write(store, 0, session=10)
    write(store, 0, session=30, guild=2)
    assert [s.session_ms for s in store.sessions()] == [10, 20, 30]
    assert [s.session_ms for s in store.sessions(1)] == [10, 20]
    assert store.latest_session(1).session_ms == 20
    assert store.latest_session(3) is None


def test_listing_separates_the_tracks(store):
    write(store, 0)
    write(store, 0, track="12")
    write(store, 0, track="34")
    assert len(store.list_chunks(1, 0)) == 1
    assert len(store.list_chunks(1, 0, track=None)) == 3
    assert len(store.list_chunks(1, 0, track="12")) == 1


def test_people_are_remembered_once_each(store):
    store.add_person(1, 5, 42)
    store.add_person(1, 5, 7)
    store.add_person(1, 5, 42)
    assert store.people(1, 5) == [42, 7]
    assert store.people(1, 6) == []


def test_trim_deletes_the_oldest_whole_session(store):
    for session in (1, 2, 3):
        for minute in range(4):
            write(store, minute, session=session)
    victims = store.trim()
    assert [s.session_ms for s in victims] == [1]
    assert [s.session_ms for s in store.sessions()] == [2, 3]
    assert store.total_bytes() == 8 * KB


def test_trim_keeps_the_newest_session_of_each_guild(store):
    write(store, 0, size=20 * KB, session=1, guild=1)
    write(store, 0, size=20 * KB, session=2, guild=2)
    assert store.trim() == []


def test_plan_trim_is_a_pure_function(store):
    for session in (1, 2, 3):
        write(store, 0, session=session)
    victims = plan_trim(store.sessions(), max_bytes=2 * KB)
    assert [s.session_ms for s in victims] == [1]
    assert len(store.sessions()) == 3


def test_delete_session(store):
    write(store, 0, session=1)
    assert store.delete_session(1, 1) is True
    assert store.sessions() == []
    assert store.delete_session(1, 1) is False


def test_commit_discards_an_empty_chunk(store):
    part = store.part_path(1, 2, 0, MIX_TRACK)
    part.write_bytes(b"")
    assert store.commit(part, start_ms=0, duration_ms=1000, segment_ms=0, track=MIX_TRACK) is None
    assert not part.exists()

    part = store.part_path(1, 2, 1, MIX_TRACK)
    part.write_bytes(b"data")
    assert store.commit(part, start_ms=1, duration_ms=0, segment_ms=0, track=MIX_TRACK) is None
    assert not part.exists()


def test_cleanup_parts(store):
    write(store, 0)
    stale = store.part_path(1, 0, 999, MIX_TRACK)
    stale.write_bytes(b"leftover")
    assert store.cleanup_parts() == 1
    assert not stale.exists()
    assert len(store.list_chunks(1, 0)) == 1

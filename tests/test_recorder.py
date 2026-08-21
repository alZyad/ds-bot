"""End-to-end tests of the segment/chunk/track state machine.

The pump is driven by hand (one ``_handle_frame`` per simulated 20 ms tick) so
the tests are deterministic and do not depend on wall-clock timing.
"""

import struct

import pytest

from dsbot.recorder import ChannelRecorder
from dsbot.store import MIX_TRACK, ChunkStore

SAMPLES = 960  # 20 ms of mono 48 kHz


def loud(value=6000):
    return struct.pack(f"<{SAMPLES * 2}h", *([value] * SAMPLES * 2))  # stereo in


@pytest.fixture
def recorder(cfg):
    store = ChunkStore(cfg.data_dir / "recordings", max_bytes=cfg.max_disk_bytes)
    return ChannelRecorder(cfg, store, guild_id=1, channel_id=2)


async def tick(recorder, *, speakers=(), value=6000):
    for user_id in speakers:
        recorder.mixer.submit(user_id, loud(value))
    await recorder._handle_frame()


def chunks(recorder, track=MIX_TRACK):
    return recorder.store.list_chunks(1, 2, track=track)


# -- silence and segments ---------------------------------------------------

async def test_nothing_is_recorded_while_nobody_talks(recorder):
    for _ in range(50):
        await tick(recorder)
    assert not recorder.recording
    assert chunks(recorder) == []


async def test_a_segment_opens_on_the_first_voiced_frame(recorder):
    await tick(recorder, speakers=[1])
    assert recorder.recording
    assert recorder.stats.segments == 1
    await recorder.stop()
    assert len(chunks(recorder)) == 1


async def test_silence_shorter_than_the_timeout_keeps_the_segment_open(recorder):
    # silence_timeout is 0.1s in the test config, i.e. 5 frames
    await tick(recorder, speakers=[1])
    for _ in range(4):
        await tick(recorder)
    assert recorder.recording
    await tick(recorder, speakers=[1])
    assert recorder.stats.segments == 1


async def test_a_short_pause_is_replayed_not_dropped(recorder):
    await tick(recorder, speakers=[1])
    for _ in range(4):
        await tick(recorder)          # buffered, not yet written
    assert recorder.stats.frames_written == 1
    await tick(recorder, speakers=[1])
    # the four held frames plus the new one
    assert recorder.stats.frames_written == 6


async def test_silence_past_the_timeout_ends_the_segment_and_is_discarded(recorder):
    await tick(recorder, speakers=[1])
    for _ in range(5):
        await tick(recorder)
    assert not recorder.recording
    assert recorder.stats.frames_written == 1  # the trailing silence is not stored
    assert len(chunks(recorder)) == 1


async def test_talking_again_starts_a_second_segment(recorder):
    await tick(recorder, speakers=[1])
    for _ in range(5):
        await tick(recorder)
    await tick(recorder, speakers=[1])
    assert recorder.stats.segments == 2
    await recorder.stop()
    assert len({c.segment_ms for c in chunks(recorder)}) == 2


async def test_quiet_audio_does_not_count_as_speech(recorder):
    for _ in range(10):
        await tick(recorder, speakers=[1], value=10)  # below silence_rms=100
    assert not recorder.recording
    assert chunks(recorder) == []


async def test_a_custom_gate_overrides_the_config(cfg):
    store = ChunkStore(cfg.data_dir / "recordings", max_bytes=cfg.max_disk_bytes)
    recorder = ChannelRecorder(
        cfg, store, guild_id=1, channel_id=2, silence_rms=10_000
    )
    for _ in range(5):
        await tick(recorder, speakers=[1], value=6000)  # loud enough by default
    assert not recorder.recording


# -- chunks -----------------------------------------------------------------

async def test_chunks_rotate_while_a_segment_continues(recorder):
    # chunk_seconds is 0.2s in the test config, i.e. 10 frames
    for _ in range(25):
        await tick(recorder, speakers=[1])
    assert len(chunks(recorder)) == 2  # two closed, one still open
    assert recorder.recording
    await recorder.stop()
    assert len(chunks(recorder)) == 3
    assert len({c.segment_ms for c in chunks(recorder)}) == 1


async def test_flush_makes_the_open_chunk_visible(recorder):
    for _ in range(5):
        await tick(recorder, speakers=[1])
    assert chunks(recorder) == []
    await recorder.flush()
    assert len(chunks(recorder)) == 1
    assert recorder.recording  # flushing does not end the conversation


async def test_stop_is_idempotent_and_closes_the_open_chunk(recorder):
    await recorder.start()
    for _ in range(3):
        await tick(recorder, speakers=[1])
    await recorder.stop()
    assert len(chunks(recorder)) == 1
    await recorder.stop()
    assert len(chunks(recorder)) == 1


async def test_the_buffer_is_trimmed_to_the_disk_budget(cfg):
    # Through the fake encoder a 10-frame chunk is 10 * 1920 bytes per track,
    # and one speaker means two tracks, so a group is 38400 bytes. A 100 KiB
    # budget therefore holds two groups and no more.
    store = ChunkStore(cfg.data_dir / "recordings", max_bytes=100_000)
    recorder = ChannelRecorder(cfg, store, guild_id=1, channel_id=2)
    for _ in range(40):
        await tick(recorder, speakers=[1])
    await recorder.stop()
    assert store.total_bytes() <= 100_000
    assert recorder.stats.trimmed_chunks > 0
    # the mix and the speaker track were trimmed together, never one alone
    assert len(chunks(recorder)) == len(chunks(recorder, "1"))


async def test_a_group_bigger_than_the_budget_is_still_kept(cfg):
    """The buffer degrades to holding one group rather than to holding nothing."""
    store = ChunkStore(cfg.data_dir / "recordings", max_bytes=4000)
    recorder = ChannelRecorder(cfg, store, guild_id=1, channel_id=2)
    for _ in range(40):
        await tick(recorder, speakers=[1])
    await recorder.stop()
    starts = {c.start_ms for c in store.list_chunks(1, 2, track=None)}
    assert len(starts) == 1
    assert store.total_bytes() > 4000


# -- per-speaker tracks -----------------------------------------------------

async def test_each_speaker_gets_an_aligned_track(recorder):
    for _ in range(5):
        await tick(recorder, speakers=[1, 2])
    await recorder.stop()

    assert recorder.store.speakers(1, 2) == [1, 2]
    mix = chunks(recorder)[0]
    for user_id in (1, 2):
        track = chunks(recorder, str(user_id))[0]
        assert track.start_ms == mix.start_ms
        assert track.duration_ms == mix.duration_ms
        assert track.segment_ms == mix.segment_ms


async def test_a_late_speaker_is_padded_back_to_the_chunk_start(recorder):
    for _ in range(4):
        await tick(recorder, speakers=[1])
    for _ in range(4):
        await tick(recorder, speakers=[1, 2])  # user 2 arrives mid-chunk
    await recorder.stop()

    mix = chunks(recorder)[0]
    late = chunks(recorder, "2")[0]
    # the late track covers the whole chunk, so it still lines up with the mix
    assert late.duration_ms == mix.duration_ms
    assert late.size_bytes == mix.size_bytes


async def test_too_many_speakers_keeps_the_mix_and_the_partial_tracks(recorder):
    # max_speaker_tracks is 2 in the test config
    for _ in range(3):
        await tick(recorder, speakers=[1, 2])
    assert recorder._tracks_enabled
    for _ in range(3):
        await tick(recorder, speakers=[1, 2, 3])  # a third voice tips it over
    assert not recorder._tracks_enabled
    await recorder.stop()

    assert recorder.stats.tracks_given_up == 1
    assert len(chunks(recorder)) >= 1              # the mix is always kept
    assert recorder.store.speakers(1, 2) == [1, 2]  # what was captured is kept
    assert chunks(recorder, "3") == []              # the newcomer gets no track


async def test_crossing_the_limit_mid_chunk_keeps_the_tracks_aligned(recorder):
    """A short track would drift against the mix once segments are merged.

    Closing the speaker tracks the moment the limit is hit leaves them shorter
    than the mix for that chunk. Export can merge two segments into one file, so
    a short chunk in the middle shifts every later frame of that speaker earlier
    than the mix. Freezing them with silence to the end of the chunk is what
    prevents it.
    """
    # chunk_frames is 10, max_speaker_tracks is 2 in the test config
    for _ in range(4):
        await tick(recorder, speakers=[1, 2])
    for _ in range(6):
        await tick(recorder, speakers=[1, 2, 3])  # tips over mid-chunk
    await recorder.stop()

    for chunk in chunks(recorder):
        group = [
            c for c in recorder.store.list_chunks(1, 2, track=None)
            if c.start_ms == chunk.start_ms
        ]
        assert {c.duration_ms for c in group} == {chunk.duration_ms}
    assert recorder.store.speakers(1, 2) == [1, 2]  # the newcomer still gets none


async def test_tracks_are_reconsidered_for_each_conversation(recorder):
    for _ in range(3):
        await tick(recorder, speakers=[1, 2, 3])
    assert not recorder._tracks_enabled
    for _ in range(5):  # end the segment
        await tick(recorder)
    await tick(recorder, speakers=[1])  # a new, quieter conversation
    assert recorder._tracks_enabled


async def test_tracks_can_be_switched_off_entirely(cfg):
    from dataclasses import replace
    cfg = replace(cfg, max_speaker_tracks=0)
    store = ChunkStore(cfg.data_dir / "recordings", max_bytes=cfg.max_disk_bytes)
    recorder = ChannelRecorder(cfg, store, guild_id=1, channel_id=2)
    for _ in range(5):
        await tick(recorder, speakers=[1, 2])
    await recorder.stop()
    assert len(chunks(recorder)) == 1
    assert store.speakers(1, 2) == []


async def test_speakers_are_reported_once_each(recorder):
    seen = []
    recorder.on_speaker = seen.append
    for _ in range(5):
        await tick(recorder, speakers=[1, 2])
    assert sorted(seen) == [1, 2]


# -- pump and introspection -------------------------------------------------

async def test_the_pump_runs_on_a_real_clock(recorder):
    await recorder.start()
    import asyncio
    await asyncio.sleep(0.1)
    await recorder.stop()
    assert recorder.stats.frames_seen >= 3  # ~5 frames in 100ms


async def test_describe_reports_the_buffer(recorder):
    for _ in range(12):
        await tick(recorder, speakers=[1])
    info = recorder.describe()
    assert info["channel_id"] == 2
    assert info["in_segment"] is True
    assert info["segments"] == 1
    assert info["silence_rms"] == 100
    assert info["last_rms"] > 0
    assert info["tracks"] is True
    assert info["buffered_ms"] > 0

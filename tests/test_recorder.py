"""End-to-end tests of the segment/chunk state machine.

The pump is driven by hand (one ``_handle_frame`` per simulated 20 ms tick) so
the tests are deterministic and do not depend on wall-clock timing.
"""

import struct

import pytest

from dsbot.recorder import ChannelRecorder
from dsbot.store import ChunkStore

SAMPLES = 960  # 20 ms of mono 48 kHz


def loud(value=6000):
    return struct.pack(f"<{SAMPLES * 2}h", *([value] * SAMPLES * 2))  # stereo in


@pytest.fixture
def recorder(cfg):
    store = ChunkStore(
        cfg.data_dir / "recordings",
        retention_ms=cfg.retention_ms,
        strategy=cfg.retention_strategy,
    )
    return ChannelRecorder(cfg, store, guild_id=1, channel_id=2)


async def tick(recorder, *, speakers=(), value=6000):
    for user_id in speakers:
        recorder.mixer.submit(user_id, loud(value))
    await recorder._handle_frame()


def chunks(recorder):
    return recorder.store.list_chunks(1, 2)


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
    # the short pause was replayed into the recording, not dropped
    assert recorder.stats.frames_written == 6
    await recorder.stop()
    assert len({c.segment_ms for c in chunks(recorder)}) == 1


async def test_silence_longer_than_the_timeout_closes_the_segment(recorder):
    await tick(recorder, speakers=[1])
    for _ in range(5):
        await tick(recorder)
    assert not recorder.recording
    assert recorder.stats.frames_written == 1  # trailing silence is discarded
    assert len(chunks(recorder)) == 1

    await tick(recorder, speakers=[1])
    assert recorder.stats.segments == 2
    await recorder.stop()
    assert len({c.segment_ms for c in chunks(recorder)}) == 2


async def test_resuming_after_a_long_silence_starts_a_new_segment_immediately(recorder):
    await tick(recorder, speakers=[1])
    for _ in range(200):  # 4 seconds of silence, well past the timeout
        await tick(recorder)
    await tick(recorder, speakers=[2])
    assert recorder.recording
    assert recorder.stats.segments == 2
    assert recorder.stats.frames_written == 2  # only the two voiced frames


async def test_quiet_frames_count_as_silence(recorder):
    # a packet arrives, but below the RMS gate (cfg.silence_rms == 100)
    await tick(recorder, speakers=[1], value=10)
    assert not recorder.recording
    assert recorder.stats.frames_written == 0


async def test_everybody_is_mixed_into_one_timeline(recorder):
    await tick(recorder, speakers=[1, 2, 3])
    await recorder.stop()
    written = chunks(recorder)
    assert len(written) == 1
    # one frame of mono audio, whatever the number of speakers
    assert written[0].path.stat().st_size == SAMPLES * 2
    assert written[0].duration_ms == 20


async def test_chunks_rotate_inside_a_long_conversation(recorder):
    # chunk_seconds is 0.2s in the test config, i.e. 10 frames
    for _ in range(25):
        await tick(recorder, speakers=[1])
    written = chunks(recorder)
    assert len(written) == 2
    assert all(c.duration_ms == 200 for c in written)
    # same conversation, so all chunks share a segment id
    assert len({c.segment_ms for c in written}) == 1
    # ...and they are gapless: each chunk starts where the previous one is due
    assert recorder.recording


async def test_flush_makes_the_live_chunk_available(recorder):
    for _ in range(3):
        await tick(recorder, speakers=[1])
    assert chunks(recorder) == []
    await recorder.flush()
    assert len(chunks(recorder)) == 1
    assert chunks(recorder)[0].duration_ms == 60
    # the conversation carries on in a new chunk
    assert recorder.recording
    await tick(recorder, speakers=[1])
    await recorder.stop()
    assert sum(c.duration_ms for c in chunks(recorder)) == 80


async def test_the_buffer_is_trimmed_to_the_retention_target(recorder):
    # retention is 1s, chunks are 0.2s -> at most 5 chunks survive
    for _ in range(200):  # 4 seconds of continuous speech
        await tick(recorder, speakers=[1])
    await recorder.stop()
    written = chunks(recorder)
    total = sum(c.duration_ms for c in written)
    assert total <= recorder.cfg.retention_ms
    assert total >= recorder.cfg.retention_ms - 200  # never more than one chunk short
    # what survives is the *newest* audio
    assert written[-1].end_ms == max(c.end_ms for c in written)
    assert recorder.stats.trimmed_chunks > 0


async def test_stop_is_idempotent_and_closes_the_open_chunk(recorder):
    recorder._running = True
    await tick(recorder, speakers=[1])
    await recorder.stop()
    await recorder.stop()
    assert not recorder.recording
    assert len(chunks(recorder)) == 1


async def test_the_pump_runs_on_a_real_clock(cfg):
    import asyncio

    store = ChunkStore(cfg.data_dir / "recordings", retention_ms=cfg.retention_ms)
    rec = ChannelRecorder(cfg, store, guild_id=1, channel_id=3)
    await rec.start()
    try:
        deadline = asyncio.get_running_loop().time() + 0.35
        while asyncio.get_running_loop().time() < deadline:
            rec.feed(1, loud())
            await asyncio.sleep(0.02)
    finally:
        await rec.stop()
    written = store.list_chunks(1, 3)
    assert written, "the pump should have produced at least one chunk"
    assert 0 < sum(c.duration_ms for c in written) <= 400
    assert rec.stats.frames_seen >= 10


async def test_describe_reports_the_buffer(recorder):
    await tick(recorder, speakers=[1])
    await recorder.stop()
    info = recorder.describe()
    assert info["channel_id"] == 2
    assert info["segments"] == 1
    assert info["buffered_ms"] == 20
    assert info["running"] is False

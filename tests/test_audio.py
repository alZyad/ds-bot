import struct

from dsbot.audio import Mixer, add, rms, to_mono


def frame(value, samples=1920):
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_to_mono_halves_the_payload():
    stereo = struct.pack("<4h", 100, 200, 300, 400)
    mono = to_mono(stereo)
    assert len(mono) == len(stereo) // 2
    assert struct.unpack("<2h", mono) == (150, 350)


def test_add_clips_instead_of_wrapping():
    a = struct.pack("<2h", 30000, -30000)
    b = struct.pack("<2h", 30000, -30000)
    assert struct.unpack("<2h", add(a, b)) == (32767, -32768)


def test_rms():
    assert rms(b"") == 0
    assert rms(struct.pack("<4h", 0, 0, 0, 0)) == 0
    assert rms(struct.pack("<4h", 1000, -1000, 1000, -1000)) == 1000


def test_pull_without_input_is_silence():
    mixer = Mixer()
    result = mixer.pull()
    assert result.silent
    assert result.rms == 0
    assert result.pcm == bytes(mixer.frame_size_out)


def test_pull_mixes_every_speaker():
    mixer = Mixer()
    mixer.submit(1, frame(4000, 1920))
    mixer.submit(2, frame(4000, 1920))
    result = mixer.pull()
    assert result.speakers == frozenset({1, 2})
    assert len(result.pcm) == mixer.frame_size_out
    assert result.rms == 8000  # the two speakers sum


def test_pull_pads_a_partial_packet():
    mixer = Mixer()
    mixer.submit(1, frame(4000, 960))  # half a frame
    result = mixer.pull()
    assert len(result.pcm) == mixer.frame_size_out
    assert result.speakers == frozenset({1})


def test_buffers_are_bounded_and_drop_the_oldest_audio():
    mixer = Mixer(max_buffer_ms=40)  # 2 frames
    for _ in range(10):
        mixer.submit(1, frame(1000, 1920))
    assert mixer.dropped_bytes == 8 * mixer.frame_size_in
    mixer.pull()
    mixer.pull()
    assert mixer.pull().silent


def test_reset_forgets_everyone():
    mixer = Mixer()
    mixer.submit(1, frame(4000, 1920))
    mixer.reset()
    assert mixer.pull().silent

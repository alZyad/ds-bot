import struct

from dsbot.audio import FULL_SCALE, Limiter, Mixer, add, mix, peak, rms, to_mono


def frame(value, samples=1920):
    return struct.pack(f"<{samples}h", *([value] * samples))


def mono(value, samples=960):
    return struct.pack(f"<{samples}h", *([value] * samples))


def test_to_mono_halves_the_payload():
    stereo = struct.pack("<4h", 100, 200, 300, 400)
    result = to_mono(stereo)
    assert len(result) == len(stereo) // 2
    assert struct.unpack("<2h", result) == (150, 350)


def test_add_clips_instead_of_wrapping():
    a = struct.pack("<2h", 30000, -30000)
    b = struct.pack("<2h", 30000, -30000)
    assert struct.unpack("<2h", add(a, b)) == (32767, -32768)


def test_peak_and_rms():
    assert peak(b"") == 0 and rms(b"") == 0
    assert peak(struct.pack("<3h", -5000, 100, 4000)) == 5000
    assert rms(struct.pack("<4h", 1000, -1000, 1000, -1000)) == 1000


def test_mix_leaves_quiet_audio_untouched():
    limiter = Limiter(frame_ms=20)
    result = mix([mono(1000), mono(2000)], limiter)
    assert peak(result) == 3000
    assert limiter.gain == 1.0
    assert limiter.limited_frames == 0


def test_mix_limits_instead_of_clipping():
    limiter = Limiter(frame_ms=20)
    # Summed, these two would be 60000: way past what 16 bits can hold. A plain
    # sum would square the peaks off; the limiter has to scale them down.
    result = mix([mono(30000), mono(30000)], limiter)
    assert peak(result) <= FULL_SCALE
    assert limiter.gain < 1.0
    assert limiter.limited_frames == 1
    # and it is attenuation, not clipping: every sample has the same value
    samples = struct.unpack(f"<{len(result) // 2}h", result)
    assert len(set(samples)) == 1


def test_the_limiter_releases_back_to_unity():
    limiter = Limiter(frame_ms=20, release_ms=200.0)
    mix([mono(30000), mono(30000)], limiter)
    assert limiter.gain < 1.0
    for _ in range(20):  # 400ms of quiet is plenty for a 200ms release
        mix([mono(500)], limiter)
    assert limiter.gain == 1.0


def test_the_limiter_recovers_gradually_not_instantly():
    limiter = Limiter(frame_ms=20, release_ms=200.0)
    mix([mono(30000), mono(30000)], limiter)
    ducked = limiter.gain
    mix([mono(500)], limiter)
    assert ducked < limiter.gain < 1.0


def test_mix_of_nothing_is_nothing():
    assert mix([], Limiter()) == b""


def test_pull_without_input_is_silence():
    mixer = Mixer()
    result = mixer.pull()
    assert result.silent
    assert result.rms == 0
    assert result.pcm == bytes(mixer.frame_size_out)
    assert result.tracks == {}


def test_pull_mixes_and_keeps_every_speaker_separately():
    mixer = Mixer()
    mixer.submit(1, frame(4000))
    mixer.submit(2, frame(4000))
    result = mixer.pull()

    assert result.speakers == frozenset({1, 2})
    assert result.rms == 8000  # the two speakers sum in the mix
    assert len(result.pcm) == mixer.frame_size_out
    # the isolated tracks are the individual voices, not the mix
    assert set(result.tracks) == {1, 2}
    assert all(len(t) == mixer.frame_size_out for t in result.tracks.values())
    assert rms(result.tracks[1]) == 4000


def test_isolated_tracks_are_never_limited():
    mixer = Mixer()
    mixer.submit(1, frame(30000))
    mixer.submit(2, frame(30000))
    result = mixer.pull()
    # the mix had to duck to fit, but each speaker's own track is intact
    assert peak(result.pcm) <= FULL_SCALE
    assert peak(result.tracks[1]) == 30000
    assert peak(result.tracks[2]) == 30000


def test_pull_pads_a_partial_packet():
    mixer = Mixer()
    mixer.submit(1, frame(4000, 960))  # half a frame
    result = mixer.pull()
    assert len(result.pcm) == mixer.frame_size_out
    assert len(result.tracks[1]) == mixer.frame_size_out
    assert result.speakers == frozenset({1})


def test_buffers_are_bounded_and_drop_the_oldest_audio():
    mixer = Mixer(max_buffer_ms=40)  # 2 frames
    for _ in range(10):
        mixer.submit(1, frame(1000))
    assert mixer.dropped_bytes == 8 * mixer.frame_size_in
    mixer.pull()
    mixer.pull()
    assert mixer.pull().silent


def test_reset_forgets_everyone_and_the_limiter():
    mixer = Mixer()
    mixer.submit(1, frame(30000))
    mixer.submit(2, frame(30000))
    mixer.pull()
    assert mixer.limiter.gain < 1.0
    mixer.reset()
    assert mixer.pull().silent
    assert mixer.limiter.gain == 1.0

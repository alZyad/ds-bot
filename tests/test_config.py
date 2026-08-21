import pytest

from dsbot.config import Config


def test_derived_values():
    cfg = Config(silence_timeout=5.0, chunk_seconds=60.0, frame_ms=20, max_disk_mb=1024.0)
    assert cfg.silence_frames == 250   # 5s / 20ms
    assert cfg.chunk_frames == 3000    # 60s / 20ms
    assert cfg.max_disk_bytes == 1024 * 1024 * 1024
    assert cfg.merge_gap_ms == 120_000


@pytest.mark.parametrize("kwargs", [
    {"min_speakers": 0},
    {"chunk_seconds": 0},
    {"max_disk_mb": 0},
    {"max_speaker_tracks": -1},
    {"merge_gap_seconds": -1},
    {"silence_rms": -1},
    {"silence_rms": 40000},
    {"leave_grace": -1},
    {"frame_ms": 15},
])
def test_validate_rejects_nonsense(kwargs):
    with pytest.raises(ValueError):
        Config(**kwargs).validate()


def test_channel_filters():
    assert Config().channel_allowed(1)
    assert not Config(exclude_channel_ids=frozenset({1})).channel_allowed(1)
    include = Config(include_channel_ids=frozenset({1}))
    assert include.channel_allowed(1) and not include.channel_allowed(2)


def test_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DISCORD_TOKEN", " tok ")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MIN_SPEAKERS", "3")
    monkeypatch.setenv("MAX_DISK_MB", "512")
    monkeypatch.setenv("MAX_SPEAKER_TRACKS", "4")
    monkeypatch.setenv("AUDIO_BITRATE", "96k")
    monkeypatch.setenv("LEAVE_GRACE", "7.5")
    monkeypatch.setenv("MERGE_GAP_SECONDS", "60")
    monkeypatch.setenv("ANNOUNCE", "no")
    monkeypatch.setenv("EXCLUDE_CHANNEL_IDS", "12, 34")
    cfg = Config.from_env()
    assert cfg.token == "tok"
    assert cfg.min_speakers == 3
    assert cfg.max_disk_mb == 512
    assert cfg.max_speaker_tracks == 4
    assert cfg.audio_bitrate == "96k"
    assert cfg.leave_grace == 7.5
    assert cfg.merge_gap_seconds == 60
    assert cfg.announce is False
    assert cfg.exclude_channel_ids == frozenset({12, 34})


def test_from_env_validates(monkeypatch):
    monkeypatch.setenv("MAX_DISK_MB", "0")
    with pytest.raises(ValueError):
        Config.from_env()

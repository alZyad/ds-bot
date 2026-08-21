import pytest

from dsbot.config import Config


def test_derived_frame_counts():
    cfg = Config(silence_timeout=5.0, chunk_seconds=60.0, frame_ms=20)
    assert cfg.silence_frames == 250   # 5s / 20ms
    assert cfg.chunk_frames == 3000    # 60s / 20ms
    assert cfg.retention_ms == 10_800_000  # 3h


@pytest.mark.parametrize("kwargs", [
    {"retention_strategy": "nope"},
    {"min_speakers": 0},
    {"chunk_seconds": 0},
    {"retention_seconds": 10, "chunk_seconds": 60},
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
    monkeypatch.setenv("RETENTION_SECONDS", "7200")
    monkeypatch.setenv("RETENTION_STRATEGY", "high-water")
    monkeypatch.setenv("ANNOUNCE", "no")
    monkeypatch.setenv("EXCLUDE_CHANNEL_IDS", "12, 34")
    cfg = Config.from_env()
    assert cfg.token == "tok"
    assert cfg.min_speakers == 3
    assert cfg.retention_seconds == 7200
    assert cfg.retention_strategy == "high-water"
    assert cfg.announce is False
    assert cfg.exclude_channel_ids == frozenset({12, 34})


def test_from_env_rejects_a_bad_strategy(monkeypatch):
    monkeypatch.setenv("RETENTION_STRATEGY", "wat")
    with pytest.raises(ValueError):
        Config.from_env()

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dsbot.config import Config

FAKE_FFMPEG = Path(__file__).with_name("fake_ffmpeg.py")


@pytest.fixture
def cfg(tmp_path):
    return Config(
        data_dir=tmp_path,
        min_speakers=2,
        silence_timeout=0.1,   # 5 frames at 20ms, keeps tests fast
        silence_rms=100,
        chunk_seconds=0.2,     # 10 frames
        retention_seconds=1.0,
        ffmpeg=str(FAKE_FFMPEG),
    )

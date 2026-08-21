"""Environment driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional, only used to load a local .env file
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


RETENTION_STRATEGIES = ("oldest-chunk", "oldest-segment", "high-water")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else float(raw)


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw is None or raw == "" else int(raw)


def _ids(name: str) -> frozenset[int]:
    raw = os.getenv(name) or ""
    return frozenset(int(part) for part in raw.replace(" ", "").split(",") if part)


@dataclass(frozen=True)
class Config:
    """All knobs of the bot, resolved once at startup."""

    token: str = ""
    data_dir: Path = Path("./data")

    # recording
    min_speakers: int = 2
    silence_timeout: float = 5.0
    silence_rms: int = 150
    frame_ms: int = 20
    sample_rate: int = 48000

    # rolling buffer
    retention_seconds: float = 3 * 60 * 60
    retention_strategy: str = "oldest-chunk"
    chunk_seconds: float = 60.0
    high_water_slack: float = 900.0
    low_water_slack: float = 900.0

    # output
    mp3_bitrate: str = "64k"
    ffmpeg: str = "ffmpeg"
    max_upload_mb: float = 9.0

    # behaviour
    announce: bool = True
    include_channel_ids: frozenset[int] = field(default_factory=frozenset)
    exclude_channel_ids: frozenset[int] = field(default_factory=frozenset)
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Config":  # noqa: UP037
        if load_dotenv is not None:
            load_dotenv()

        cfg = cls(
            token=(os.getenv("DISCORD_TOKEN") or "").strip(),
            data_dir=Path(os.getenv("DATA_DIR") or "./data").expanduser(),
            min_speakers=_int("MIN_SPEAKERS", 2),
            silence_timeout=_float("SILENCE_TIMEOUT", 5.0),
            silence_rms=_int("SILENCE_RMS", 150),
            frame_ms=_int("FRAME_MS", 20),
            sample_rate=_int("SAMPLE_RATE", 48000),
            retention_seconds=_float("RETENTION_SECONDS", 3 * 60 * 60),
            retention_strategy=(os.getenv("RETENTION_STRATEGY") or "oldest-chunk").strip(),
            chunk_seconds=_float("CHUNK_SECONDS", 60.0),
            high_water_slack=_float("HIGH_WATER_SLACK", 900.0),
            low_water_slack=_float("LOW_WATER_SLACK", 900.0),
            mp3_bitrate=(os.getenv("MP3_BITRATE") or "64k").strip(),
            ffmpeg=(os.getenv("FFMPEG") or "ffmpeg").strip(),
            max_upload_mb=_float("MAX_UPLOAD_MB", 9.0),
            announce=_bool("ANNOUNCE", True),
            include_channel_ids=_ids("INCLUDE_CHANNEL_IDS"),
            exclude_channel_ids=_ids("EXCLUDE_CHANNEL_IDS"),
            log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.retention_strategy not in RETENTION_STRATEGIES:
            raise ValueError(
                f"RETENTION_STRATEGY must be one of {RETENTION_STRATEGIES}, "
                f"got {self.retention_strategy!r}"
            )
        if self.min_speakers < 1:
            raise ValueError("MIN_SPEAKERS must be >= 1")
        if self.chunk_seconds <= 0:
            raise ValueError("CHUNK_SECONDS must be > 0")
        if self.retention_seconds < self.chunk_seconds:
            raise ValueError("RETENTION_SECONDS must be >= CHUNK_SECONDS")
        if self.frame_ms not in (10, 20, 40, 60):
            raise ValueError("FRAME_MS must be one of 10, 20, 40, 60")

    # -- derived ------------------------------------------------------------

    @property
    def silence_frames(self) -> int:
        """Number of consecutive silent frames that end a segment."""
        return max(1, round(self.silence_timeout * 1000 / self.frame_ms))

    @property
    def chunk_frames(self) -> int:
        return max(1, round(self.chunk_seconds * 1000 / self.frame_ms))

    @property
    def retention_ms(self) -> int:
        return int(self.retention_seconds * 1000)

    def channel_allowed(self, channel_id: int) -> bool:
        if self.include_channel_ids and channel_id not in self.include_channel_ids:
            return False
        return channel_id not in self.exclude_channel_ids

"""Environment driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional, only used to load a local .env file
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


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
    leave_grace: float = 5.0
    frame_ms: int = 20
    sample_rate: int = 48000

    # rolling buffer: one disk budget, trimmed oldest chunk first
    max_disk_mb: float = 1024.0
    chunk_seconds: float = 60.0

    # per-speaker tracks
    max_speaker_tracks: int = 6

    # output
    audio_bitrate: str = "64k"
    ffmpeg: str = "ffmpeg"
    max_upload_mb: float = 9.0
    merge_gap_seconds: float = 120.0

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
            leave_grace=_float("LEAVE_GRACE", 5.0),
            frame_ms=_int("FRAME_MS", 20),
            sample_rate=_int("SAMPLE_RATE", 48000),
            max_disk_mb=_float("MAX_DISK_MB", 1024.0),
            chunk_seconds=_float("CHUNK_SECONDS", 60.0),
            max_speaker_tracks=_int("MAX_SPEAKER_TRACKS", 6),
            audio_bitrate=(os.getenv("AUDIO_BITRATE") or "64k").strip(),
            ffmpeg=(os.getenv("FFMPEG") or "ffmpeg").strip(),
            max_upload_mb=_float("MAX_UPLOAD_MB", 9.0),
            merge_gap_seconds=_float("MERGE_GAP_SECONDS", 120.0),
            announce=_bool("ANNOUNCE", True),
            include_channel_ids=_ids("INCLUDE_CHANNEL_IDS"),
            exclude_channel_ids=_ids("EXCLUDE_CHANNEL_IDS"),
            log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.min_speakers < 1:
            raise ValueError("MIN_SPEAKERS must be >= 1")
        if self.chunk_seconds <= 0:
            raise ValueError("CHUNK_SECONDS must be > 0")
        if self.max_disk_mb <= 0:
            raise ValueError("MAX_DISK_MB must be > 0")
        if self.max_speaker_tracks < 0:
            raise ValueError("MAX_SPEAKER_TRACKS must be >= 0")
        if self.merge_gap_seconds < 0:
            raise ValueError("MERGE_GAP_SECONDS must be >= 0")
        if not 0 <= self.silence_rms <= 32767:
            raise ValueError("SILENCE_RMS must be between 0 and 32767")
        if self.leave_grace < 0:
            raise ValueError("LEAVE_GRACE must be >= 0")
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
    def max_disk_bytes(self) -> int:
        return int(self.max_disk_mb * 1024 * 1024)

    @property
    def merge_gap_ms(self) -> int:
        return int(self.merge_gap_seconds * 1000)

    def channel_allowed(self, channel_id: int) -> bool:
        if self.include_channel_ids and channel_id not in self.include_channel_ids:
            return False
        return channel_id not in self.exclude_channel_ids

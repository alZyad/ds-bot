"""Small human-readable formatting helpers used in Discord replies."""

from __future__ import annotations

import datetime as dt


def human_duration(ms: float) -> str:
    seconds = round(ms / 1000)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(num_bytes) < 1024 or unit == "GiB":
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{num_bytes:.0f} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} GiB"  # pragma: no cover


def discord_time(ms: int, style: str = "f") -> str:
    """A Discord timestamp markup, rendered in each reader's own timezone."""
    return f"<t:{int(ms / 1000)}:{style}>"


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime(
        "%Y-%m-%d %H:%M:%SZ"
    )


def slug(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).strftime("%Y%m%d-%H%M%S")

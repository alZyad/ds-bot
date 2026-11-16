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


def discord_time(ms: int, style: str = "f") -> str:
    """A Discord timestamp markup, rendered in each reader's own timezone."""
    return f"<t:{int(ms / 1000)}:{style}>"


def local_label(ms: int, tz: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%a %d %b, %H:%M")


def file_stamp(ms: int, tz: dt.tzinfo) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, tz).strftime("%Y-%m-%d_%Hh%M")

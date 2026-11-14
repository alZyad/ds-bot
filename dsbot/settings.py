"""Small JSON side-store for things that change at runtime.

Kept deliberately outside the recordings so that deleting audio never loses it.

*Who opted in to being recorded*, per server.

*A speaker name cache* — the recordings are keyed by Discord user id, which is
stable but unreadable.  We snapshot a display name the first time somebody's
audio is written so that the export dropdown can still name them after they
leave the server, when the API can no longer resolve their id to anything.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Settings:
    """A tiny persisted dict. Loads on construction, saves on every change."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"names": {}, "guilds": {}}
        self.load()

    # -- persistence --------------------------------------------------------

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("could not read %s, starting from defaults", self.path)
            return
        if isinstance(raw, dict):
            self._data["names"] = dict(raw.get("names") or {})
            self._data["guilds"] = dict(raw.get("guilds") or {})

    def save(self) -> None:
        """Write atomically: a crash mid-save must not truncate the file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=self.path.parent, prefix=".settings-", suffix=".tmp",
                delete=False, encoding="utf-8",
            ) as handle:
                tmp = Path(handle.name)
                json.dump(self._data, handle, indent=2, sort_keys=True)
            tmp.replace(self.path)
            tmp = None
        except OSError:  # pragma: no cover - disk full, read-only mount
            log.warning("could not write %s", self.path, exc_info=True)
        finally:
            if tmp is not None:
                tmp.unlink(missing_ok=True)

    # -- per-guild overrides -----------------------------------------------

    def guild_option(self, guild_id: int, key: str, default: Any = None) -> Any:
        return (self._data["guilds"].get(str(guild_id)) or {}).get(key, default)

    def set_guild_option(self, guild_id: int, key: str, value: Any) -> None:
        bucket = self._data["guilds"].setdefault(str(guild_id), {})
        if value is None:
            bucket.pop(key, None)
            if not bucket:
                self._data["guilds"].pop(str(guild_id), None)
        else:
            bucket[key] = value
        self.save()

    def opted_in(self, guild_id: int) -> list[int]:
        return [int(uid) for uid in self.guild_option(guild_id, "opted_in", [])]

    def is_opted_in(self, guild_id: int, user_id: int) -> bool:
        return user_id in self.opted_in(guild_id)

    def set_opted_in(self, guild_id: int, user_id: int, value: bool) -> bool:
        current = self.opted_in(guild_id)
        if (user_id in current) == value:
            return False
        updated = current + [user_id] if value else [u for u in current if u != user_id]
        self.set_guild_option(guild_id, "opted_in", updated or None)
        return True

    # -- speaker names ------------------------------------------------------

    def remember_name(self, user_id: int, name: str) -> None:
        if not name:
            return
        if self._data["names"].get(str(user_id)) == name:
            return
        self._data["names"][str(user_id)] = name
        self.save()

    def name_for(self, user_id: int) -> str | None:
        return self._data["names"].get(str(user_id))

"""Small JSON side-store for things the operator changes at runtime.

Two kinds of state live here, both deliberately outside the recordings so that
purging audio never loses them:

*Per-channel overrides* — currently only the silence threshold, which has to be
tuned against the actual microphones in a channel and therefore cannot be a
startup-only environment variable.

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
        self._data: dict[str, Any] = {"channels": {}, "names": {}}
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
            self._data["channels"] = dict(raw.get("channels") or {})
            self._data["names"] = dict(raw.get("names") or {})

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

    # -- per-channel overrides ---------------------------------------------

    def channel_option(self, channel_id: int, key: str, default: Any = None) -> Any:
        return (self._data["channels"].get(str(channel_id)) or {}).get(key, default)

    def set_channel_option(self, channel_id: int, key: str, value: Any) -> None:
        bucket = self._data["channels"].setdefault(str(channel_id), {})
        if value is None:
            bucket.pop(key, None)
            if not bucket:
                self._data["channels"].pop(str(channel_id), None)
        else:
            bucket[key] = value
        self.save()

    def silence_rms(self, channel_id: int, default: int) -> int:
        value = self.channel_option(channel_id, "silence_rms")
        return default if value is None else int(value)

    def set_silence_rms(self, channel_id: int, value: int | None) -> None:
        self.set_channel_option(channel_id, "silence_rms", value)

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

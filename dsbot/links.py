from __future__ import annotations

import hashlib
import hmac


class Links:
    def __init__(self, secret: str, base_url: str) -> None:
        self._key = hashlib.sha256(f"dsbot-links:{secret}".encode()).digest()
        self.base_url = base_url.rstrip("/")

    def sign(self, text: str) -> str:
        return hmac.new(self._key, text.encode(), hashlib.sha256).hexdigest()[:32]

    def verify(self, text: str, signature: str) -> bool:
        return hmac.compare_digest(self.sign(text), signature)

    def session_path(self, guild_id: int, session_ms: int, track: str) -> str:
        path = f"{guild_id}/{session_ms}/{track}"
        return f"/r/{path}/{self.sign(path)}.m4a"

    def session_url(self, guild_id: int, session_ms: int, track: str) -> str:
        return self.base_url + self.session_path(guild_id, session_ms, track)

    def file_path(self, name: str) -> str:
        return f"/f/{self.sign(name)}/{name}"

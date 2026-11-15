from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web

from .encoder import concat
from .store import ChunkStore

if TYPE_CHECKING:
    from .bot import RecordingBot

log = logging.getLogger(__name__)

EXPORT_MAX_BYTES = 1024 * 1024 * 1024
EXPORT_MAX_AGE_SECONDS = 24 * 3600
SNAPSHOT_RE = re.compile(r"^(?P<guild>\d+)_(?P<session>\d+)_(?P<track>mix|\d+)_\d+\.m4a$")


class Exporter:
    def __init__(self, store: ChunkStore, directory: Path, *, binary: str, bitrate: str) -> None:
        self.store = store
        self.directory = Path(directory)
        self.binary = binary
        self.bitrate = bitrate
        self._locks: dict[str, asyncio.Lock] = {}

    async def build(self, guild_id: int, session_ms: int, track: str) -> Path | None:
        chunks = self.store.list_chunks(guild_id, session_ms, track=track)
        if not chunks:
            return None
        key = f"{guild_id}_{session_ms}_{track}"
        path = self.directory / f"{key}_{len(chunks)}.m4a"
        async with self._locks.setdefault(key, asyncio.Lock()):
            if not path.exists():
                self.directory.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                await concat(
                    chunks, tmp, binary=self.binary, bitrate=self.bitrate,
                    workdir=self.directory,
                )
                tmp.replace(path)
                self.prune(keep=path)
        return path

    def prune(self, *, keep: Path) -> None:
        files = []
        for path in self.directory.glob("*.m4a"):
            try:
                files.append((path.stat(), path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0].st_mtime)
        total = sum(stat.st_size for stat, _ in files)
        cutoff = time.time() - EXPORT_MAX_AGE_SECONDS
        for stat, path in files:
            if path == keep:
                continue
            if stat.st_mtime < cutoff or total > EXPORT_MAX_BYTES:
                path.unlink(missing_ok=True)
                total -= stat.st_size

    def forget(self, guild_id: int, session_ms: int) -> None:
        for path in self.directory.glob(f"{guild_id}_{session_ms}_*.m4a"):
            path.unlink(missing_ok=True)


def make_app(bot: RecordingBot) -> web.Application:
    async def session_link(request: web.Request) -> web.StreamResponse:
        guild_id = int(request.match_info["guild"])
        session_ms = int(request.match_info["session"])
        track = request.match_info["track"]
        if not bot.links.verify(f"{guild_id}/{session_ms}/{track}", request.match_info["sig"]):
            raise web.HTTPNotFound()
        recorder = bot.recorders.get(guild_id)
        if recorder is not None and recorder.session_ms == session_ms:
            await recorder.flush()
        path = await bot.exporter.build(guild_id, session_ms, track)
        if path is None:
            raise web.HTTPNotFound()
        raise web.HTTPFound(bot.links.file_path(path.name))

    async def snapshot(request: web.Request) -> web.StreamResponse:
        name = request.match_info["name"]
        match = SNAPSHOT_RE.match(name)
        if match is None or not bot.links.verify(name, request.match_info["sig"]):
            raise web.HTTPNotFound()
        guild_id, session_ms, track = int(match["guild"]), int(match["session"]), match["track"]
        path = bot.exporter.directory / name
        if not path.is_file():
            raise web.HTTPFound(bot.links.session_path(guild_id, session_ms, track))
        filename = bot.download_name(guild_id, session_ms, track)
        return web.FileResponse(path, headers={
            "Content-Type": "audio/mp4",
            "Content-Disposition": f'inline; filename="{filename}"',
        })

    app = web.Application()
    app.router.add_get(
        r"/r/{guild:\d+}/{session:\d+}/{track:mix|\d+}/{sig:[0-9a-f]{32}}.m4a", session_link
    )
    app.router.add_get(r"/f/{sig:[0-9a-f]{32}}/{name}", snapshot)
    return app


async def start_web(bot: RecordingBot, port: int) -> web.AppRunner:
    runner = web.AppRunner(make_app(bot), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("serving recordings on port %s", port)
    return runner

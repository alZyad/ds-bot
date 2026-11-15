import os
import time
from urllib.parse import urlsplit

import pytest
from aiohttp.test_utils import TestClient, TestServer

from dsbot import web
from dsbot.bot import RecordingBot
from dsbot.links import Links
from dsbot.store import MIX_TRACK
from dsbot.web import make_app


def write(bot, session, minute, payload, track=MIX_TRACK, guild=100):
    start = session + minute * 60_000
    part = bot.store.part_path(guild, session, start, track)
    part.write_bytes(payload)
    bot.store.commit(part, start_ms=start, duration_ms=60_000, segment_ms=session, track=track)


def link(bot, session, track=MIX_TRACK):
    return urlsplit(bot.links.session_url(100, session, track)).path


@pytest.fixture
async def client(cfg):
    bot = RecordingBot(cfg)
    async with TestClient(TestServer(make_app(bot))) as client:
        client.bot = bot
        yield client


def test_links_are_signed_per_secret():
    a, b = Links("one", "https://x/"), Links("two", "https://x")
    assert a.session_url(1, 2, "mix").startswith("https://x/r/1/2/mix/")
    assert a.session_url(1, 2, "mix") != b.session_url(1, 2, "mix")
    assert a.verify("1/2/mix", a.sign("1/2/mix"))
    assert not a.verify("1/2/7", a.sign("1/2/mix"))


async def test_a_link_serves_the_whole_session(client):
    write(client.bot, 1000, 0, b"aaa")
    write(client.bot, 1000, 1, b"bbb")
    response = await client.get(link(client.bot, 1000))
    assert response.status == 200
    assert await response.read() == b"aaabbb"
    assert response.headers["Content-Type"] == "audio/mp4"
    assert "_everyone.m4a" in response.headers["Content-Disposition"]


async def test_one_voice_has_its_own_link(client):
    write(client.bot, 1000, 0, b"mix")
    write(client.bot, 1000, 0, b"solo", track="42")
    response = await client.get(link(client.bot, 1000, "42"))
    assert await response.read() == b"solo"
    assert "_user-42.m4a" in response.headers["Content-Disposition"]


async def test_a_forged_link_is_not_found(client):
    write(client.bot, 1000, 0, b"aaa", track="42")
    good = link(client.bot, 1000)
    assert (await client.get(good.replace("/mix/", "/42/"))).status == 404
    assert (await client.get(good[:-5] + ("0" if good[-5] != "0" else "1") + ".m4a")).status == 404
    assert (await client.get("/f/" + "0" * 32 + "/100_1000_mix_1.m4a")).status == 404


async def test_a_deleted_session_is_gone(client):
    write(client.bot, 1000, 0, b"aaa")
    client.bot.store.delete_session(100, 1000)
    assert (await client.get(link(client.bot, 1000))).status == 404


async def test_seeking_is_supported(client):
    write(client.bot, 1000, 0, b"abcdef")
    response = await client.get(link(client.bot, 1000), headers={"Range": "bytes=2-3"})
    assert response.status == 206
    assert await response.read() == b"cd"


async def test_new_audio_makes_a_new_snapshot_and_keeps_the_old(client):
    write(client.bot, 1000, 0, b"aaa")
    first = await client.get(link(client.bot, 1000))
    write(client.bot, 1000, 1, b"bbb")
    second = await client.get(link(client.bot, 1000))
    assert await second.read() == b"aaabbb"
    again = await client.get(first.url.path)
    assert await again.read() == b"aaa"


async def test_a_live_session_is_flushed_before_export(client):
    bot = client.bot

    class Live:
        session_ms = 1000

        async def flush(self):
            write(bot, 1000, 1, b"now")

    write(bot, 1000, 0, b"then")
    bot.recorders[100] = Live()
    response = await client.get(link(bot, 1000))
    assert await response.read() == b"thennow"


def test_exports_are_pruned_by_age_and_size(cfg, monkeypatch):
    bot = RecordingBot(cfg)
    directory = bot.exporter.directory
    directory.mkdir(parents=True)
    old, big, keep = (directory / f"1_1_mix_{n}.m4a" for n in (1, 2, 3))
    for path, size in ((old, 10), (big, 100), (keep, 100)):
        path.write_bytes(b"x" * size)
    stale = time.time() - 2 * web.EXPORT_MAX_AGE_SECONDS
    os.utime(old, (stale, stale))
    monkeypatch.setattr(web, "EXPORT_MAX_BYTES", 150)
    bot.exporter.prune(keep=keep)
    assert sorted(p.name for p in directory.iterdir()) == [keep.name]


async def test_a_stale_snapshot_link_falls_back_to_the_session(client):
    write(client.bot, 1000, 0, b"aaa")
    first = await client.get(link(client.bot, 1000))
    for path in client.bot.exporter.directory.iterdir():
        path.unlink()
    write(client.bot, 1000, 1, b"bbb")
    again = await client.get(first.url.path)
    assert await again.read() == b"aaabbb"

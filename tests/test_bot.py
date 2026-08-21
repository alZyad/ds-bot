"""Tests for the 'how many people are in this channel' logic and reconciliation.

Discord objects are replaced by minimal stand-ins: none of this touches the
gateway.
"""

from dataclasses import dataclass, field

import pytest

from dsbot.bot import RecordingBot
from dsbot.config import Config


@dataclass
class FakeMember:
    id: int
    bot: bool = False


@dataclass
class FakeChannel:
    id: int
    name: str = "voice"
    members: list = field(default_factory=list)
    guild: object = None

    @property
    def mention(self):
        return f"<#{self.id}>"


@dataclass
class FakeGuild:
    id: int = 100
    voice_channels: list = field(default_factory=list)
    stage_channels: list = field(default_factory=list)
    me: FakeMember = field(default_factory=lambda: FakeMember(999, bot=True))
    voice_client: object = None
    filesize_limit: int = 26_214_400

    def get_channel(self, channel_id):
        for channel in self.voice_channels + self.stage_channels:
            if channel.id == channel_id:
                return channel
        return None


@pytest.fixture
def bot(tmp_path):
    return RecordingBot(Config(data_dir=tmp_path, min_speakers=2))


def guild_with(*channels):
    guild = FakeGuild()
    guild.voice_channels = list(channels)
    for channel in channels:
        channel.guild = guild
    return guild


def test_humans_exclude_bots_and_ourselves(bot):
    channel = FakeChannel(1, members=[
        FakeMember(1), FakeMember(2), FakeMember(3, bot=True), FakeMember(999),
    ])
    guild_with(channel)
    assert [m.id for m in bot.humans_in(channel)] == [1, 2]


def test_one_person_is_not_enough(bot):
    guild = guild_with(FakeChannel(1, members=[FakeMember(1)]))
    assert bot.eligible_channels(guild) == []


def test_two_people_make_a_channel_eligible(bot):
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    assert bot.eligible_channels(guild) == [channel]
    assert bot.occupancy(guild) == {1: 2}


def test_a_bot_does_not_count_towards_the_threshold(bot):
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2, bot=True)])
    assert bot.eligible_channels(guild_with(channel)) == []


def test_the_busiest_channel_comes_first(bot):
    quiet = FakeChannel(1, members=[FakeMember(i) for i in range(2)])
    busy = FakeChannel(2, members=[FakeMember(i) for i in range(10, 15)])
    guild = guild_with(quiet, busy)
    assert bot.eligible_channels(guild) == [busy, quiet]


def test_stage_channels_are_watched_too(bot):
    stage = FakeChannel(7, members=[FakeMember(1), FakeMember(2)])
    guild = FakeGuild(stage_channels=[stage])
    stage.guild = guild
    assert bot.eligible_channels(guild) == [stage]


def test_allow_and_deny_lists_are_respected(tmp_path):
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    other = FakeChannel(2, members=[FakeMember(3), FakeMember(4)])
    guild = guild_with(channel, other)

    denied = RecordingBot(Config(data_dir=tmp_path, exclude_channel_ids=frozenset({1})))
    assert denied.eligible_channels(guild) == [other]

    allowed = RecordingBot(Config(data_dir=tmp_path, include_channel_ids=frozenset({1})))
    assert allowed.eligible_channels(guild) == [channel]


def test_min_speakers_is_configurable(tmp_path):
    channel = FakeChannel(1, members=[FakeMember(1)])
    guild = guild_with(channel)
    solo = RecordingBot(Config(data_dir=tmp_path, min_speakers=1))
    assert solo.eligible_channels(guild) == [channel]


async def test_reconcile_starts_on_the_busiest_channel(bot, monkeypatch):
    started = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(started, channel))
    busy = FakeChannel(2, members=[FakeMember(i) for i in range(10, 15)])
    quiet = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    await bot.reconcile(guild_with(quiet, busy))
    assert started == [busy]


async def test_reconcile_stays_put_while_the_channel_is_still_eligible(bot, monkeypatch):
    """Hopping to a busier channel would cut the conversation in progress."""
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, channel))
    monkeypatch.setattr(bot, "_teardown", lambda guild_id, reason: _record(calls, reason))

    quiet = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    busy = FakeChannel(2, members=[FakeMember(i) for i in range(10, 15)])
    guild = guild_with(quiet, busy)

    class Recorder:
        channel_id = 1
    bot.recorders[guild.id] = Recorder()
    guild.voice_client = type("VC", (), {"is_connected": lambda self: True})()

    await bot.reconcile(guild)
    assert calls == []  # we keep recording the quiet channel


async def test_reconcile_moves_when_the_channel_empties(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, ("setup", channel.id)))

    async def teardown(guild_id, reason):
        calls.append(("teardown", reason))
        bot.recorders.pop(guild_id, None)

    monkeypatch.setattr(bot, "_teardown", teardown)

    empty = FakeChannel(1, members=[FakeMember(1)])
    busy = FakeChannel(2, members=[FakeMember(10), FakeMember(11)])
    guild = guild_with(empty, busy)

    class Recorder:
        channel_id = 1
    bot.recorders[guild.id] = Recorder()
    guild.voice_client = type("VC", (), {"is_connected": lambda self: True})()

    await bot.reconcile(guild)
    assert calls == [("teardown", "channel no longer eligible"), ("setup", 2)]


async def test_reconcile_leaves_when_nothing_is_eligible(bot, monkeypatch):
    calls = []

    async def teardown(guild_id, reason):
        calls.append(reason)
        bot.recorders.pop(guild_id, None)

    monkeypatch.setattr(bot, "_teardown", teardown)
    guild = guild_with(FakeChannel(1, members=[FakeMember(1)]))
    guild.voice_client = object()
    await bot.reconcile(guild)
    assert calls == ["no eligible channel"]


def test_upload_limit_honours_both_the_config_and_the_guild(tmp_path):
    small = RecordingBot(Config(data_dir=tmp_path, max_upload_mb=9.0))
    assert small.upload_limit(FakeGuild(filesize_limit=26_214_400)) == 9 * 1024 * 1024
    # a guild with the free-tier 10 MiB limit wins over a larger config value
    generous = RecordingBot(Config(data_dir=tmp_path, max_upload_mb=100.0))
    limit = generous.upload_limit(FakeGuild(filesize_limit=10 * 1024 * 1024))
    assert limit < 10 * 1024 * 1024


async def _record(sink, value):
    sink.append(value)


def test_safe_name_sanitises_channel_names():
    from dsbot.bot import _safe_name

    assert _safe_name("général 🎧 / chat") == "général-chat"
    assert _safe_name("###") == "voice"
    assert len(_safe_name("x" * 200)) == 48


async def test_reconcile_recovers_from_a_lost_connection(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, ("setup", channel.id)))

    async def teardown(guild_id, reason):
        calls.append(("teardown", reason))
        bot.recorders.pop(guild_id, None)

    monkeypatch.setattr(bot, "_teardown", teardown)
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)

    class Recorder:
        channel_id = 1
    bot.recorders[guild.id] = Recorder()
    guild.voice_client = None  # gateway dropped us without an event

    await bot.reconcile(guild)
    assert calls == [("teardown", "connection lost"), ("setup", 1)]

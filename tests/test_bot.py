"""Tests for channel eligibility, reconciliation and the export helpers.

Discord objects are replaced by minimal stand-ins: none of this touches the
gateway.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from dsbot.bot import RecordingBot, _count_files, _member_label, _safe_name
from dsbot.config import Config


@dataclass
class FakeMember:
    id: int
    name: str = "someone"
    display_name: str = ""
    bot: bool = False

    def __post_init__(self):
        self.display_name = self.display_name or self.name


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
    people: dict = field(default_factory=dict)

    def get_channel(self, channel_id):
        for channel in self.voice_channels + self.stage_channels:
            if channel.id == channel_id:
                return channel
        return None

    def get_member(self, user_id):
        return self.people.get(user_id)


@pytest.fixture
def bot(tmp_path):
    return RecordingBot(Config(data_dir=tmp_path, min_speakers=2, leave_grace=0.0))


def guild_with(*channels):
    guild = FakeGuild()
    guild.voice_channels = list(channels)
    for channel in channels:
        channel.guild = guild
    return guild


def connected(guild, channel_id, bot):
    """Pretend we are already recording ``channel_id`` in ``guild``."""
    class Recorder:
        pass
    recorder = Recorder()
    recorder.channel_id = channel_id
    bot.recorders[guild.id] = recorder
    guild.voice_client = type("VC", (), {"is_connected": lambda self: True})()


async def _record(sink, value):
    sink.append(value)


# -- eligibility ------------------------------------------------------------

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


def test_the_channel_that_filled_up_first_comes_first(bot):
    """First come, first served — not whichever channel is busiest."""
    early = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    later = FakeChannel(2, members=[])
    guild = guild_with(early, later)
    assert bot.eligible_channels(guild) == [early]

    later.members = [FakeMember(i) for i in range(10, 15)]  # five people, but late
    assert bot.eligible_channels(guild) == [early, later]


def test_a_channel_that_empties_loses_its_place_in_the_queue(bot):
    first = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    second = FakeChannel(2, members=[])
    guild = guild_with(first, second)
    bot.eligible_channels(guild)

    second.members = [FakeMember(3), FakeMember(4)]
    bot.eligible_channels(guild)
    first.members = []                       # everyone leaves the first channel
    assert bot.eligible_channels(guild) == [second]

    first.members = [FakeMember(5), FakeMember(6)]  # and comes back, now last
    assert bot.eligible_channels(guild) == [second, first]


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


# -- reconciliation ---------------------------------------------------------

async def test_reconcile_starts_on_the_first_eligible_channel(bot, monkeypatch):
    started = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(started, channel))
    early = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    later = FakeChannel(2, members=[])
    guild = guild_with(early, later)
    bot.eligible_channels(guild)  # early gets in the queue first
    later.members = [FakeMember(i) for i in range(10, 15)]

    await bot.reconcile(guild)
    assert started == [early]


async def test_reconcile_stays_put_while_the_channel_is_still_eligible(bot, monkeypatch):
    """Hopping to a busier channel would cut the conversation in progress."""
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, channel))
    monkeypatch.setattr(bot, "_teardown", lambda guild_id, reason: _record(calls, reason))

    quiet = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    busy = FakeChannel(2, members=[FakeMember(i) for i in range(10, 15)])
    guild = guild_with(quiet, busy)
    connected(guild, 1, bot)

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
    connected(guild, 1, bot)

    await bot.reconcile(guild)  # leave_grace is 0 in this fixture
    assert calls == [("teardown", "channel no longer eligible"), ("setup", 2)]


async def test_a_blip_does_not_sever_the_recording(tmp_path, monkeypatch):
    """Someone reseating a headset drops the count to one for a moment."""
    bot = RecordingBot(Config(data_dir=tmp_path, min_speakers=2, leave_grace=30.0))
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, "setup"))
    monkeypatch.setattr(bot, "_teardown", lambda guild_id, reason: _record(calls, reason))

    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    connected(guild, 1, bot)

    channel.members = [FakeMember(1)]  # the second person drops out
    await bot.reconcile(guild)
    assert calls == []                 # held, not torn down
    assert bot.recorders[guild.id] is not None

    channel.members = [FakeMember(1), FakeMember(2)]  # ...and comes straight back
    await bot.reconcile(guild)
    assert calls == []
    assert guild.id not in bot._leave_since  # the grace clock was cancelled


async def test_the_grace_period_does_expire(tmp_path, monkeypatch):
    bot = RecordingBot(Config(data_dir=tmp_path, min_speakers=2, leave_grace=0.05))
    calls = []

    async def teardown(guild_id, reason):
        calls.append(reason)
        bot.recorders.pop(guild_id, None)
        guild.voice_client = None  # a real teardown disconnects

    monkeypatch.setattr(bot, "_teardown", teardown)
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, "setup"))

    channel = FakeChannel(1, members=[FakeMember(1)])
    guild = guild_with(channel)
    connected(guild, 1, bot)

    await bot.reconcile(guild)
    assert calls == []                    # inside the grace window
    await asyncio.sleep(0.15)             # the scheduled re-check fires
    assert calls == ["channel no longer eligible"]


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


async def test_reconcile_recovers_from_a_lost_connection(bot, monkeypatch):
    calls = []
    monkeypatch.setattr(bot, "_setup", lambda channel: _record(calls, ("setup", channel.id)))

    async def teardown(guild_id, reason):
        calls.append(("teardown", reason))
        bot.recorders.pop(guild_id, None)

    monkeypatch.setattr(bot, "_teardown", teardown)
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    connected(guild, 1, bot)
    guild.voice_client = None  # gateway dropped us without an event

    await bot.reconcile(guild)
    assert calls == [("teardown", "connection lost"), ("setup", 1)]


# -- names ------------------------------------------------------------------

def test_member_label_prefers_the_display_name():
    assert _member_label(FakeMember(1, name="zyad", display_name="Zyad")) == "Zyad (@zyad)"
    assert _member_label(FakeMember(1, name="zyad", display_name="zyad")) == "@zyad"


def test_speaker_labels_survive_the_member_leaving(bot):
    guild = FakeGuild(people={7: FakeMember(7, name="zyad", display_name="Zyad")})
    bot.remember_speaker(guild, 7)
    assert bot.speaker_label(guild, 7) == "Zyad (@zyad)"

    gone = FakeGuild()  # they left; the API can no longer resolve the id
    assert bot.speaker_label(gone, 7) == "Zyad (@zyad)"
    assert bot.speaker_label(gone, 8) == "user 8"


def test_remembering_an_unknown_member_is_a_no_op(bot):
    bot.remember_speaker(FakeGuild(), 12)
    assert bot.settings.name_for(12) is None


# -- export helpers ---------------------------------------------------------

def test_upload_limit_honours_both_the_config_and_the_guild(tmp_path):
    small = RecordingBot(Config(data_dir=tmp_path, max_upload_mb=9.0))
    assert small.upload_limit(FakeGuild(filesize_limit=26_214_400)) == 9 * 1024 * 1024
    # a guild with the free-tier 10 MiB limit wins over a larger config value
    generous = RecordingBot(Config(data_dir=tmp_path, max_upload_mb=100.0))
    limit = generous.upload_limit(FakeGuild(filesize_limit=10 * 1024 * 1024))
    assert limit < 10 * 1024 * 1024


def test_segments_for_merges_close_conversations(tmp_path):
    from dsbot.store import MIX_TRACK
    bot = RecordingBot(Config(data_dir=tmp_path, merge_gap_seconds=120.0))
    for start, segment in ((0, 0), (60_000, 0), (180_000, 180_000), (10_000_000, 10_000_000)):
        part = bot.store.part_path(1, 2, start, MIX_TRACK)
        part.write_bytes(b"x" * 100)
        bot.store.commit(
            part, start_ms=start, duration_ms=60_000, segment_ms=segment,
            track=MIX_TRACK,
        )
    # 0-120s and 180-240s are 60s apart, so they merge; the far one stays alone
    segments = bot.segments_for(1, 2, track=MIX_TRACK, since_ms=None)
    assert len(segments) == 2
    assert segments[0].duration_ms == 180_000


def test_counting_files_before_encoding_them(tmp_path):
    from dsbot.store import MIX_TRACK, group_segments
    bot = RecordingBot(Config(data_dir=tmp_path))
    chunks = []
    for i in range(4):
        part = bot.store.part_path(1, 2, i * 60_000, MIX_TRACK)
        part.write_bytes(b"x" * 400)
        chunks.append(bot.store.commit(
            part, start_ms=i * 60_000, duration_ms=60_000, segment_ms=0,
            track=MIX_TRACK,
        ))
    jobs = [("mixed", group_segments(chunks))]
    assert _count_files(bot, jobs, max_bytes=10_000) == 1
    assert _count_files(bot, jobs, max_bytes=1000) == 2
    assert _count_files(bot, jobs, max_bytes=400) == 4


def test_safe_name_sanitises_channel_names():
    assert _safe_name("général 🎧 / chat") == "général-chat"
    assert _safe_name("###") == "voice"
    assert len(_safe_name("x" * 200)) == 48

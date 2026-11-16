"""Tests for channel eligibility, reconciliation and the export helpers.

Discord objects are replaced by minimal stand-ins: none of this touches the
gateway.
"""

import asyncio
from dataclasses import dataclass, field

import pytest

from dsbot.bot import (
    RecordingBot,
    _member_label,
    help_command,
    record_me,
    stop_recording_me,
)
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
    sent: list = field(default_factory=list)

    @property
    def mention(self):
        return f"<#{self.id}>"

    async def send(self, message, **kwargs):
        self.sent.append(message)


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


def joining(cfg):
    bot = RecordingBot(cfg)
    for user_id in range(1, 21):
        bot.settings.set_opted_in(100, user_id, True)
    return bot


@pytest.fixture
def bot(tmp_path):
    return joining(Config(data_dir=tmp_path, min_speakers=2, leave_grace=0.0))


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

    denied = joining(Config(data_dir=tmp_path, exclude_channel_ids=frozenset({1})))
    assert denied.eligible_channels(guild) == [other]

    allowed = joining(Config(data_dir=tmp_path, include_channel_ids=frozenset({1})))
    assert allowed.eligible_channels(guild) == [channel]


def test_min_speakers_is_configurable(tmp_path):
    channel = FakeChannel(1, members=[FakeMember(1)])
    guild = guild_with(channel)
    solo = joining(Config(data_dir=tmp_path, min_speakers=1))
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
    assert calls == [("teardown", "fewer than 2 people"), ("setup", 2)]


async def test_a_blip_does_not_sever_the_recording(tmp_path, monkeypatch):
    """Someone reseating a headset drops the count to one for a moment."""
    bot = joining(Config(data_dir=tmp_path, min_speakers=2, leave_grace=30.0))
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
    bot = joining(Config(data_dir=tmp_path, min_speakers=2, leave_grace=0.05))
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
    assert calls == ["fewer than 2 people"]


async def test_reconcile_leaves_when_nothing_is_eligible(bot, monkeypatch):
    calls = []

    async def teardown(guild_id, reason):
        calls.append(reason)
        bot.recorders.pop(guild_id, None)

    monkeypatch.setattr(bot, "_teardown", teardown)
    guild = guild_with(FakeChannel(1, members=[FakeMember(1)]))
    guild.voice_client = object()
    await bot.reconcile(guild)
    assert calls == ["nothing to record"]


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


# -- opt-in -----------------------------------------------------------------

def test_nobody_is_recorded_by_default(tmp_path):
    bot = RecordingBot(Config(data_dir=tmp_path))
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    assert bot.eligible_channels(guild_with(channel)) == []


def test_one_opted_in_person_among_two_is_enough(tmp_path):
    bot = RecordingBot(Config(data_dir=tmp_path))
    bot.settings.set_opted_in(100, 2, True)
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    assert bot.eligible_channels(guild) == [channel]
    assert [m.id for m in bot.recorded_in(channel)] == [2]


def test_an_opted_in_person_alone_is_not_recorded(tmp_path):
    bot = RecordingBot(Config(data_dir=tmp_path))
    bot.settings.set_opted_in(100, 1, True)
    channel = FakeChannel(1, members=[FakeMember(1)])
    assert bot.eligible_channels(guild_with(channel)) == []


def test_only_opted_in_voices_reach_the_recorder(tmp_path):
    bot = RecordingBot(Config(data_dir=tmp_path))
    bot.settings.set_opted_in(100, 1, True)
    fed = []
    recorder = type("R", (), {"feed": lambda self, uid, pcm: fed.append(uid)})()
    on_packet = bot.packet_handler(100, recorder)
    packet = type("P", (), {"pcm": b"x"})()
    on_packet(FakeMember(1), packet)
    on_packet(FakeMember(2), packet)
    on_packet(FakeMember(3, bot=True), packet)
    assert fed == [1]
    bot.settings.set_opted_in(100, 2, True)
    on_packet(FakeMember(2), packet)
    assert fed == [1, 2]


async def test_opting_out_ends_the_recording_when_nobody_is_left(tmp_path, monkeypatch):
    bot = RecordingBot(Config(data_dir=tmp_path, leave_grace=0.0))
    bot.settings.set_opted_in(100, 1, True)
    reasons = []

    async def teardown(guild_id, reason):
        reasons.append(reason)
        bot.recorders.pop(guild_id, None)
        guild.voice_client = None

    monkeypatch.setattr(bot, "_teardown", teardown)
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    connected(guild, 1, bot)

    bot.settings.set_opted_in(100, 1, False)
    await bot.reconcile(guild)
    assert reasons == ["nobody here has opted in"]


async def test_newcomers_are_announced_once(bot):
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2), FakeMember(50)])
    guild = guild_with(channel)
    connected(guild, 1, bot)
    bot._announced[guild.id] = {1, 2}

    await bot.reconcile(guild)
    assert channel.sent == []

    bot.settings.set_opted_in(100, 50, True)
    await bot.reconcile(guild)
    await bot.reconcile(guild)
    assert channel.sent == ["🔴 Also recording <@50>."]


@dataclass
class FakeResponse:
    sent: list = field(default_factory=list)

    async def send_message(self, message, *, ephemeral=False, **kwargs):
        self.sent.append((message, ephemeral))


@dataclass
class FakeInteraction:
    client: object
    guild: object
    user: object
    channel_id: int = 0
    response: FakeResponse = field(default_factory=FakeResponse)


def _as_member(user_id, channel=None):
    member = FakeMember(user_id)
    member.mention = f"<@{user_id}>"
    member.voice = type("V", (), {"channel": channel})() if channel else None
    return member


async def test_record_me_opts_in_publicly(tmp_path, monkeypatch):
    bot = RecordingBot(Config(data_dir=tmp_path))
    monkeypatch.setattr(bot, "reconcile", lambda guild: _record([], guild))
    interaction = FakeInteraction(bot, FakeGuild(), _as_member(7))

    await record_me.callback(interaction)
    assert bot.settings.is_opted_in(100, 7)
    message, ephemeral = interaction.response.sent[-1]
    assert "<@7> opted in" in message and not ephemeral

    await record_me.callback(interaction)
    assert interaction.response.sent[-1][1] is True


async def test_record_me_in_the_recorded_chat_is_not_announced_twice(bot, monkeypatch):
    monkeypatch.setattr(bot, "reconcile", lambda guild: _record([], guild))
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2)])
    guild = guild_with(channel)
    connected(guild, 1, bot)
    interaction = FakeInteraction(bot, guild, _as_member(50, channel), channel_id=1)

    await record_me.callback(interaction)
    assert "Recording them now." in interaction.response.sent[-1][0]
    assert 50 in bot._announced[guild.id]


async def test_stop_recording_me_opts_out_publicly(tmp_path, monkeypatch):
    bot = RecordingBot(Config(data_dir=tmp_path))
    monkeypatch.setattr(bot, "reconcile", lambda guild: _record([], guild))
    bot.settings.set_opted_in(100, 7, True)
    interaction = FakeInteraction(bot, FakeGuild(), _as_member(7))

    await stop_recording_me.callback(interaction)
    assert not bot.settings.is_opted_in(100, 7)
    message, ephemeral = interaction.response.sent[-1]
    assert "<@7> opted out" in message and not ephemeral

    await stop_recording_me.callback(interaction)
    assert interaction.response.sent[-1][1] is True


async def test_help_lists_the_commands_privately(tmp_path):
    bot = RecordingBot(Config(data_dir=tmp_path))
    interaction = FakeInteraction(bot, FakeGuild(), _as_member(7))

    await help_command.callback(interaction)
    message, ephemeral = interaction.response.sent[-1]
    assert ephemeral
    for command in ("/record-me", "/stop-recording-me", "/recordings"):
        assert f"`{command}`" in message
    assert "2+ people" in message


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


# -- sessions ---------------------------------------------------------------

def test_a_first_join_opens_a_new_session(bot, monkeypatch):
    monkeypatch.setattr("dsbot.bot.now_ms", lambda: 1_000_000)
    assert bot.session_for(100) == 1_000_000


def test_rejoining_within_the_gap_continues_the_session(tmp_path, monkeypatch):
    bot = RecordingBot(Config(data_dir=tmp_path, session_gap_seconds=300))
    bot._sessions[100] = 5_000
    bot._left_at[100] = 10_000
    monkeypatch.setattr("dsbot.bot.now_ms", lambda: 10_000 + 299_000)
    assert bot.session_for(100) == 5_000
    monkeypatch.setattr("dsbot.bot.now_ms", lambda: 10_000 + 300_000)
    assert bot.session_for(100) == 310_000


def test_after_a_restart_the_last_chunk_decides(tmp_path, monkeypatch):
    from dsbot.store import MIX_TRACK
    bot = RecordingBot(Config(data_dir=tmp_path, session_gap_seconds=300))
    part = bot.store.part_path(100, 5_000, 5_000, MIX_TRACK)
    part.write_bytes(b"x")
    bot.store.commit(part, start_ms=5_000, duration_ms=60_000, segment_ms=5_000, track=MIX_TRACK)
    monkeypatch.setattr("dsbot.bot.now_ms", lambda: 65_000 + 299_000)
    assert bot.session_for(100) == 5_000
    monkeypatch.setattr("dsbot.bot.now_ms", lambda: 65_000 + 300_000)
    assert bot.session_for(100) == 365_000


def test_speakers_are_listed_in_their_session(bot):
    guild = FakeGuild(people={7: FakeMember(7, name="zyad")})
    bot.remember_speaker(guild, 7, 5_000)
    bot.remember_speaker(guild, 8, 5_000)
    assert bot.store.people(100, 5_000) == [7, 8]


# -- presence ---------------------------------------------------------------

def test_the_status_says_how_many_people_are_recorded(bot, monkeypatch):
    assert bot.presence_text() is None
    channel = FakeChannel(1, members=[FakeMember(1), FakeMember(2), FakeMember(50)])
    guild = guild_with(channel)
    connected(guild, 1, bot)
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)
    assert bot.presence_text() == "🔴 Recording 2 people"
    channel.members = [FakeMember(1), FakeMember(50)]
    assert bot.presence_text() == "🔴 Recording 1 person"


async def test_the_status_is_only_sent_when_it_changes(bot, monkeypatch):
    sent = []

    async def change_presence(activity=None):
        sent.append(activity.name if activity else None)

    monkeypatch.setattr(bot, "change_presence", change_presence)
    texts = iter(["🔴 Recording 2 people", "🔴 Recording 2 people", None])
    monkeypatch.setattr(bot, "presence_text", lambda: next(texts))
    for _ in range(3):
        await bot._update_presence()
    assert sent == ["🔴 Recording 2 people", None]

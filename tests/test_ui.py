from dataclasses import dataclass, field

from dsbot.bot import RecordingBot
from dsbot.config import Config
from dsbot.store import MIX_TRACK
from dsbot.ui import DeleteButton, RecordingsView, SessionSelect, recordings, render


@dataclass
class Member:
    id: int
    name: str
    display_name: str


@dataclass
class Guild:
    id: int = 100
    people: dict = field(default_factory=dict)

    def get_member(self, user_id):
        return self.people.get(user_id)


@dataclass
class Live:
    session_ms: int
    channel_id: int = 1


def write(bot, session, track=MIX_TRACK):
    part = bot.store.part_path(100, session, session, track)
    part.write_bytes(b"x")
    bot.store.commit(part, start_ms=session, duration_ms=90_000, segment_ms=session, track=track)


def make_bot(tmp_path):
    return RecordingBot(Config(data_dir=tmp_path, public_url="https://rec.example"))


def test_an_empty_server_explains_how_to_start(tmp_path):
    text = render(make_bot(tmp_path), Guild(), None)
    assert "recorded: nobody yet" in text
    assert "No recordings." in text


def test_a_session_links_everyone_and_each_voice(tmp_path):
    bot = make_bot(tmp_path)
    bot.settings.set_opted_in(100, 7, True)
    write(bot, 1_000_000)
    write(bot, 1_000_000, "7")
    bot.store.add_person(100, 1_000_000, 7)
    guild = Guild(people={7: Member(7, "zyad", "Zy[a]d")})

    text = render(bot, guild, 1_000_000)
    assert "recorded: <@7>" in text
    assert "**<t:1000:f> → <t:1090:t>** · 1m30s" in text
    assert "[All](<https://rec.example/r/100/1000000/mix/" in text
    assert "[Zyad](<https://rec.example/r/100/1000000/7/" in text


def test_a_live_session_with_nothing_said_yet(tmp_path):
    bot = make_bot(tmp_path)
    bot.recorders[100] = Live(2_000_000)
    text = render(bot, Guild(), 2_000_000)
    assert "🔴 live in <#1>" in text
    assert "No audio." in text


def test_many_voices_stay_within_one_message(tmp_path):
    bot = make_bot(tmp_path)
    write(bot, 1_000_000)
    for user_id in range(10**17, 10**17 + 40):
        write(bot, 1_000_000, str(user_id))
    assert len(render(bot, Guild(), 1_000_000)) <= 2000


async def test_admins_can_delete_finished_sessions_only(tmp_path):
    bot = make_bot(tmp_path)
    write(bot, 1_000_000)
    write(bot, 2_000_000)
    view = RecordingsView(bot, Guild(), 2_000_000, admin=True)
    kinds = [type(item) for item in view.children]
    assert kinds == [SessionSelect, DeleteButton]
    select = view.children[0]
    assert [o.value for o in select.options] == ["2000000", "1000000"]
    assert [o.default for o in select.options] == [True, False]

    assert DeleteButton not in [type(i) for i in RecordingsView(bot, Guild(), 2_000_000, admin=False).children]
    bot.recorders[100] = Live(2_000_000)
    assert DeleteButton not in [type(i) for i in RecordingsView(bot, Guild(), 2_000_000, admin=True).children]


def test_the_stop_notice_links_the_session(tmp_path):
    bot = make_bot(tmp_path)
    assert bot._stop_message(None, "bye") == "⏹️ Stopped (bye) · empty"
    write(bot, 1_000_000)
    text = bot._stop_message(bot.store.session(100, 1_000_000), "bye")
    assert text.startswith("⏹️ Stopped (bye) · 1m30s · [listen](<https://rec.example/r/100/1000000/mix/")


async def test_opening_recordings_flushes_the_live_session_first(tmp_path):
    bot = make_bot(tmp_path)
    sent = []

    class Recorder(Live):
        async def flush(self):
            write(bot, self.session_ms)

    bot.recorders[100] = Recorder(2_000_000)
    response = type("R", (), {"send_message": lambda self, text, **kw: _append(sent, text)})()
    interaction = type("I", (), {
        "client": bot, "guild": Guild(), "user": object(), "response": response,
    })()
    await recordings.callback(interaction)
    assert "[All](<https://rec.example/r/100/2000000/mix/" in sent[0]


async def _append(sink, value):
    sink.append(value)

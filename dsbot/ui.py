from __future__ import annotations

from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .format import discord_time, human_duration, local_label
from .store import MIX_TRACK, Session

if TYPE_CHECKING:
    from .bot import RecordingBot

SELECT_LIMIT = 25
MESSAGE_BUDGET = 1900


def live_session(bot: RecordingBot, guild_id: int) -> int | None:
    recorder = bot.recorders.get(guild_id)
    return recorder.session_ms if recorder is not None else None


async def flush_live(bot: RecordingBot, guild_id: int) -> None:
    recorder = bot.recorders.get(guild_id)
    if recorder is not None:
        await recorder.flush()


def session_ids(bot: RecordingBot, guild_id: int) -> list[int]:
    ids = {s.session_ms for s in bot.store.sessions(guild_id)}
    live = live_session(bot, guild_id)
    if live is not None:
        ids.add(live)
    return sorted(ids, reverse=True)


def _mentions(user_ids) -> str:
    return ", ".join(f"<@{uid}>" for uid in user_ids)


def _link(label: str, url: str) -> str:
    clean = label.replace("[", "").replace("]", "") or "?"
    return f"[{clean}](<{url}>)"


def render(bot: RecordingBot, guild, session_ms: int | None) -> str:
    opted = bot.settings.opted_in(guild.id)
    count = len(session_ids(bot, guild.id))
    sessions = f"{count} session{'' if count == 1 else 's'}" if count else "No recordings"
    header = f"{sessions} · opted in: {_mentions(opted) if opted else 'nobody'}"
    if session_ms is None:
        return header
    footer = f"-# {round(100 * bot.store.total_bytes() / bot.cfg.max_disk_bytes)}% full"
    lines = [header]

    session = bot.store.session(guild.id, session_ms)
    recorder = bot.recorders.get(guild.id)
    live = recorder is not None and recorder.session_ms == session_ms
    duration = session.duration_ms if session else 0
    start = discord_time(session_ms, "f")
    when = (
        f"**{start}** 🔴 live in <#{recorder.channel_id}>"
        if live
        else f"**{start} → {discord_time(session.end_ms if session else session_ms, 't')}**"
    )
    lines.append(f"\n{when} · {human_duration(duration)}")
    if session is None or not session.mix:
        return "\n".join([*lines, "No audio.", footer])

    text = "\n".join(lines)
    links = [_link("All", bot.links.session_url(guild.id, session_ms, MIX_TRACK))]
    for user_id in session.speakers:
        link = _link(
            bot.short_name(guild, user_id),
            bot.links.session_url(guild.id, session_ms, str(user_id)),
        )
        if len(text) + len(" · ".join([*links, link])) + 3 > MESSAGE_BUDGET:
            break
        links.append(link)
    return f"{text}\n▶ {' · '.join(links)}\n{footer}"


def _option(bot: RecordingBot, guild, session_ms: int, session: Session | None, selected):
    live = live_session(bot, guild.id) == session_ms
    duration = session.duration_ms if session else 0
    people = len(bot.store.people(guild.id, session_ms))
    return discord.SelectOption(
        label=local_label(session_ms, bot.cfg.tz) + (" · live" if live else ""),
        value=str(session_ms),
        description=f"{human_duration(duration)} · {people} people",
        default=session_ms == selected,
    )


class RecordingsView(discord.ui.View):
    def __init__(self, bot: RecordingBot, guild, session_ms: int | None, *, admin: bool):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild = guild
        self.session_ms = session_ms
        self.admin = admin
        ids = session_ids(bot, guild.id)
        if len(ids) > 1:
            self.add_item(SessionSelect(self, ids[:SELECT_LIMIT]))
        deletable = (
            admin
            and session_ms is not None
            and live_session(bot, guild.id) != session_ms
            and bot.store.session(guild.id, session_ms) is not None
        )
        if deletable:
            self.add_item(DeleteButton(self))

    def content(self) -> str:
        return render(self.bot, self.guild, self.session_ms)

    def reopen(self, session_ms: int | None) -> RecordingsView:
        return RecordingsView(self.bot, self.guild, session_ms, admin=self.admin)


class SessionSelect(discord.ui.Select):
    def __init__(self, owner: RecordingsView, ids: list[int]) -> None:
        sessions = {s.session_ms: s for s in owner.bot.store.sessions(owner.guild.id)}
        super().__init__(
            placeholder="Session",
            options=[
                _option(owner.bot, owner.guild, sid, sessions.get(sid), owner.session_ms)
                for sid in ids
            ],
        )
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        await flush_live(self.owner.bot, self.owner.guild.id)
        view = self.owner.reopen(int(self.values[0]))
        await interaction.response.edit_message(content=view.content(), view=view)


class DeleteButton(discord.ui.Button):
    def __init__(self, owner: RecordingsView) -> None:
        super().__init__(label="Delete", style=discord.ButtonStyle.danger)
        self.owner = owner

    async def callback(self, interaction: discord.Interaction) -> None:
        confirm = ConfirmDelete(self.owner)
        await interaction.response.edit_message(
            content=f"Delete {discord_time(self.owner.session_ms, 'f')}? Permanent.",
            view=confirm,
        )


class ConfirmDelete(discord.ui.View):
    def __init__(self, owner: RecordingsView) -> None:
        super().__init__(timeout=120)
        self.owner = owner

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _button) -> None:
        bot, guild, session_ms = self.owner.bot, self.owner.guild, self.owner.session_ms
        if live_session(bot, guild.id) == session_ms:
            note = "Live, not deleted."
        else:
            bot.store.delete_session(guild.id, session_ms)
            bot.exporter.forget(guild.id, session_ms)
            note = f"🗑️ Deleted {discord_time(session_ms, 'f')}."
        ids = session_ids(bot, guild.id)
        view = self.owner.reopen(ids[0] if ids else None)
        await interaction.response.edit_message(content=f"{note}\n{view.content()}", view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _button) -> None:
        view = self.owner.reopen(self.owner.session_ms)
        await interaction.response.edit_message(content=view.content(), view=view)


@app_commands.command(name="recordings", description="Sessions and links")
@app_commands.guild_only()
async def recordings(interaction: discord.Interaction) -> None:
    bot: RecordingBot = interaction.client
    guild = interaction.guild
    await flush_live(bot, guild.id)
    ids = session_ids(bot, guild.id)
    permissions = getattr(interaction.user, "guild_permissions", None)
    view = RecordingsView(
        bot, guild, ids[0] if ids else None,
        admin=bool(permissions and permissions.manage_guild),
    )
    await interaction.response.send_message(view.content(), view=view, ephemeral=True)

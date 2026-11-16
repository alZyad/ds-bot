# ds-bot — opt-in Discord voice recorder

A Python bot that records the voices of people who **asked to be recorded**,
groups what they said into **sessions**, and serves each session as a
**private link** that plays in the browser — the whole conversation, or one
person's voice on its own.

```
/record-me ──▶ settings.json (opt-in, per server)
                    │
voice packets ──────┴─▶ only opted-in voices pass
        │
        ▼
   Mixer ─── one mixed mono frame + one frame per speaker, every 20 ms
        │
        ▼
   Segments ─── open on speech, close after 5 s of silence
        │
        ▼
   Chunks (60 s) ──▶ ffmpeg ──▶ data/recordings/<guild>/<session>/*.aac
        │
        ▼
   Retention ─── delete the oldest session while over MAX_DISK_MB
        │
        ▼
   https://…/r/<guild>/<session>/<track>/<signature>.m4a ──▶ join + remux ──▶ browser
```

Why each piece is the way it is: [`docs/DESIGN.md`](docs/DESIGN.md).

## Behaviour

| | How it works |
| --- | --- |
| Who is recorded | Nobody by default. `/record-me` opts you in for the server, `/stop-recording-me` opts you out. Both answer publicly. Other people's voices never reach the recorder, checked on every packet. |
| When it joins | A voice channel with **2+ people**, at least one of them opted in. One channel per server, first come first served, no hopping. |
| What it says | Short notices in the voice channel's chat: "🔴 Recording @A, @B. Nobody else. `/help`" on joining, "🔴 +@C" when someone opts in later, "⏹️ Stopped (reason) · 12m · listen" on leaving. While recording, the bot's status reads "🔴 Recording N people". |
| Silence | Segments close after `SILENCE_TIMEOUT` of silence; dead air is never stored. |
| Sessions | A session ends once the bot has been out of voice for `SESSION_GAP_SECONDS` (5 min). Rejoining sooner, or moving channel, continues it. |
| Storage | While the total exceeds `MAX_DISK_MB` (10 GiB), the oldest session is deleted whole. The newest session of a server is never deleted. |
| Listening | `/recordings` lists sessions with links: everyone, plus each voice. Links never expire; they stop working once the session is deleted. |

## Commands

| Command | What it does |
| --- | --- |
| `/record-me` | Opt in: you are recorded whenever you're in a voice channel with someone else. Public. |
| `/stop-recording-me` | Opt out. What was already recorded stays until it rotates out. Public. |
| `/recordings` | The latest session: when, how long, who, and links. A picker switches between the last 25 sessions; **Manage Server** also gets *Delete*. Only you see it. |
| `/help` | The commands and the rules, in four lines. Only you see it. |

## Links

The bot runs a small web server on `WEB_PORT` (8080). A link is

    PUBLIC_URL/r/<guild>/<session>/<mix|user id>/<signature>.m4a

signed with an HMAC keyed on the bot token, so links cannot be guessed and stay
valid until the session is deleted (or the token changes). Opening one joins the
session's chunks into an `.m4a` (no re-encode, cached in `data/exports`, pruned
after 24 h or past 1 GiB) and redirects to that immutable file, so seeking works
even while the session keeps growing. A snapshot URL that has since been pruned
redirects back to its session, so a copied address keeps working. A live session is flushed first, so the
last sentence is included.

The bot does not terminate TLS; put it behind a reverse proxy (Caddy:
`rec.example.org { reverse_proxy dsbot:8080 }`).

## Requirements

* Python 3.11+, `ffmpeg` on `PATH`, `libopus` and `libsodium`
  (Debian: `apt install ffmpeg libopus0 libsodium23`)
* In the Discord developer portal: the **Server Members Intent**; invite with
  scopes `bot` + `applications.commands` and permissions **View Channels**,
  **Connect**, **Send Messages**.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # put your token in it
python -m dsbot
```

Or with Docker:

```bash
docker build -t ds-bot .
docker run -d -e DISCORD_TOKEN=... -e PUBLIC_URL=https://rec.example.org \
  -v ds-bot-data:/data -p 8080:8080 ds-bot
```

Slash commands sync on startup; Ctrl/Cmd+R refreshes a Discord client that
doesn't show them yet.

## Configuration

Environment variables (or `.env`); [`.env.example`](.env.example) is annotated.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (required); also keys the link signatures |
| `PUBLIC_URL` | `http://localhost:8080` | Base of the links handed out |
| `WEB_PORT` | `8080` | Port of the link server |
| `TIMEZONE` | `Europe/Paris` | Time zone of the session picker labels and file names |
| `MIN_SPEAKERS` | `2` | People needed in a channel (one of them opted in) |
| `SILENCE_TIMEOUT` | `5.0` | Seconds of silence that end a segment |
| `SILENCE_RMS` | `150` | Loudness (0..32767) below which audio counts as silence |
| `LEAVE_GRACE` | `5.0` | Seconds before leaving a channel that stopped qualifying |
| `SESSION_GAP_SECONDS` | `300` | Time out of voice that ends a session |
| `MAX_DISK_MB` | `10240` | Recording budget across every server |
| `CHUNK_SECONDS` | `60` | Chunk length |
| `MAX_SPEAKER_TRACKS` | `6` | Speakers per conversation before per-voice tracks stop; `0` disables them |
| `AUDIO_BITRATE` | `64k` | AAC bitrate |
| `DATA_DIR` | `./data` | Recordings, exports and `settings.json` |
| `ANNOUNCE` | `true` | Post the join/leave notices |
| `INCLUDE_CHANNEL_IDS` / `EXCLUDE_CHANNEL_IDS` | — | Allow / deny lists of voice channel ids |

## Storage

Mono AAC at 64 kbps is about 29 MB per hour per track. A session records the mix
plus one track per speaker (up to `MAX_SPEAKER_TRACKS`), so 5 h with 5 people
talking is roughly 0.9 GB, and 10 GiB holds about a dozen such evenings. Only
speech counts: silence is never stored.

Per-voice tracks are aligned with the mix chunk by chunk. When a conversation
passes `MAX_SPEAKER_TRACKS` distinct speakers, the tracks already written are
kept and no new one opens until the next conversation; the mix always has
everyone who opted in.

## Recording people

Recording a conversation is regulated in many places, and Discord's developer
policy requires that users know. Here nobody is recorded without running
`/record-me`, every opt-in is public, and the voice channel is told who is being
recorded whenever the bot joins. Check what consent rules apply where you are.

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest
```

No Discord connection and no ffmpeg needed: `tests/fake_ffmpeg.py` stands in for
the encoder. `tests/test_ffmpeg_real.py` runs the real binary when installed.

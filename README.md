# ds-bot — a Discord voice recording buffer

A Python bot that watches every voice channel of a server, records the
conversation whenever **two or more people** are in one, keeps a **rolling
~3 hour buffer** of what was said, and hands it back as **mp3 in chat** on
demand.

```
voice packets (per user, 20 ms)
        │
        ▼
   Mixer ─── one mixed mono frame every 20 ms (wall-clock anchored)
        │
        ▼
   Segment state machine ─── opens on speech, closes after 5 s of silence
        │
        ▼
   Chunks (60 s of speech each) ──▶ ffmpeg ──▶ data/<guild>/<channel>/*.mp3
        │
        ▼
   Retention ─── drop the oldest audio once the buffer passes ~3 h
        │
        ▼
   /rec get ──▶ concatenate ──▶ upload to the text channel
```

## Behaviour

| Requirement | How it works |
| --- | --- |
| Detect how many people are in each audio channel | `on_voice_state_update` plus a 20 s reconciliation sweep count non-bot members of every voice/stage channel. |
| Record when 2+ people are present | The bot joins the busiest eligible channel and starts recording. Threshold is `MIN_SPEAKERS`. |
| Stop on 5 s of silence, resume as soon as someone talks | The silence that follows speech is *buffered*, not written. If someone speaks again within `SILENCE_TIMEOUT` the pause is replayed into the recording (natural rhythm kept); otherwise it is dropped and the segment is closed. The next word opens a new segment on the very next 20 ms frame — there is no restart latency, because the bot never actually leaves the channel. |
| Keep a ~3 h buffer, dropping old bits | Recording is written as 60 s mp3 chunks. After each chunk the retention policy deletes the oldest audio. See [Retention strategies](#retention-strategies). |
| A command to get the recordings as mp3 | `/rec get` concatenates the buffer of a channel and posts it, split into as many mp3 files as Discord's attachment limit requires. |

Everyone in the channel is mixed into **one** mono timeline, so an export is a
single conversation you can listen to, not one file per participant.

## Requirements

* Python 3.11+
* `ffmpeg` on `PATH` (does all mp3 encoding and concatenation)
* `libopus` and `libsodium` (installed with `discord.py[voice]` on most
  platforms; on Debian/Ubuntu: `apt install libopus0 libsodium23`)

In the [Discord developer portal](https://discord.com/developers/applications):

* enable the **Server Members Intent** (needed to see who sits in a voice
  channel). The message content intent is *not* needed.
* invite the bot with the scopes `bot` + `applications.commands` and the
  permissions **View Channels**, **Connect**, **Send Messages**,
  **Attach Files**.

## Run it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then put your token in it
python -m dsbot
```

Or with Docker (ffmpeg included):

```bash
docker build -t ds-bot .
docker run --rm -e DISCORD_TOKEN=... -v ds-bot-data:/data ds-bot
```

Slash commands are synced on startup and may take a minute to appear.

## Commands

| Command | What it does |
| --- | --- |
| `/rec get [channel] [minutes]` | Post the buffered recording as mp3. Defaults to the channel you are in and the whole buffer. Splits into `part01…partNN` when it does not fit one attachment. |
| `/rec status` | What is being recorded right now, how much is buffered per channel, who is where. Ephemeral. |
| `/rec list [channel]` | The conversations currently in the buffer, with their timestamps and durations. Ephemeral. |
| `/rec purge [channel]` | Delete a channel's buffer. Requires **Manage Server**. |

`/rec get` flushes the in-progress chunk first, so it always includes the
sentence that just finished.

## Retention strategies

The buffer is a **ring of small files**, not one big file: audio is written as
`CHUNK_SECONDS` (60 s by default) mp3 chunks named
`c<start>_d<duration>_s<segment>.mp3`, and expiring audio means `unlink`-ing
the oldest chunk. That choice is what makes the three policies below cheap —
each one only decides *which* chunks to drop, in `plan_trim()`
(`dsbot/store.py`), a pure function with unit tests.

Set `RETENTION_STRATEGY` to pick one:

### `oldest-chunk` (default)

Drop the oldest chunk until the buffer is back under `RETENTION_SECONDS`.

* Overshoot is at most one chunk, so the buffer sits between 2 h 59 m and 3 h.
* One `unlink` per minute of recording: negligible IO.
* Trade-off: the oldest surviving conversation may start mid-sentence. Nothing
  large is ever lost — you lose a minute at the far end of the window.
* Tighten or loosen the fluctuation with `CHUNK_SECONDS`: 30 s chunks halve the
  swing, 5 min chunks quarter the number of files.

### `oldest-segment`

Drop whole conversations, oldest first, never a partial one.

* What you keep is always a set of complete discussions — good if you export to
  hand recordings to people, since no file ever begins mid-word.
* Trade-off: the buffer swings by the length of a whole conversation. After
  dropping a 40 min meeting you are at 2 h 20 m, not 3 h. It also cannot help
  with a single meeting longer than the retention target, so it falls back to
  chunk granularity inside the last remaining segment.

### `high-water`

Do nothing until the buffer exceeds `RETENTION_SECONDS + HIGH_WATER_SLACK`,
then trim down to `RETENTION_SECONDS - LOW_WATER_SLACK` in one pass.

* Fewest delete operations, and trimming happens in bursts instead of every
  minute — the friendliest option for network storage or a spinning disk.
* Trade-off: the widest fluctuation. With the default 15 min slacks the buffer
  moves between 2 h 45 m and 3 h 15 m.

### Considered and rejected

* **One rolling file, truncate the head.** Dropping the first minute of a single
  long mp3 means rewriting the whole file: O(buffer) IO every minute, plus mp3
  frame-boundary and ID3 problems. The chunk ring gets the same result with one
  `unlink`.
* **Fixed ring of N preallocated slots.** Bounds disk exactly, but overwriting a
  slot while an export is reading it is a race, and mp3 is variable size anyway
  so "N slots" does not actually bound the duration.
* **Wall-clock window** (keep everything from the last 3 hours) instead of a
  duration window (keep 3 hours *of speech*). Simpler, but a channel used
  10 minutes per hour would retain only ~30 minutes of discussion. Since silence
  is never stored, the duration window is what "3 hours of recordings" actually
  means.
* **Byte quota per channel.** Easy disk planning, but with variable bitrate you
  no longer know how much conversation you are keeping.

### Natural extension

**Tiered quality.** Nothing in the layout stops you from re-encoding chunks
older than an hour down to 24 kbps mono: chunks are independent files, and the
duration lives in the filename, so a background task could triple the retained
history at the same disk cost. Not implemented, because it trades CPU for
history and the default 3 h already fits in ~85 MB per channel.

## Storage

Audio is mixed to 48 kHz **mono** and encoded straight into ffmpeg's stdin —
raw PCM never touches the disk.

| Format | Per hour of speech | 3 h buffer |
| --- | --- | --- |
| 48 kHz stereo PCM (what Discord sends) | 691 MB | 2.0 GB |
| 48 kHz mono PCM | 346 MB | 1.0 GB |
| **mp3 64 kbps mono (default)** | **29 MB** | **86 MB** |
| mp3 32 kbps mono | 14 MB | 43 MB |

Only *speech* counts: an idle channel costs nothing.

## Configuration

Every value is read from the environment (or a `.env` file); see
[`.env.example`](.env.example) for the full annotated list.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (required) |
| `MIN_SPEAKERS` | `2` | People needed before recording starts |
| `SILENCE_TIMEOUT` | `5.0` | Seconds of silence that end a segment |
| `SILENCE_RMS` | `150` | Amplitude gate; `0` trusts Discord's own voice detection only |
| `RETENTION_SECONDS` | `10800` | Target buffer per channel (3 h) |
| `RETENTION_STRATEGY` | `oldest-chunk` | `oldest-chunk` / `oldest-segment` / `high-water` |
| `CHUNK_SECONDS` | `60` | Chunk size, i.e. deletion granularity |
| `HIGH_WATER_SLACK` / `LOW_WATER_SLACK` | `900` | Band for `high-water` |
| `MP3_BITRATE` | `64k` | Encoder bitrate |
| `MAX_UPLOAD_MB` | `9.0` | Attachment cap; the guild's real limit wins if lower |
| `DATA_DIR` | `./data` | Where chunks and temporary exports live |
| `ANNOUNCE` | `true` | Post a notice in the channel when recording starts/stops |
| `INCLUDE_CHANNEL_IDS` / `EXCLUDE_CHANNEL_IDS` | — | Allow / deny lists of voice channel ids |

## Design notes

* **Wall-clock alignment.** The pump asks the mixer for exactly one frame per
  20 ms of real time, anchored to a monotonic clock, so a busy event loop does
  not stretch the recording. If it ever falls more than a second behind it
  re-anchors instead of spinning to catch up, and counts the event
  (`late_resyncs` in `/rec status`).
* **Bounded memory.** Per-user packet buffers hold 400 ms; a client that floods
  drops its *oldest* audio rather than growing without limit.
* **Crash safety.** A chunk being written is a `.part` file, invisible to
  readers, and becomes visible by an atomic rename once ffmpeg has flushed it.
  Leftovers from a crash are swept at startup. There is no index to corrupt —
  timing metadata lives in the filenames.
* **One channel per server at a time.** Discord allows a bot one voice
  connection per guild. The bot picks the busiest eligible channel and then
  *stays there* while it remains eligible, even if another channel gets busier —
  hopping would cut the conversation already being captured. To cover several
  channels of one server simultaneously, run several bot applications with
  complementary `INCLUDE_CHANNEL_IDS`.
* **Silence is never stored**, so a 3 h buffer is 3 h of actual talking.
* **Chunk-boundary padding.** Concatenating mp3 files adds up to one encoder
  frame (~26 ms) of padding per boundary, so a 3 h export can run a few seconds
  longer than the sum of its chunks. Audible content is unaffected. Larger
  `CHUNK_SECONDS` reduces it; switching the chunk format to Opus in an Ogg
  container would remove it, at the cost of Discord not previewing the file.

## Recording people

Recording a conversation is regulated in many places, and Discord's own
developer policy requires that users know. `ANNOUNCE=true` (the default) posts a
visible notice in the channel when recording starts and stops, and the bot
appears in the member list of the voice channel while it records. Keep the
notice on, tell your members, and check what consent rules apply where you are.

## Tests

```bash
pip install pytest pytest-asyncio
python -m pytest
```

The suite covers the mixer, the segment/silence state machine, chunk rotation,
all three retention strategies, export splitting, and the
"how many people are in this channel" logic. It needs no Discord connection, and
no ffmpeg either: `tests/fake_ffmpeg.py` stands in for the encoder, so the whole
pipeline is exercised end to end.

`tests/test_ffmpeg_real.py` runs the same path against the actual binary and is
skipped when ffmpeg is missing — a stub cannot catch a wrong ffmpeg invocation.

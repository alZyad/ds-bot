# ds-bot — a Discord voice recording buffer

A Python bot that watches every voice channel of a server, records the
conversation whenever **two or more people** are in one, keeps a rolling buffer
of what was said, and hands it back as **playable audio in chat** on demand —
the mixed conversation, or each person's voice on its own.

```
voice packets (per user, 20 ms)
        │
        ▼
   Mixer ─── one mixed mono frame + one frame per speaker, every 20 ms
        │     (wall-clock anchored, peak-limited mix)
        ▼
   Segment state machine ─── opens on speech, closes after 5 s of silence
        │
        ▼
   Chunks (60 s each) ──▶ ffmpeg ──▶ data/recordings/<guild>/<channel>/*.aac
        │                            one file per track: mix + each speaker
        ▼
   Retention ─── delete the oldest chunk while the buffer exceeds MAX_DISK_MB
        │
        ▼
   /rec get ──▶ join ──▶ remux to .m4a ──▶ upload, one file per conversation
```

The full rationale, including the alternatives that were rejected, is in
[`docs/DESIGN.md`](docs/DESIGN.md).

## Behaviour

| Requirement | How it works |
| --- | --- |
| Detect how many people are in each audio channel | `on_voice_state_update` plus a 20 s reconciliation sweep count non-bot members of every voice/stage channel. |
| Record when 2+ people are present | The bot joins the first channel to become eligible and stays there. Threshold is `MIN_SPEAKERS`. |
| Stop on 5 s of silence, resume as soon as someone talks | The silence that follows speech is *buffered*, not written. If someone speaks again within `SILENCE_TIMEOUT` the pause is replayed into the recording (natural rhythm kept); otherwise it is dropped and the segment is closed. The next word opens a new segment on the very next 20 ms frame — there is no restart latency, because the bot never actually leaves the channel. |
| Keep a bounded buffer, dropping old bits | Audio is written as 60 s chunks. Whenever the total exceeds `MAX_DISK_MB` the oldest chunk is deleted. One rule, no strategies to choose between. |
| A command to get the recordings in chat | `/rec get` posts the mixed recording, one file per conversation. `/rec get-all` adds every speaker's isolated track; `/rec get-single` gives you a dropdown to pick one person. |

## Requirements

* Python 3.11+
* `ffmpeg` on `PATH` (does all encoding and remuxing)
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
| `/rec get [channel] [minutes]` | The mixed recording, one `.m4a` per conversation. Defaults to the channel you are in and the whole buffer. |
| `/rec get-all [channel] [minutes]` | The mix plus every speaker's isolated track. Asks for confirmation past 10 files. |
| `/rec get-single [channel] [minutes]` | A dropdown of who has audio buffered; pick one and get only their voice. |
| `/rec status` | What is being recorded, the live loudness reading, how much is buffered. Ephemeral. |
| `/rec list [channel]` | The conversations in the buffer with timestamps and sizes — one file each when exported. Ephemeral. |
| `/rec config [channel] [silence_rms] [reset]` | Read or set the silence gate for a channel. Ephemeral. |
| `/rec purge [channel]` | Delete a channel's buffer. Requires **Manage Server**. |

`/rec get*` flushes the in-progress chunk first, so it always includes the
sentence that just finished.

**There is no permission check on the `get` commands.** That is deliberate for a
small private server: anyone can pull any channel's audio. If your members are
not all mutually trusted, gate them on `view_channel` + `connect` for the target
channel so people can only pull audio from rooms they could have walked into.

## Retention

One rule:

> While the buffer is larger than `MAX_DISK_MB`, delete the oldest chunk.

The buffer is a **ring of small files**, not one big file, which is what makes
that cheap: expiring audio is an `unlink`, not a rewrite. Chunks sharing a start
time form a *group* — the mix plus the speaker tracks covering that moment — and
a group is always deleted whole, so a speaker track can never outlive its mix.

The last remaining group is never deleted, so a budget smaller than one chunk
degrades to "hold one chunk" rather than to an empty buffer.

The cost of chunk-granularity deletion is that the oldest file may begin
mid-sentence. That is cosmetic, and it only ever affects the oldest audio.
Several cleverer schemes were tried on paper first and rejected — a speech-
duration window, a "never drop below N hours" floor, a segment count cap, and
whole-segment deletion; [`docs/DESIGN.md`](docs/DESIGN.md#retention) explains
why each one is worse.

## Storage

Audio is mixed to 48 kHz **mono** and encoded straight into ffmpeg's stdin — raw
PCM never touches the disk. Chunks are **AAC in an ADTS stream**, remuxed to
`.m4a` on export.

AAC because it plays in VLC and Windows Media Player alike and is roughly a
third smaller than mp3 at the same quality. ADTS because it is self-framing, so
joining chunks is byte concatenation plus one stream-copy remux — nothing is
re-encoded on the way out.

| | Per hour of talking | 1 GiB budget |
| --- | --- | --- |
| 48 kHz stereo PCM (what Discord sends) | 691 MB | 1.5 h |
| **AAC 64 kbps, mix only** | **29 MB** | **~36 h** |
| AAC 64 kbps, mix + 6 speaker tracks | 86 MB | ~12 h |

Only *speech* counts: an idle channel costs nothing. Quality is capped upstream
— Discord encodes each microphone to roughly 64 kbps Opus before it reaches us,
so spending more bits here preserves detail that is already gone.

## Isolated tracks

Alongside the mix, each speaker gets their own file while a conversation has no
more than `MAX_SPEAKER_TRACKS` (6) distinct speakers. Distinct *speakers*, not
channel occupants: a track only exists because somebody talked, so a 10-person
channel where 3 people speak gets 3 tracks.

Alignment is free. The recorder pulls exactly one frame per 20 ms of real time
and feeds every track from that same pull, so all of them share one
sample-accurate timeline. A speaker first heard part-way through a chunk gets
silence back-filled to the chunk start; if the speaker limit is exceeded
mid-chunk the open tracks are frozen with silence to the end of it. Either way,
every track in a chunk is exactly as long as that chunk's mix.

When the limit is exceeded, what was already captured is kept and no further
track is opened for the rest of the conversation. Tracks are reconsidered from
scratch for the next one. The mix always records everyone regardless.

The mix is peak-limited: adding several voices together can exceed full scale,
and chopping the peaks off — which is what a plain 16-bit sum does — sounds like
crackling exactly during the crosstalk you most want to replay. Isolated tracks
are never summed with anything, so they are always an unlimited copy.

## Configuration

Every value is read from the environment (or a `.env` file); see
[`.env.example`](.env.example) for the full annotated list.

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_TOKEN` | — | Bot token (required) |
| `MIN_SPEAKERS` | `2` | People needed before recording starts |
| `SILENCE_TIMEOUT` | `5.0` | Seconds of silence that end a segment |
| `SILENCE_RMS` | `150` | Default amplitude gate; override per channel with `/rec config` |
| `LEAVE_GRACE` | `5.0` | Seconds to wait before leaving a channel that emptied |
| `MAX_DISK_MB` | `1024` | The whole buffer budget, across every channel |
| `CHUNK_SECONDS` | `60` | Chunk size, i.e. deletion granularity |
| `MAX_SPEAKER_TRACKS` | `6` | Speakers per conversation before isolated tracks stop; `0` disables them |
| `AUDIO_BITRATE` | `64k` | AAC bitrate |
| `MAX_UPLOAD_MB` | `9.0` | Attachment cap; the guild's real limit wins if lower |
| `MERGE_GAP_SECONDS` | `120` | Conversations closer than this merge into one export file |
| `DATA_DIR` | `./data` | Where chunks, settings and temporary exports live |
| `ANNOUNCE` | `true` | Post a notice in the channel when recording starts/stops |
| `INCLUDE_CHANNEL_IDS` / `EXCLUDE_CHANNEL_IDS` | — | Allow / deny lists of voice channel ids |

`SILENCE_RMS` per-channel overrides and the speaker name cache live in
`DATA_DIR/settings.json`, outside the recordings, so `/rec purge` cannot destroy
a tuned threshold.

## Design notes

* **Wall-clock alignment.** The pump asks the mixer for exactly one frame per
  20 ms of real time, anchored to a monotonic clock, so a busy event loop does
  not stretch the recording. If it ever falls more than a second behind it
  re-anchors instead of spinning to catch up, and counts the event.
* **Bounded memory.** Per-user packet buffers hold 400 ms; a client that floods
  drops its *oldest* audio rather than growing without limit.
* **Crash safety.** A chunk being written is a `.part` file, invisible to
  readers, and becomes visible by an atomic rename once ffmpeg has flushed it.
  Leftovers from a crash are swept at startup. There is no index to corrupt —
  all timing metadata lives in the filenames.
* **One channel per server at a time.** Discord allows a bot one voice
  connection per guild. The bot follows the *first* channel to become eligible
  and stays there while it remains so, even if another gets busier — hopping
  would cut the conversation already being captured. A second busy channel is
  therefore **not recorded**; to cover several at once, run several bot
  applications with complementary `INCLUDE_CHANNEL_IDS`.
* **A blip does not sever the recording.** Dropping below `MIN_SPEAKERS` starts
  a `LEAVE_GRACE` countdown rather than an immediate disconnect, so a reconnect
  or a channel move does not split the conversation in two.
* **Silence is never stored**, so the buffer holds actual talking.
* **Chunk-boundary padding.** Joining AAC chunks adds about one encoder frame
  (~27 ms) of padding per boundary, so a long export runs slightly longer than
  the sum of its chunks — roughly 20 s over a 12 h buffer of 60 s chunks.
  Audible content is unaffected. A larger `CHUNK_SECONDS` reduces it.
* **The silence gate is not optional.** `voice-recv` delivers packets whenever a
  client transmits, so an always-open microphone in a noisy room transmits
  continuously. Without the RMS gate the 5 s rule would never fire and every
  session would be one unbroken segment of mostly dead air.

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

The suite covers the mixer and limiter, the segment/silence state machine, chunk
rotation, track alignment and the speaker limit, retention, export splitting and
segment merging, the settings store, and the "how many people are in this
channel" logic. It needs no Discord connection, and no ffmpeg either:
`tests/fake_ffmpeg.py` stands in for the encoder, so the whole pipeline is
exercised end to end.

`tests/test_ffmpeg_real.py` runs the same path against the actual binary and is
skipped when ffmpeg is missing — a stub cannot catch a wrong ffmpeg invocation,
and that has already cost this project one shipped bug.

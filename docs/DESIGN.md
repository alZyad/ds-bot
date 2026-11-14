# Design

The decisions behind the recorder, and why. Written after a design review that
walked the whole tree, and kept as the record of *why* each choice was made —
`README.md` describes what the bot does, this describes what was rejected.

Status: **implemented.**

## Recording

**Opt-in, per person, per server.** Nobody is recorded until they run
`/record-me`; `/stop-recording-me` undoes it. Both replies are public, so the
server sees who is recorded. The filter sits on every voice packet and reads
the live opt-in list, so opting out takes effect mid-sentence and a
non-consenting voice never reaches the mixer, the disk or the merged file.

Rejected: recording everyone and filtering on export. The merged file cannot be
un-mixed, so consent has to be enforced before mixing.

**One channel per server, first come first served.** Discord allows a bot one
voice connection per guild, so a busy server can have a conversation we do not
capture. We join the first channel that becomes eligible and stay there until
it empties, rather than hopping to whichever channel is busiest — hopping would
cut the conversation already in progress, and that is the loss that matters.

**Eligible means 2 or more humans, at least one opted in** (bots and ourselves
excluded). One opted-in person alone is a monologue, not a conversation.

**No pre-roll.** We are not connected at one person, so joining costs a second
or two of the opening. Accepted: losing the first few seconds is cheap.

**5s grace before leaving.** When headcount drops to 1 we wait 5s before
disconnecting, so a reconnect, a channel move or someone reseating a headset
does not sever the recording and split the segment.

## Silence and segments

A *segment* is a stretch of actual conversation. It opens on the first voiced
frame and closes after 5s of continuous silence. Silence that ends a segment is
buffered and then discarded, so a natural pause mid-sentence stays inside the
segment but dead air is never stored.

*Chunks* are 60s slices of a segment. They are the unit of retention.

**Voiced means someone is transmitting AND mixed RMS >= threshold.** The RMS
gate is not optional: `voice-recv` delivers packets whenever a client
transmits, so an always-open mic in a noisy room transmits continuously and
without the gate the 5s rule would never fire — every session would be one
unbroken segment of mostly dead air.

The threshold defaults to 150 on a 0..32767 scale and is set with `SILENCE_RMS`.
A per-channel runtime override existed and was dropped with the `/rec` commands.

- `0` records everything including room noise
- `150` default
- `400` only clear speech; soft talkers get clipped

## Audio

**AAC 64kbps mono in `.m4a`.** Opus is better per byte but legacy Windows Media
Player will not open it; AAC plays in both VLC and WMP and is ~30% smaller than
mp3 at equal quality. Mono is not a compromise — each user's Opus stream is
mono at the source and is upmixed to stereo for delivery, so keeping stereo
would double storage for zero added information.

Quality is capped upstream: Discord encodes each mic to roughly 64kbps Opus
before it reaches us. Spending more bits preserves detail that is already gone.
64kbps AAC is transparent relative to that input.

**Soft limiter, not clipping.** Summing several voices can exceed full scale.
The mixer mixes with headroom and attenuates only the moments that would
overflow, easing back afterwards, instead of chopping the peaks — chopping
sounds like harsh crackling and it happens exactly during the crosstalk you
most want to replay. Only the mixed track is affected; per-speaker tracks are
never summed with anything.

## Per-speaker tracks

The mixed track is always written. Per-speaker tracks are written alongside it
when the segment has **6 or fewer distinct speakers**.

Distinct *speakers*, not channel occupants: a track only exists because someone
talked, so occupancy is the wrong denominator. A 10-person channel where 3
people talk gets 3 tracks.

If a 7th distinct speaker talks mid-segment, per-speaker recording **stops** and
the tracks already written are kept. Deleting them would throw away good audio;
continuing without the newcomer would produce tracks that silently omit a
participant, which is worse than none because you cannot tell by looking.

The open tracks are *frozen* rather than closed: they keep receiving silence to
the end of the chunk in progress, and no new one is opened for the rest of the
segment. Closing them on the spot would leave them shorter than the mix for that
chunk, and because export can merge two segments into one file, a short chunk
would shift every later frame of that speaker earlier than the mix it is meant
to line up with.

Alignment is free: the recorder runs a wall-clock pump emitting exactly one 20ms
frame per 20ms of real time, so every track fed from that pump is sample-aligned
by construction.

## Sessions

A *session* is everything recorded in a server between the bot joining voice and
the bot having been out of voice for 5 minutes. Moving channel, a reconnect or a
short break keeps the session. The leave time is tracked in memory; after a
restart the end of the newest chunk stands in for it.

Sessions are what people ask for ("last night's call"), so they are the unit of
listing, linking and deletion.

## Retention

**Delete the oldest whole session while total size exceeds `MAX_DISK_MB`
(10 GiB).** The newest session of each server is never deleted, so the session
being recorded cannot vanish under the recorder.

Sizing: 5 h of 5 people talking is about 0.9 GB (mix plus five tracks), so the
live session and the previous one together stay under 2 GB, far below the cap.

This replaces the earlier chunk-granular ring buffer. Whole-session deletion was
rejected then for its cliffs, one deletion wiping half of a 1 GB history; at
10 GiB a session is a small fraction of the budget, and a session that starts
mid-sentence is worse than one that is gone.

## Delivery

**Links, not attachments.** A server's upload limit is 10 MB at boost tier 0,
which is about 20 minutes of mix; a session is hours. The bot runs an aiohttp
server and hands out

    PUBLIC_URL/r/<guild>/<session>/<mix|user id>/<signature>.m4a

The signature is an HMAC over the path, keyed on a hash of the bot token: no
link database, nothing to guess, and no expiry. A link dies when its session is
deleted (404). Rotating the token invalidates every link, which is acceptable.

Opening a link flushes the live recorder if the session is live, joins the
chunks into an `.m4a` (stream copy, `+faststart` so playback starts before the
download ends) and redirects to an immutable snapshot named after the chunk
count. Browsers seek with Range requests on the snapshot URL, so a session that
grows mid-playback does not shift bytes under the player. Snapshots are cached
in `data/exports` and pruned past 24 h or 1 GiB.

## Commands

| Command | Behaviour |
| --- | --- |
| `/record-me` | opt in, announced publicly |
| `/stop-recording-me` | opt out, announced publicly; past audio rotates out |
| `/recordings` | ephemeral: latest session, links, a picker for the last 25, Delete for Manage Server |

**No permission gate on listening.** Deliberate for a small private server: any
member can open `/recordings`. Deleting a session requires Manage Server and a
confirmation.

**Names**: keyed by Discord user ID on disk, resolved live at render time,
falling back to a JSON name cache written when a speaker is first heard.

## Consent

Only people who opted in are recorded, every opt-in and opt-out is public, and
whenever the bot joins a channel it names who is being recorded in that
channel's chat, adding anyone who opts in later. Recording people without their
knowledge is a legal question in many jurisdictions, not merely a courtesy.

## Implementation notes

Decisions taken while writing the code, recorded so they can be revisited.

**Chunks are ADTS AAC (`.aac`), remuxed to `.m4a` on export.** ADTS is a raw
framed stream, so joining chunks is byte concatenation followed by a single
stream-copy remux — no re-encode anywhere on the export path. Measured cost:
about 27ms of encoder padding per chunk boundary (three 2s chunks concatenate to
6.08s), which over a 12h buffer of 60s chunks accumulates to roughly 20 seconds
of drift against wall clock. Accepted; the alternative is re-encoding every
export.

**`-f adts` is load-bearing.** Chunks are written to a `.part` file and renamed,
so ffmpeg cannot infer the container from the extension. Without the explicit
flag it exits 234 with "Unable to choose an output format" and every chunk
silently fails to encode. The same trap previously shipped as a bug with mp3.

**One directory per session, the track encoded in the filename:**
`recordings/<guild>/<session_ms>/c<start_ms>_d<duration_ms>_s<segment_ms>_t<mix|user_id>.aac`,
plus `people.txt` listing who spoke. One glob and one regex read a session;
retention is one `rmtree`.

**Chunk rotation is synchronised across every open encoder**, so all tracks in a
group share a start and duration. This is an invariant worth keeping: every
track in a chunk is exactly as long as that chunk's mix, no matter when its
speaker started talking or when the speaker limit was hit.

**Speaker tracks are written with silence** for the frames that speaker is
quiet. This is what makes the tracks timeline-aligned with each other and with
the mix, at the cost of storing silence. AAC makes that cheap but not free.

**The limiter has no lookahead beyond the current frame.** Attack applies the
full required gain to the whole 20ms frame, which guarantees the frame cannot
clip; release creeps back over roughly 200ms. The worst artefact is a 20ms gain
step at the onset of crosstalk, which is inaudible next to the clipping it
replaces. Fast path: when the summed peaks of all tracks cannot exceed full
scale, the mix is a straight `audioop.add` with no limiting work at all.

**Settings live in `settings.json` beside the recordings, not inside them**, so
deleting a session cannot lose the opt-in list or the speaker name cache.

**Environment variables changed.** `MP3_BITRATE` became `AUDIO_BITRATE`;
`RETENTION_SECONDS`, `RETENTION_STRATEGY`, `HIGH_WATER_SLACK`, `LOW_WATER_SLACK`,
`MAX_UPLOAD_MB`, `MERGE_GAP_SECONDS` and `AUTOJOIN_DEFAULT` are gone;
`SESSION_GAP_SECONDS`, `PUBLIC_URL`, `WEB_PORT` and `TIMEZONE` are new.
`MAX_DISK_MB` now defaults to 10240 and `MAX_SPEAKER_TRACKS` to 6.

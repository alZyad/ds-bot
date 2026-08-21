# Design

The decisions behind the recorder, and why. Written after a design review that
walked the whole tree; it supersedes the original `README.md` description
wherever the two disagree.

Status: **agreed, not yet implemented.** The code currently implements the
superseded design (mp3, 3h speech-duration buffer, per-channel retention,
mixed track only).

## Recording

**One channel per server, first come first served.** Discord allows a bot one
voice connection per guild, so a busy server can have a conversation we do not
capture. We join the first channel that becomes eligible and stay there until
it empties, rather than hopping to whichever channel is busiest — hopping would
cut the conversation already in progress, and that is the loss that matters.

**Eligible means 2 or more humans** (bots and ourselves excluded).

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

The threshold defaults to 150 on a 0..32767 scale and is configurable per
channel via `/rec config`, persisted to JSON in the data directory, because a
gaming channel with open mics needs a very different gate from a call.

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
the partial tracks already written are kept. Deleting them would throw away good
audio; continuing without the newcomer would produce tracks that silently omit a
participant, which is worse than none because you cannot tell by looking.

Alignment is free: the recorder runs a wall-clock pump emitting exactly one 20ms
frame per 20ms of real time, so every track fed from that pump is sample-aligned
by construction.

## Retention

**Delete the oldest 60s chunk while total size exceeds `MAX_DISK_MB` (1GB).**

That is the entire rule. 1GB holds roughly 12 hours of talking with mixed plus
six speaker tracks.

Rejected along the way:

- *A 3h buffer of speech duration.* Per-channel and unbounded in total; a busy
  server could hold 30h.
- *"Keep at least 3h, only drop a whole segment if what remains still exceeds
  3h."* Elegant — it guarantees a floor and never cuts mid-sentence — but
  segments are unbounded, so a single marathon segment means nothing is ever
  deleted, and capping segment length to fix that reintroduces the mid-sentence
  cuts the rule existed to avoid.
- *A limit of N segments.* Segment length varies by three orders of magnitude,
  so 10 segments is anywhere from 5 minutes to 50 hours. It bounds neither
  storage nor history.
- *Whole-segment deletion under a disk cap.* Cliffs: if 1GB is two large
  segments, one deletion wipes half the history.

Chunk deletion costs only that the oldest file may begin mid-sentence, which is
cosmetic and only ever affects the oldest audio.

## Export

**One file per segment**, named by time. Segments separated by less than 2
minutes are merged into one file — the gap is silence that was never stored, and
without merging a chatty 12h buffer becomes 50+ attachments.

A segment larger than the server's upload limit is split; nothing else is.

Discord's limits are the *server's*, not the uploader's — Nitro does not help a
bot. 10MB by default, 50MB at boost level 2, 100MB at level 3, 10 attachments
per message. Read at runtime from `guild.filesize_limit` rather than hardcoded.

## Commands

| Command | Behaviour |
| --- | --- |
| `/rec get` | the mixed recording |
| `/rec get-all` | mixed plus every speaker track; confirms first past 10 files |
| `/rec get-single` | dropdown of who has audio, pick one |
| `/rec status` | what is being recorded, live RMS, how much is buffered |
| `/rec list` | the conversations in the buffer |
| `/rec purge` | delete a channel's buffer (requires Manage Server) |
| `/rec config` | read or set the silence threshold for a channel |

**No permission gate except on `purge`.** Deliberate: this runs on a small
private server among friends. On any server where the members are not all
mutually trusted, `get`/`get-all`/`get-single` should be gated on `view_channel`
plus `connect` for the target channel, so you can only pull audio from rooms you
could have walked into.

**Names in the dropdown**: keyed by Discord user ID on disk, resolved live to
`Display Name (@handle)` at render time, falling back to a JSON name cache
written when a speaker's first chunk lands — otherwise anyone who leaves the
server shows as a bare number for as long as their audio is buffered.

## Consent

The bot announces when it starts and stops recording in the channel it is
recording. Recording people without their knowledge is a legal question in many
jurisdictions, not merely a courtesy; the announcement is on by default and
turning it off is the operator's decision and responsibility.

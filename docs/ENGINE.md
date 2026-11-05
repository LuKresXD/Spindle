# How Spindle decides what is playing

One `Player.tick()` every 2 s. Nothing in the tick waits on the network.

```
arecord ──► ring buffer ──► level (RMS of the last 2 s)
                 │
                 └──► 6 s WAV window ──► IdentifyWorker (thread)
                                            │  AcoustID → Shazam
                                            │  Resolver (Spotify + Discogs collection)
                                            │  cover art (Apple → Deezer → Spotify)
                                            ▼
                                     IdentifyResult(at = when the audio ended)
                                            │
tick ◄──────────────────────────────────────┘ applied on the next tick
  │
  ├── ScrobbleSession (album lock / compilation)
  ├── Scrobbler outbox (thread) ──► Last.fm, offline queue
  └── ScreenState ──► Display
```

## Phases

| phase | meaning | screen |
|---|---|---|
| IDLE | quiet (below `silence.threshold_db`) | black |
| LISTENING | music, nothing identified yet | dim word + level bar |
| PLAYING | we know the track | cover, tracklist, progress |

Starting a session needs `threshold + start_margin_db` for `start_ticks`
consecutive ticks (run-out groove noise sits at the threshold). Ending one
needs `min_silence_seconds` below the plain threshold. A dead capture
(arecord gone) holds the state instead of counting as silence.

## Resolution: which album?

A fingerprint says *artist, title*. The same recording is on the album, a
single, a few compilations and a deluxe edition. The `Resolver` ranks the
Spotify candidates by:

1. title and artist actually match
2. **the album is in the Discogs collection** (the record on the platter)
3. the album the session is already locked onto
4. album > single > compilation; standard edition > deluxe / remaster
5. Spotify's own order

If the chosen album is owned, the tracklist is **merged** with the pressing:
Discogs order and positions (`A1 … D3`), Spotify names and durations. If
Spotify has nothing but the collection has a release with that track, the
**Discogs tracklist alone** carries the album lock (white labels, local
pressings, anything streaming never got).

The collection loads on its own thread at start, refreshes daily, and
prefetches every release's tracklist once (cached on disk). Lookups from the
engine are memory-only.

## Session rules

* **Confirmation gate**: a track must be identified twice before it changes
  anything. One-off false matches never do.
* **Album lock**: first confirmed track with a known album. Backfills what
  played before the first ID by elapsed time; forward-fills gaps between
  anchors; advances by duration (+3 % drift, 3–15 s buffer).
* **Clock-bounded fill**: a gap track is scrobbled only when the elapsed
  time since the current track started had room for it in full; the first
  track that does not fit is scrobbled only if it played enough, the rest
  never played. The record need not follow the tracklist (a bootleg cut, a
  skipped track, the needle moved): Graduation, 2026-11-04, scrobbled two
  tracks the pressing does not have before this rule existed.
* **Backwards anchor**: a track identified before the predicted one is a
  lagging identification of the track that just ended, so its original
  start is kept and the timing model simply re-advances. If that track
  started more than its duration plus 45 s ago it has played in full:
  the needle went back, the session is replaying, and that track and
  everything after it may scrobble again (the second play of a side is a
  real play).
* **Side aware**: when the pressing is known, timing stops at the last track
  of a side (the run-out) and waits for the flip; backfill never crosses a
  side boundary.
* **Re-lock**: the session moves to a better tracklist when one appears
  (the owned pressing once the collection loaded, the album after a lock on
  a single, the edition that actually has the track playing). What was
  scrobbled carries over by title. An owned pressing is never traded for a
  streaming edition.
* **Same-artist mismatch** (a title not on the locked album) is ignored:
  catalogue confusion, not a compilation.
* **Compilation mode**: two consecutive confirmed tracks by *other* artists.
  Each track is scrobbled when it ends (next track or silence) if it played
  half its length or 4 minutes, with its real start time.
* **Simple mode**: no album from any source. Scrobble by elapsed time; the
  needle lift finalises the track.
* **Early end** (needle lift, silence): Last.fm's half-or-4-minutes rule.
  Timing advance and gap fill skip it (the track played in full).

## Last.fm outbox

`scrobble()` / `update_now_playing()` queue a job; one thread sends them in
order. Connectivity errors and Last.fm "temporarily unavailable" codes put
the scrobble in the persistent offline queue; a rejected scrobble (bad
parameters, auth) is logged, reported to Telegram and dropped. A successful
send flushes the queue; otherwise it is retried every 10 minutes.

## Where things live

| module | job |
|---|---|
| `capture.py` | continuous arecord → ring buffer, levels, WAV windows |
| `fingerprint.py` | AcoustID + Shazam (`Identifier`), corrections.yaml |
| `resolve.py` | fingerprint → album (Spotify + collection) |
| `spotify.py` | candidates, canonical names, album tracklists |
| `discogs.py` | the collection: releases, tracklists, positions |
| `tracklist.py` | `AlbumTracklist`, sides, Discogs ↔ Spotify merge |
| `session.py` | album lock, re-lock, compilation, backfill, timing |
| `engine.py` | the tick, `IdentifyWorker`, `ScreenState` |
| `scrobbler.py` | Last.fm outbox, offline queue, play counts |
| `art.py` | cover art with a disk cache |
| `display.py` | framebuffer renderer |
| `bot.py` / `notify.py` | Telegram |

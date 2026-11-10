# How Spindle decides what is playing

One `Player.tick()` every 2 s. Nothing in the tick waits on the network.

```
arecord ──► ring buffer ──► level (RMS of the last 2 s)
                 │
                 └──► 6 s WAV window ──► IdentifyWorker (thread)
                                            │  Shazam (→ AcoustID, optional)
                                            │  Resolver (Spotify → Deezer, + Discogs collection)
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
| LISTENING | music, nothing identified yet | the lockup and a breathing dot |
| PLAYING | we know the track | cover, tracklist, progress |

Starting a session needs `threshold + start_margin_db` for `start_ticks`
consecutive ticks (run-out groove noise sits at the threshold), **and audio
that moves like music**: the spectral motion of the last window (mean change
of the 1/3-octave log band profile from one second to the next) must reach
`MUSIC_MOTION_MIN` (0.11). A turntable running with nothing on it is loud
enough (the Plugable input at full gain put it at -38 dB RMS, 1 dB over the
gate) but its spectrum never moves (measured 0.05-0.07; the steadiest music
0.29, an ambient drone 0.5). Such input stays IDLE with the screen black; a
lookup still runs 20 s into the run, then 40 s later, doubling to every 5
min, so a record that really sounds that steady still gets found, and a hit
starts the session from the first loud tick. Ending a session needs
`min_silence_seconds` below the plain threshold. A dead capture (arecord
gone) holds the state instead of counting as silence.

**After a side** the needle rides the run-out until it is lifted. That crackle clears the
start gate and "moves" more than music does (the motion measure only rejects steady hum), so
it used to start a phantom session seconds after the side ended and take the end-of-side
card with it. Once a session has ended, loud input is the run-out of that side until the
level has sat 10 dB under the threshold for 4 s (the needle is up); a lookup still runs 8 s
in, so music that really follows (a hidden track, a clock that ended the side early) is
found and becomes a session.

**The lead-out** (one thump per revolution, `capture.revolution_pulse`) ends a side at once,
but only where a side can end: on the side's last track within its final 45 s, or, with
nothing locked, once something has been identified and has played a minute. A sparse intro
with one note per revolution is otherwise a lead-out too. Once the clock has run the side's
last track out, the first quiet tick or the first lead-out tick ends it.

A capture that stops delivering (arecord alive, nothing on its pipe for 6 s) is killed and
relaunched; until then the state is held, as for a dead capture.

`audio.capture_gain_db` sets the card's capture gain through amixer at every
start, so the level the thresholds were tuned for survives a reboot or a
re-plug (the USB device's own default is full gain).

## Recognition and catalogues

Shazam is asked first and alone by default. AcoustID is an optional second try
(`fingerprint.acoustid_fallback`): Chromaprint matches a track from its start, and a 6 s
window from the middle did not match once in 3,054 lookups on the real deck.

Names, track lengths and album tracklists come from Spotify while it answers and from
Deezer's public API when it cannot or has nothing (`deezer.Catalogue`; a day-long rate limit
on the Spotify app once left every record that was not in memory without names, lengths,
covers or scrobbles). Deezer album ids carry a `dz:` prefix; the same pressing reached
through either catalogue is the same record to the session.

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
A box set is one Discogs release (Sade's *This Far*: six records, sides A to L),
so against a nine-track album the whole box never reaches the coverage bar; the
resolver then cuts the box to the run of sides that hold the album's tracks
(`box_part`) and merges with that, so the side map and the flips still work.
One record is never cut this way: a half-matched single LP is another edition.

The collection loads on its own thread at start, refreshes daily, and
prefetches every release's tracklist once (cached on disk). Lookups from the
engine are memory-only.

## Session rules

* **Confirmation gate**: a track must be identified twice before it changes
  anything. One-off false matches never do. Confirmation is per *run*: two
  identifications more than 120 s apart do not confirm each other, so a
  track heard again later in the session is confirmed afresh and its
  first-heard time describes this play.
* **Fingerprint offsets**: Shazam reports where in the track the window
  sits. Each answer implies a start for the track; the newest 12 are voted
  on, and the largest set that advances with the clock (within 5 s plus 3 %
  of the interval for the deck's speed) wins. Two agreeing answers pin the
  start: the needle-drop position for the backfill, the gap budget between
  anchors, the start of a track the needle was moved to, and the elapsed
  time on screen. One odd answer (a repeated section, intro = outro), first
  or later, is outvoted; a needle moved inside a track is followed once the
  new place has the majority. AcoustID hits carry no offset.
* **Album lock**: first confirmed track with a known album. Backfills what
  played before the first ID by elapsed time; forward-fills gaps between
  anchors; advances by duration (1.5 s past the end once the start is
  pinned, 0.5 s with a measured deck speed, 3 % up to 15 s otherwise). The
  next track starts at the predicted end, not at the tick that noticed it,
  and inherits a pinned start.
* **Clock-bounded fill**: a gap track is scrobbled only when the elapsed
  time since the current track started had room for it in full; the first
  track that does not fit is scrobbled only if it played enough, the rest
  never played. The record need not follow the tracklist (a bootleg cut, a
  skipped track, the needle moved): Graduation, 2026-11-04, scrobbled two
  tracks the pressing does not have before this rule existed.
* **Backwards anchor**: a track identified before the predicted one needs
  two hits since the clock left it (a run stays "confirmed" for two minutes,
  so one late hit proves nothing), and a hit the offsets place in the last
  minute of the track just before is that track's tail heard inside the next
  one (Lost In The World into Who Will Survive In America), not a replay.
  Otherwise it is one of three things. *Lag*: the timing model ran that track to its end within
  the last 45 s and the capture window still carried it; its start is kept
  and the model re-advances. *Replay*: it ran to its end longer ago; the
  needle went back, and it and everything after it may scrobble again (the
  second play of a side is a real play). *Needle move*: it never played
  this pass, or was cut short; the current track is finished off (half-or-4-
  minutes rule) and the new one starts where it was heard, or where the
  offsets say it began.
* **Any order is a play**: entering a track whose previous play on this
  pass is over (its recorded start lies a full duration before the new
  start) clears it for scrobbling again, whether the timing model or an
  anchor got there. Listening to 3, then 1, 2, 3 scrobbles 3 twice.
* **Side aware**: when the pressing is known, timing stops at the last track
  of a side (the run-out) and waits for the flip; backfill never crosses a
  side boundary.
* **Re-lock**: the session moves to a better tracklist when one appears
  (the owned pressing once the collection loaded, the album after a lock on
  a single, the edition that actually has the track playing). What was
  scrobbled carries over by title. An owned pressing is never traded for a
  streaming edition. A stranger's lock that his record displaces before
  anything was scrobbled restarts the session on his record, with the
  needle-drop backfill. A lock on one of his records yields to another of
  his records after three confirmed hits on it over 30 s with none on the
  locked one in between (the same song on two of his albums, a sample heard
  as its source).
* **Anchors need the artist**: a hit from another album counts as this
  album's track only when its artist is the record's or that track's; a
  stranger's "Intro" does not move the needle.
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
order. The same track starting again inside its previous play is that play
sent twice and is dropped; a start a full track later is a replay and counts.
The offline queue stays on disk until each entry has been accepted; a refusal
of the credentials keeps the play queued and says so once an hour. Connectivity errors and Last.fm "temporarily unavailable" codes put
the scrobble in the persistent offline queue; a rejected scrobble (bad
parameters, auth) is logged, reported to Telegram and dropped. A successful
send flushes the queue; otherwise it is retried every 10 minutes.

## Where things live

| module | job |
|---|---|
| `capture.py` | continuous arecord → ring buffer, levels, WAV windows |
| `fingerprint.py` | Shazam, optional AcoustID (`Identifier`), corrections.yaml |
| `resolve.py` | fingerprint → album (catalogue + collection) |
| `spotify.py` | candidates, canonical names, album tracklists |
| `deezer.py` | the second catalogue and `Catalogue`, which asks Spotify first |
| `discogs.py` | the collection: releases, tracklists, positions |
| `tracklist.py` | `AlbumTracklist`, sides, Discogs ↔ Spotify merge |
| `session.py` | album lock, re-lock, compilation, backfill, timing |
| `engine.py` | the tick, `IdentifyWorker`, `ScreenState` |
| `scrobbler.py` | Last.fm outbox, offline queue, play counts |
| `art.py` | cover art with a disk cache |
| `display.py` | framebuffer renderer |
| `notify.py` | Telegram as the log channel (start, errors, warning digests) |

## Touch display

`web.py` serves the screen: `GET /events` streams every `ScreenState` as JSON
(Server-Sent Events, one message per change, keepalive every 15 s), `/art/<key>`
serves the cover bytes the state refers to, `/api/lyrics` fetches time-synced
lyrics from LRCLIB (exact lookup by artist, title, album, duration; else the best
search hit within 12 s of the duration), `/api/info` builds the info sheet from
the Discogs release (`DiscogsCollection.release_details`: labels, country, year,
formats, credits per track, the date it entered the collection), a Wikipedia
summary and Last.fm counts. Lookups are cached per track or album with a lifetime: what
was found is kept, what was not is asked again after six hours, a failed lookup after two
minutes, play counts after ten. The Wikipedia text is taken only from a page that carries
the album's name in its title and the artist in its text. Nothing runs on the tick thread. The page interpolates the progress between ticks with the
measured deck speed, so the bar and the lyrics stay on time.

The cover follows the track the session believes is playing, not the last
identification: a sample at the end of a song identifies as its source (the
O'Jays inside Drake's Middle of the Ocean, 2026-11-06) and must change neither
the track nor the cover.

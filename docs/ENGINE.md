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

Until something is recognised the engine asks as fast as answers come. After five misses
in a row each further one waits 2 s longer, up to the locked pace of one lookup in 8 s: a
record no service knows costs about 150 lookups a side instead of 600, against an
unofficial API that is the only recogniser there is. The first hit brings the fast pace
back.

## Resolution: which record?

A fingerprint says *artist, title*. The same recording is on the album, a
single, a few compilations and a deluxe edition, and a catalogue search lists
them in an order of its own. The `Resolver` decides with what it knows about
the shelf, by rules that hold for any collection (there is no list of special
albums anywhere):

1. **The record comes first.** A hit whose title is on a pressing in the
   Discogs collection *is* that pressing: its order and its sides are the
   truth. A track counts as the collection's when the release is by the
   hit's artist, or that one track on it is (a Various Artists compilation, a
   guest's track on a label sampler).
2. **The catalogue dresses the record.** The streaming edition that lends it
   names, lengths and a cover is chosen by how well the two lists agree, both
   ways: how much of the pressing the edition contains, and how much of the
   edition is on the pressing. Never by its name ("Blond" on the sleeve is
   "Blonde" in the catalogue), and not by coverage alone (a 40-track
   anthology contains a whole album; an album contains a whole 7"). An
   edition must contain half the pressing and agree with it at least 50%.
3. **One record, one edition.** When the hit's own albums fit loosely or not
   at all (a bonus track that only came out as a single, a remix closing side
   D), the catalogue is asked about the tracks next to it on the side, and
   the best edition found is remembered for the whole record.
4. **No edition at all**: the pressing itself is the album (Discogs' list),
   and each track takes its catalogue name and length the first time it is
   heard; lengths the list lacks are asked for a couple per identification,
   the tracks after the needle first.
5. **A title on two owned records** is the one the session is locked on, else
   the one the fingerprint names as its album, else the one the catalogue
   lists first for that track. (The session can still change its mind: see
   Re-lock.)
6. **Nothing owned**: a streaming album. The one the session is locked on,
   album > single > compilation, standard > deluxe / remaster, then the
   catalogue's order.

A merged list keeps Discogs' order and positions (`A1 … D3`) with the
catalogue's names and durations. A box set is one Discogs release (Sade's
*This Far*: six records, sides A to L), so no album ever contains half of it;
the resolver cuts the box to the run of sides that hold the album's tracks
(`box_part`) and merges with that, so the side map and the flips still work.
One record is never cut this way.

The resolver hands out **one list object per pressing and edition** for the
life of the process, so what it learns later (a length, a canonical name)
reaches a session that locked earlier.

### Names

Artists are compared as whole names, never as substrings: "Ye" is not "Yeat"
and "Che" is not "Cher". A credit line is taken apart ("A, B & C feat. D"),
case, accents and Discogs' "(2)" numbering are folded, "$" between letters
reads as "s" (A$AP, Ty Dolla $ign), and letters of every script are kept: a
Greek, Japanese or Korean title has a key like any other. What two strings
cannot say comes from data: for every artist on the shelf the collection asks
Discogs once for its **name variations and aliases** (Кино is also "Kino";
Ye is also "Kanye West") and a record answers to all of them.

A title that is on a record twice (the album cut on side B, its remix closing
side D) is the copy on the side the needle is on.

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
  a single, the edition that actually has the track playing, a better
  edition of the same pressing when the resolver finds one). What was
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
| `resolve.py` | fingerprint → record: the owned pressing first, dressed by the best-agreeing edition |
| `names.py` | names compared as names: keys, credit lines, any script |
| `spotify.py` | candidates, canonical names, album tracklists |
| `deezer.py` | the second catalogue and `Catalogue`, which asks Spotify first |
| `discogs.py` | the collection: releases, tracklists, positions, artists' other names |
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

### Panel wear and a silent engine

An OLED remembers what stays lit in one place. The layers that do (the home for a whole
side, the Listening mark, a page word) follow two CSS variables that the page walks a pixel
or two every 150 s, on a path that takes hours to repeat and stays inside a 12 x 8 px box;
the blurred backdrop stays put, so no edge ever shows. The Listening mark settles to 40%
after two minutes (a record nothing identifies would leave it at full white for a side).
The engine speaks every 2 s: after 30 s of nothing the page drops to the black idle
screen instead of leaving a frozen "now playing" lit, and picks up again with the next
event.


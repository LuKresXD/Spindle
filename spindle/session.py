"""Vinyl scrobble session: album lock + compilation mode.

Two modes:

  ALBUM       locked to one album (and, when the pressing is known, one side).
              Uses the tracklist + durations to infer and scrobble tracks the
              fingerprinter misses: backfill (before the first ID), forward
              fill (between IDs) and timing advance. Every inference is
              bounded by the clock: a gap track is scrobbled only when the
              elapsed time had room for it, because the record need not
              follow the tracklist (a bootleg cut, a skipped track, the
              needle moved).

              The needle goes where the listener puts it: a fingerprint that
              names a track BEFORE the predicted one is (a) a lagging
              identification of the track the timing model just finished,
              when it arrives within REPLAY_SLACK of that track's end, (b) a
              replay of a track that already ran to its end this pass, which
              makes it and everything after it eligible again, or (c) the
              needle moving back to a track that never played (or was cut
              short): the current track is finished off and the new one
              starts fresh. Any track a pass enters again after its previous
              play ended is a new play (3 -> 1 -> 2 -> 3 scrobbles 3 twice).

              Shazam reports where in the track the window sits. Two
              identifications of one run whose offsets advance with the clock
              (within OFFSET_TOLERANCE) pin the track's start exactly: the
              needle-drop position, the elapsed time on screen and the gap
              budget stop being guesses. Offsets that disagree (a repeated
              section, intro = outro) are ignored and the clock decides.

  COMPILATION consecutive identifications from different artists (DJ mix,
              bootleg, mixtape). No tracklist: each recognised track is
              scrobbled once it has played long enough, with its start time.

Mode selection:

  The first confirmed identification with a known album starts ALBUM mode.
  Only identifications by a DIFFERENT artist count toward COMPILATION
  (MISMATCH_THRESHOLD of them). A same-artist track that is not on the
  locked album is either a fingerprint confusion within the catalogue or a
  better album for this record; the latter re-locks the session when the new
  album also contains the track that is playing (a single upgraded to the
  album, a streaming edition upgraded to the owned pressing).

Session dedup:

  (artist, normalised title) pairs scrobbled in this silence-bounded
  session, across mode switches and re-locks. Cleared only on silence.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum

from .fingerprint import TrackInfo
from .resolve import Resolved
from .tracklist import AlbumTracklist

logger = logging.getLogger(__name__)

# ── Timing constants ─────────────────────────────────────────────────────────
VINYL_DRIFT          = 0.03  # ±3 % speed tolerance (wow/flutter/motor drift)
NEEDLE_TOLERANCE     = 15    # seconds of needle-drop imprecision
ADVANCE_BUFFER_MIN   = 3     # min seconds past duration before auto-advance
ADVANCE_BUFFER_MAX   = 15    # max seconds past duration before auto-advance
FALLBACK_TRACK_DURATION = 240  # assumed duration (s) when nobody knows (4 min)
REPLAY_SLACK         = 45    # s past a track's end: a backwards anchor is a replay, not lag
OFFSET_TOLERANCE     = 5     # s: two offsets of one run must advance with the clock this closely
SPEED_MIN_INTERVAL   = 20    # s between two offsets before their ratio counts as a speed sample
SPEED_SAMPLES_MIN    = 3     # samples before the measured deck speed replaces the assumed 1.0
SPEED_SAMPLES_MAX    = 60    # keep the newest this many (the deck's speed drifts slowly)
SPEED_RANGE          = (0.90, 1.10)   # a ratio outside this is a bad offset, not a deck

# ── Compilation detection ─────────────────────────────────────────────────────
MISMATCH_THRESHOLD = 3      # different-artist IDs (spread over MISMATCH_MIN_SECONDS) → COMPILATION
MISMATCH_MIN_SECONDS = 60   # a fingerprint confusion lasts a few windows; a mix lasts minutes
CONFIRM_THRESHOLD  = 2      # times a track must be identified before acting on it
CONFIRM_RUN_GAP    = 120    # s without hearing a track: its next ID starts a new run


# ── Helpers ──────────────────────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """Normalize a track title for cross-edition dedup.

    Strips version/remix/remaster parentheticals, featured-artist tags,
    vinyl format markers, and trailing edition suffixes so that:
      "The Girl Is Mine (2008 with will.i.am)"  →  "the girl is mine"
      "Thriller 7\" – Special Edit"              →  "thriller"
      "Heroes (Remastered 2017)"                 →  "heroes"
    """
    s = title
    s = re.sub(
        r'\s*[\(\[]'
        r'(?:\d{4}\s+)?'
        r'(?:with|feat\.?|ft\.?|featuring|remix|remaster(?:ed)?|edit|'
        r'version|mix|single|radio|live|demo|acoustic|instrumental|bonus)'
        r'[^\)\]]*[\)\]]',
        '', s, flags=re.IGNORECASE,
    ).strip()
    s = re.sub(
        r'\s*[-–—]\s*'
        r'(?:special\s+edit|remaster(?:ed)?(?:\s+\d{4})?|single\s+version|'
        r'radio\s+edit|album\s+version|extended|remix)\s*$',
        '', s, flags=re.IGNORECASE,
    ).strip()
    s = re.sub(r'\s*\d+["”]\s*', ' ', s).strip()
    return s.lower() if s else title.lower()


def _artist_key(name: str) -> str:
    s = (name or "").lower().replace("$", "s").replace("'", "").replace("’", "")
    s = re.sub(r"\s*\(\d+\)$", "", s)
    return " ".join(re.sub(r"[^a-z0-9Ѐ-ӿ]+", " ", s).split())


def same_artist(a: str, b: str) -> bool:
    ka, kb = _artist_key(a), _artist_key(b)
    if not ka or not kb:
        return False
    if "various" in (ka, kb) or "various artists" in (ka, kb):
        return True
    return ka == kb or ka in kb or kb in ka


class SessionMode(Enum):
    ALBUM       = "album"
    COMPILATION = "compilation"


# ── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Anchor:
    """A confirmed fingerprint identification at a known wall-clock time."""
    track_index: int
    timestamp: float


@dataclass
class _Run:
    """Consecutive identifications of one track: the confirmation gate, and the
    track's wall-clock start when the run's fingerprint offsets agree."""
    hits:       int = 0
    first_seen: float = 0.0          # window-end time of the run's first identification
    last_seen:  float = 0.0
    ref:        tuple[float, float] | None = None   # (at, offset) of the last consistent ID
    start:      float | None = None  # at - offset - window, once a second offset confirms it
    speed_ref:  tuple[float, float] | None = None   # (at, offset) the deck speed is measured from


@dataclass
class AlbumState:
    """All state for one album-mode session."""
    album_id:            str
    tracklist:           AlbumTracklist
    music_start:         float           # wall-clock time music first started
    anchors:             list = field(default_factory=list)
    current_index:       int   = 0       # best estimate of current track
    current_track_start: float = 0.0     # estimated wall-clock start of current track
    scrobbled:           set   = field(default_factory=set)  # track indices scrobbled
    starts:              dict  = field(default_factory=dict) # index → wall-clock start, this pass
    completed:           set   = field(default_factory=set)  # indices timing ran to the end
    start_exact:         bool  = False   # current_track_start came from agreeing offsets
    exhausted:           bool  = False   # advanced past the last track
    side_done:           bool  = False   # timing reached the end of the current side


# ── Main class ────────────────────────────────────────────────────────────────

class ScrobbleSession:
    """Scrobbling state for one silence-bounded music session.

    Public API
    ----------
    on_identified(resolved, music_start, at=None) → [(TrackInfo, timestamp), ...]
    check_advance()                              → [(TrackInfo, timestamp), ...]
    on_silence()                                 → [(TrackInfo, timestamp), ...]
    get_current_track()                          → Optional[TrackInfo]
    get_progress()                               → Optional[(elapsed, duration)]
    is_locked()                                  → bool
    album_state / mode / comp_scrobbled_count / locked_album_id / locked_artist
    reset()
    """

    def __init__(
        self,
        min_play_seconds: int = 30,
        chunk_duration: int = 10,
        min_play_fraction: float = 0.5,
        clock: Callable[[], float] = time.time,
        window_seconds: float = 6.0,
    ):
        self.min_play_seconds  = min_play_seconds
        self.min_play_fraction = min_play_fraction
        self.chunk_duration    = chunk_duration
        self.window_seconds    = window_seconds     # the identified audio window (offset math)
        self._clock            = clock

        self.mode: SessionMode | None = None   # None = no session yet

        self._album: AlbumState | None = None
        self._mismatch_count: int = 0
        self._mismatch_since: float | None = None
        self._abandoned: AlbumState | None = None     # the album a compilation switch left
        self._comp_count: int = 0
        self._comp_current: TrackInfo | None = None
        self._comp_start: float = 0.0

        # Session-wide dedup: (artist_lower, normalized_title)
        self._session_scrobbled: set[tuple[str, str]] = set()

        # Confirmation gate: (artist_lower, title_lower) → the current run of IDs
        self._confirm: dict[tuple[str, str], _Run] = {}
        # Deck speed samples (track seconds per wall second), kept across sessions:
        # the turntable's speed is a property of the deck, not of one record
        self._speed_samples: list[float] = []

    # ── Properties ───────────────────────────────────────────────────────────

    @property
    def album_state(self) -> AlbumState | None:
        return self._album

    @property
    def comp_scrobbled_count(self) -> int:
        return self._comp_count

    @property
    def locked_album_id(self) -> str:
        return self._album.album_id if self._album else ""

    @property
    def locked_artist(self) -> str:
        return self._album.tracklist.artist if self._album else ""

    # ── Public API ───────────────────────────────────────────────────────────

    def on_identified(
        self,
        resolved: Resolved,
        music_start: float,
        at: float | None = None,
    ) -> list[tuple[TrackInfo, float]]:
        """Handle a resolved identification made at wall-clock `at`.

        Returns (TrackInfo, timestamp) pairs ready to scrobble. The caller
        also calls check_advance() every tick for timing-based advances.
        """
        now = self._clock() if at is None else at
        track = resolved.track

        if not self._is_confirmed(track, now, resolved.offset):
            return []

        if self.mode == SessionMode.COMPILATION:
            back = self._return_to_album(resolved, now)
            if back is not None:
                return back
            return self._handle_compilation_id(track, now)

        if not resolved.has_album:
            return []                                 # no tracklist: the engine's simple mode

        assert resolved.tracklist is not None
        if self.mode is None:
            return self._start_album_session(resolved, resolved.tracklist, music_start, now)

        return self._handle_album_id(resolved, now)

    def check_advance(self) -> list[tuple[TrackInfo, float]]:
        """Timing-based track advance, called every tick.

        ALBUM mode only. When the current track's duration plus a drift buffer
        has elapsed, scrobbles it and moves to the next track, except at the
        end of a side: there the needle is in the run-out groove and the
        pointer waits for the flip (silence) or for a fresh anchor.
        """
        if self.mode != SessionMode.ALBUM or not self._album or self._album.exhausted:
            return []
        al = self._album
        if al.side_done:
            return []

        current = al.tracklist.get_track_at(al.current_index)
        if not current:
            return []

        duration = current.duration or FALLBACK_TRACK_DURATION
        elapsed = self._clock() - al.current_track_start
        # A deck running 3 % fast ends a 5-minute track 9 s early: predict the end
        # with the measured speed. The drift buffer only covers what is still
        # unknown: nothing much once the start came from agreeing offsets.
        expected = duration / self.deck_speed()
        if al.start_exact:
            buffer = float(ADVANCE_BUFFER_MIN)
        else:
            buffer = min(max(duration * VINYL_DRIFT, ADVANCE_BUFFER_MIN), ADVANCE_BUFFER_MAX)
        if elapsed < expected + buffer:
            return []

        result: list[tuple[TrackInfo, float]] = []
        result.extend(self._try_scrobble(al.current_index, al.current_track_start))
        al.completed.add(al.current_index)

        if al.tracklist.is_side_end(al.current_index):
            al.side_done = True
            logger.info("Session: end of side %s — waiting for the flip",
                        al.tracklist.side_at(al.current_index))
            return result

        next_idx = al.current_index + 1
        next_track = al.tracklist.get_track_at(next_idx)
        if next_track:
            self._set_current(next_idx, self._clock())
            logger.info("Session: ⏭  track %d — %s — %s",
                        next_idx + 1, next_track.artist, next_track.title)
        else:
            al.exhausted = True
            logger.info("Session: end of tracklist — waiting for silence")
        return result

    def played_enough(self, track: TrackInfo, elapsed: float) -> bool:
        """Last.fm's rule: at least half the track or 4 minutes, and never
        under min_play_seconds. Used when a track ends early (needle lifted,
        silence, mode switch); timing-advance and gap-fill know the track
        played in full and skip this."""
        if elapsed < self.min_play_seconds:
            return False
        if track.duration:
            return elapsed >= min(track.duration * self.min_play_fraction, 240)
        return True

    def on_silence(self) -> list[tuple[TrackInfo, float]]:
        """End the session: scrobble what played long enough, reset everything."""
        now = self._clock()
        result: list[tuple[TrackInfo, float]] = []

        if self.mode == SessionMode.ALBUM and self._album:
            result.extend(self._finalize_album_current(now))
            logger.info("Session: 🔓 ALBUM ended — %s / %s (%d tracks scrobbled)",
                        self._album.tracklist.artist, self._album.tracklist.album_name,
                        len(self._album.scrobbled))
        elif self.mode == SessionMode.COMPILATION:
            result.extend(self._finalize_comp(now))
            logger.info("Session: 🔓 COMPILATION ended (%d tracks scrobbled)", self._comp_count)

        self.reset()
        return result

    def get_current_track(self) -> TrackInfo | None:
        """What we believe is playing (now-playing + display)."""
        if self.mode == SessionMode.ALBUM and self._album:
            return self._album.tracklist.get_track_at(self._album.current_index)
        if self.mode == SessionMode.COMPILATION:
            return self._comp_current
        return None

    def get_progress(self) -> tuple[float, float] | None:
        """(elapsed, duration) for the current track, or None."""
        if self.mode != SessionMode.ALBUM or not self._album:
            return None
        current = self._album.tracklist.get_track_at(self._album.current_index)
        if not current or not current.duration:
            return None
        elapsed = (self._clock() - self._album.current_track_start) * self.deck_speed()
        return (min(elapsed, current.duration), current.duration)

    def deck_speed(self) -> float:
        """Track seconds per wall second, measured from fingerprint offsets:
        1.03 = the deck runs 3 % fast. 1.0 until enough samples exist."""
        if len(self._speed_samples) < SPEED_SAMPLES_MIN:
            return 1.0
        s = sorted(self._speed_samples)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

    @property
    def deck_speed_samples(self) -> int:
        return len(self._speed_samples)

    def current_track_start(self) -> float | None:
        if self.mode == SessionMode.ALBUM and self._album:
            return self._album.current_track_start
        if self.mode == SessionMode.COMPILATION and self._comp_current:
            return self._comp_start
        return None

    def is_locked(self) -> bool:
        """True while in album mode with a live (non-exhausted) album session."""
        return (self.mode == SessionMode.ALBUM and self._album is not None
                and not self._album.exhausted)

    def reset(self) -> None:
        self.mode = None
        self._album = None
        self._mismatch_count = 0
        self._mismatch_since = None
        self._abandoned = None
        self._comp_count = 0
        self._comp_current = None
        self._comp_start = 0.0
        self._session_scrobbled.clear()
        self._confirm.clear()

    # ── Confirmation gate ────────────────────────────────────────────────────

    def _is_confirmed(self, track: TrackInfo, now: float, offset: float | None = None) -> bool:
        """True once this track has been identified CONFIRM_THRESHOLD times in
        the current run (IDs less than CONFIRM_RUN_GAP apart).

        Real tracks are identified again and again while they play; a
        one-off false fingerprint match never reaches the threshold. A track
        heard again after a long gap starts a new run, so its first-seen
        time (and its offsets) describe this play, not an earlier one.
        """
        key = (track.artist.lower(), track.title.lower())
        run = self._confirm.get(key)
        if run is None or now - run.last_seen > CONFIRM_RUN_GAP:
            run = _Run(first_seen=now)
            self._confirm[key] = run
        run.hits += 1
        run.last_seen = now
        if offset is not None:
            if run.ref is None:
                run.ref = (now, offset)
            elif now > run.ref[0]:
                # the offsets must advance with the clock, allowing for the turntable's
                # speed error over the interval (a 3 % fast deck gains 6 s in 200 s)
                ref_at, ref_offset = run.ref
                drift = (offset - ref_offset) - (now - ref_at)
                if abs(drift) <= OFFSET_TOLERANCE + VINYL_DRIFT * (now - ref_at):
                    run.start = now - offset - self.window_seconds
                    run.ref = (now, offset)          # consistent: measure the next one from here
                    if run.speed_ref is None:
                        run.speed_ref = (ref_at, ref_offset)
                    span = now - run.speed_ref[0]
                    if span >= SPEED_MIN_INTERVAL:
                        ratio = (offset - run.speed_ref[1]) / span
                        if SPEED_RANGE[0] <= ratio <= SPEED_RANGE[1]:
                            self._speed_samples.append(ratio)
                            del self._speed_samples[:-SPEED_SAMPLES_MAX]
                else:
                    run.start = None
                    logger.info("Session: offsets of '%s' disagree with the clock by %.0fs "
                                "(a repeated section?) — position not trusted",
                                track.title, drift)
        if run.hits < CONFIRM_THRESHOLD:
            logger.debug("Session: confirm %d/%d — '%s — %s'", run.hits, CONFIRM_THRESHOLD,
                         track.artist, track.title)
            return False
        return True

    def _first_seen(self, track: TrackInfo, default: float) -> float:
        key = (track.artist.lower(), track.title.lower())
        run = self._confirm.get(key)
        return run.first_seen if run is not None else default

    def _run_start(self, track: TrackInfo) -> float | None:
        """The track's wall-clock start by the current run's offsets, if they agree."""
        key = (track.artist.lower(), track.title.lower())
        run = self._confirm.get(key)
        return run.start if run is not None else None

    def _offset_start(self, track: TrackInfo, now: float, floor: float) -> float | None:
        """The offset-derived start, when it is physically possible: not after
        now, not before `floor` by more than the needle tolerance, and not so
        far back that the track would already be over."""
        start = self._run_start(track)
        if start is None or start > now:
            return None
        if start < floor - NEEDLE_TOLERANCE:
            return None
        duration = track.duration or FALLBACK_TRACK_DURATION
        if now - start > duration + OFFSET_TOLERANCE:
            return None
        return max(start, floor)

    # ── Session-level dedup ───────────────────────────────────────────────────

    def _dedup_key(self, track: TrackInfo) -> tuple[str, str]:
        return (track.artist.lower(), normalize_title(track.title))

    def _already_scrobbled(self, track: TrackInfo) -> bool:
        return self._dedup_key(track) in self._session_scrobbled

    def _mark_scrobbled(self, track: TrackInfo) -> None:
        self._session_scrobbled.add(self._dedup_key(track))

    def _set_current(self, index: int, start: float, exact: bool = False) -> None:
        """Make `index` the current track, started at wall-clock `start`
        (`exact`: the start came from agreeing fingerprint offsets).

        Entering a track whose previous play on this pass is already over
        (its start lies a full duration before the new one) is a new play:
        it may scrobble again. A re-sync onto the same play (same start)
        changes nothing.
        """
        al = self._album
        assert al is not None
        al.start_exact = exact
        prev = al.starts.get(index)
        if prev is not None and index in al.scrobbled:
            track = al.tracklist.get_track_at(index)
            duration = track.duration if track and track.duration else FALLBACK_TRACK_DURATION
            if start - prev >= duration:
                al.scrobbled.discard(index)
                al.completed.discard(index)
                if track:
                    self._session_scrobbled.discard(self._dedup_key(track))
                logger.info("Session: track %d again — a new play", index + 1)
        al.current_index = index
        al.current_track_start = start
        al.starts[index] = start

    # ── Album mode: session start ─────────────────────────────────────────────

    def _start_album_session(self, resolved: Resolved, tracklist: AlbumTracklist,
                             music_start: float, now: float) -> list[tuple[TrackInfo, float]]:
        track_index = tracklist.find_track_index(resolved.track.title)
        if track_index is None:
            logger.debug("Session: '%s' not found in tracklist for %s — ignoring",
                         resolved.track.title, tracklist.album_name)
            return []

        self._album = AlbumState(
            album_id=resolved.album_id, tracklist=tracklist, music_start=music_start,
            anchors=[Anchor(track_index, now)], current_index=track_index,
            current_track_start=music_start, starts={track_index: music_start},
        )
        self.mode = SessionMode.ALBUM
        self._mismatch_count = 0

        track = tracklist.get_track_at(track_index) or resolved.track
        start = self._offset_start(track, now, floor=music_start)
        pos = tracklist.position_at(track_index)
        logger.info("Session: 🔒 ALBUM — %s / %s  (track %d/%d%s, first ID: '%s', %s%s%s)",
                    tracklist.artist, tracklist.album_name, track_index + 1,
                    len(tracklist.tracks), f" {pos}" if pos else "", resolved.track.title,
                    tracklist.source, ", owned" if tracklist.owned else "",
                    f", {now - start:.0f}s in by offset" if start is not None else "")

        return self._retroactive_backfill(track_index, now, start)

    # ── Album mode: ongoing identifications ───────────────────────────────────

    def _handle_album_id(self, resolved: Resolved, now: float) -> list[tuple[TrackInfo, float]]:
        assert self._album is not None
        al = self._album

        # A better home for this session? (the owned pressing once the collection has
        # loaded, the album when we locked onto a single, the edition that has this track)
        new_tl = resolved.tracklist if resolved.has_album else None
        if new_tl is not None and new_tl is not al.tracklist and self._is_upgrade(new_tl):
            return self._relock(resolved, new_tl, now)

        track_index = self._find_in_current_album(resolved)
        if track_index is not None:
            self._mismatch_count = 0
            self._mismatch_since = None
            return self._apply_anchor(track_index, now, resolved.track)

        # Not on the locked album. Same artist, or a title that names the locked
        # artist (their remix of someone else's track): catalogue confusion, ignore.
        # Different artist: maybe a compilation, but only once it has persisted.
        if same_artist(resolved.track.artist, al.tracklist.artist) or \
                _artist_key(al.tracklist.artist) in _artist_key(resolved.track.title):
            logger.info("Session: '%s — %s' is not on '%s' (same artist) — ignoring",
                        resolved.track.artist, resolved.track.title, al.tracklist.album_name)
            return []

        if al.tracklist.owned:
            # a record from the collection is this record: a stranger's name on it
            # is a fingerprint confusion (Slam heard as the Prodigy remix, 2026-11-06)
            logger.info("Session: '%s — %s' while locked on owned '%s' — ignoring",
                        resolved.track.artist, resolved.track.title, al.tracklist.album_name)
            return []

        if self._mismatch_since is None:
            self._mismatch_since = now
        self._mismatch_count += 1
        spread = now - self._mismatch_since
        logger.info("Session: mismatch %d/%d over %.0fs — '%s — %s' while locked on '%s'",
                    self._mismatch_count, MISMATCH_THRESHOLD, spread,
                    resolved.track.artist, resolved.track.title, al.tracklist.artist)
        if self._mismatch_count < MISMATCH_THRESHOLD or spread < MISMATCH_MIN_SECONDS:
            return []

        logger.info("Session: 🎛  switching to COMPILATION mode (different artists on '%s')",
                    al.tracklist.album_name)
        result = self._finalize_album_current(now)
        self.mode = SessionMode.COMPILATION
        self._abandoned = al
        self._album = None
        self._mismatch_count = 0
        self._mismatch_since = None
        result.extend(self._handle_compilation_id(resolved.track, now))
        return result

    def _find_in_current_album(self, resolved: Resolved) -> int | None:
        """This track's index in the locked album, or None.

        Accepts the same album id with a matching title, or (any edition,
        any primary artist: feat. tracks differ) a title the tracklist has.
        """
        if not self._album:
            return None
        return self._album.tracklist.find_track_index(resolved.track.title)

    def _is_upgrade(self, new_tl: AlbumTracklist) -> bool:
        """Is `new_tl` a better home for this session than the locked album?

        It must be the same artist (or a Various Artists record), contain the
        track believed to be playing and everything scrobbled so far, and be
        either the owned pressing when the locked one is not, or a different,
        longer edition (the album when we locked onto a single; the edition
        that actually has the track that is playing). An owned pressing is
        never traded for a streaming edition.
        """
        assert self._album is not None
        old = self._album.tracklist
        if old.owned and not new_tl.owned:
            return False
        if not same_artist(new_tl.artist, old.artist):
            return False
        cur = old.get_track_at(self._album.current_index)
        if cur is None or new_tl.find_track_index(cur.title) is None:
            return False
        for i in self._album.scrobbled:
            t = old.get_track_at(i)
            if t and new_tl.find_track_index(t.title) is None:
                return False
        if new_tl.owned and not old.owned:
            return True
        if new_tl.album_id == old.album_id:
            return False
        return len(new_tl.tracks) > len(old.tracks)

    def _relock(self, resolved: Resolved, new_tl: AlbumTracklist,
                now: float) -> list[tuple[TrackInfo, float]]:
        """Move the session onto a better album without losing what played."""
        assert self._album is not None
        old = self._album
        idx = new_tl.find_track_index(resolved.track.title)
        if idx is None:
            return []
        cur = old.tracklist.get_track_at(old.current_index)
        same_track = cur is not None and new_tl.find_track_index(cur.title) == idx
        # the same track keeps playing: keep its start; a new one: finish the old first
        result = [] if same_track else self._finalize_album_current(now)
        if same_track:
            start = old.current_track_start
        else:
            by_offset = self._offset_start(resolved.track, now, floor=old.current_track_start)
            start = by_offset if by_offset is not None else self._first_seen(resolved.track, now)

        def mapped(indices) -> dict[int, int]:
            out: dict[int, int] = {}
            for i in indices:
                t = old.tracklist.get_track_at(i)
                j = new_tl.find_track_index(t.title) if t else None
                if j is not None:
                    out[i] = j
            return out

        scrobbled = set(mapped(old.scrobbled).values())
        completed = set(mapped(old.completed).values())
        starts = {j: old.starts[i] for i, j in mapped(old.starts).items()}
        starts[idx] = start
        self._album = AlbumState(
            album_id=resolved.album_id, tracklist=new_tl, music_start=old.music_start,
            anchors=old.anchors + [Anchor(idx, now)], current_index=idx,
            current_track_start=start, scrobbled=scrobbled, starts=starts, completed=completed,
        )
        self._mismatch_count = 0
        logger.info("Session: 🔁 re-locked — %s / %s → %s (%d tracks, %s%s), now at track %d",
                    new_tl.artist, old.tracklist.album_name, new_tl.album_name,
                    len(new_tl.tracks), new_tl.source, ", owned" if new_tl.owned else "",
                    idx + 1)
        return result

    def _apply_anchor(self, track_index: int, now: float,
                      heard_track: TrackInfo) -> list[tuple[TrackInfo, float]]:
        """A confirmed identification of `track_index` whose audio ended at `now`.
        `heard_track` is the identified track (its confirmation run carries the
        first-heard time and, when the offsets agree, the exact start)."""
        assert self._album is not None
        al = self._album
        al.anchors.append(Anchor(track_index, now))
        al.exhausted = False
        result: list[tuple[TrackInfo, float]] = []
        track = al.tracklist.get_track_at(track_index) or heard_track
        # the window that first matched this track ended at first_seen and was
        # window_seconds long: the track had begun by its start
        heard = max(al.current_track_start,
                    min(now, self._first_seen(heard_track, now) - self.window_seconds))

        if track_index == al.current_index:
            logger.debug("Session: anchor confirms track %d ✓", track_index + 1)
            # the offsets know better than the clock where this track began
            prev_idx = max((i for i in al.starts if i != track_index
                            and al.starts[i] < al.current_track_start), default=None)
            floor = al.starts[prev_idx] if prev_idx is not None else al.music_start
            start = self._offset_start(track, now, floor=floor)
            if start is not None and abs(start - al.current_track_start) > OFFSET_TOLERANCE:
                logger.info("Session: track %d started %.0fs %s than the clock thought (offset)",
                            track_index + 1, abs(start - al.current_track_start),
                            "later" if start > al.current_track_start else "earlier")
                self._set_current(track_index, start, exact=True)
                al.side_done = False
            elif start is not None and not al.start_exact:
                al.start_exact = True                # the clock's guess was right: trust it
            elif al.side_done:
                # the side "ended" by timing but the same track is still being heard:
                # the timing model ran fast; keep showing it, the flip will come
                pass
        elif track_index > al.current_index:
            start = self._offset_start(track, now, floor=al.current_track_start)
            until = start if start is not None else now
            gap, consumed, complete = self._fill_forward(al.current_index, track_index, until)
            result.extend(gap)
            exact = start is not None
            if start is None:
                # every gap track fit the clock: the anchored track began when they ran out;
                # otherwise it began some time before it was first heard
                start = min(now, al.current_track_start + consumed) if complete else heard
            self._set_current(track_index, start, exact=exact)
            al.side_done = False
            logger.info("Session: anchor → track %d (filled %d gap tracks)",
                        track_index + 1, len(gap))
        else:
            start = self._offset_start(track, now, floor=al.current_track_start)
            result.extend(self._anchor_backwards(track_index, now, heard, start))
        return result

    def _anchor_backwards(self, track_index: int, now: float, heard: float,
                          start: float | None) -> list[tuple[TrackInfo, float]]:
        """The fingerprint names a track before the one the clock predicts.

        Lag: the timing model ran this track to its end within the last
        REPLAY_SLACK seconds and the capture window still carried it; keep
        its start so the model re-advances without moving any timestamp.
        Replay: the track ran to its end longer ago; the needle went back, so
        it and everything after it may scrobble once more. Needle move: the
        track never played this pass (or was cut short); finish the current
        track off and start this one where it was heard (or where the offsets
        say it began).
        """
        assert self._album is not None
        al = self._album
        track = al.tracklist.get_track_at(track_index)
        duration = track.duration if track and track.duration else FALLBACK_TRACK_DURATION
        prev_start = al.starts.get(track_index)
        result: list[tuple[TrackInfo, float]] = []
        if prev_start is None or track_index not in al.completed:
            result.extend(self._finalize_album_current(now))
            logger.info("Session: ↩  needle moved back to track %d (from %d)",
                        track_index + 1, al.current_index + 1)
            self._set_current(track_index, start if start is not None else heard,
                              exact=start is not None)
        elif now - (prev_start + duration) <= REPLAY_SLACK and start is None:
            logger.info("Session: lagging ID of track %d (clock is on %d), re-syncing",
                        track_index + 1, al.current_index + 1)
            self._set_current(track_index, prev_start)
        else:
            result.extend(self._finalize_album_current(now))
            replayed = sorted(i for i in al.scrobbled if i >= track_index)
            for i in replayed:
                al.scrobbled.discard(i)
                t = al.tracklist.get_track_at(i)
                if t:
                    self._session_scrobbled.discard(self._dedup_key(t))
            for i in [i for i in al.starts if i >= track_index]:
                del al.starts[i]
                al.completed.discard(i)
            logger.info("Session: ↩  replay from track %d (%d scrobbled tracks eligible again)",
                        track_index + 1, len(replayed))
            self._set_current(track_index, start if start is not None else heard,
                              exact=start is not None)
        al.side_done = False
        return result

    def _finalize_album_current(self, now: float) -> list[tuple[TrackInfo, float]]:
        """Scrobble the current album track if it has played long enough."""
        if not self._album:
            return []
        current = self._album.tracklist.get_track_at(self._album.current_index)
        elapsed = now - self._album.current_track_start
        if current and self.played_enough(current, elapsed):
            return self._try_scrobble(self._album.current_index,
                                      self._album.current_track_start)
        return []

    # ── Album mode: scrobbling helpers ────────────────────────────────────────

    def _try_scrobble(self, index: int, timestamp: float,
                      force: bool = False) -> list[tuple[TrackInfo, float]]:
        """Scrobble album track at `index` if eligible and not already done.

        force: skip the min-duration check (gap-fill / backfill, where the
        track is known to have played in full).
        """
        if not self._album:
            return []
        track = self._album.tracklist.get_track_at(index)
        if not track:
            return []
        if index in self._album.scrobbled:
            return []
        if self._already_scrobbled(track):
            logger.debug("Session dedup: '%s — %s'", track.artist, track.title)
            self._album.scrobbled.add(index)
            return []
        if not force and track.duration and track.duration < self.min_play_seconds:
            return []
        self._album.scrobbled.add(index)
        self._mark_scrobbled(track)
        return [(track, timestamp)]

    # ── Album mode: backfill + gap-fill ──────────────────────────────────────

    def _retroactive_backfill(self, anchor_index: int, anchor_time: float,
                              anchor_start: float | None = None) -> list[tuple[TrackInfo, float]]:
        """Infer and scrobble tracks that played before the first identification.

        With the anchor track's start known (fingerprint offsets), the time
        between music_start and that start belonged to the tracks before it:
        walk backwards filling whole tracks, and count a cut-in first track
        only if it played enough. Without it, elapsed = anchor_time -
        music_start: walk backwards summing durations; the earliest start
        track for which the remaining time falls inside the anchor track
        (± tolerance) is the needle-drop position. Never crosses a side
        boundary: the music before this side started was a different
        silence-bounded session.
        """
        if not self._album:
            return []
        if anchor_start is not None:
            return self._backfill_from_start(anchor_index, anchor_start)
        if anchor_index == 0:
            return []
        elapsed = anchor_time - self._album.music_start
        if elapsed < self.chunk_duration:
            return []

        tracklist = self._album.tracklist
        tolerance = elapsed * VINYL_DRIFT + NEEDLE_TOLERANCE
        anchor_track = tracklist.get_track_at(anchor_index)
        anchor_dur = anchor_track.duration if anchor_track and anchor_track.duration else 600

        best_start = anchor_index
        cumulative = 0.0
        for i in range(anchor_index - 1, -1, -1):
            if tracklist.is_side_end(i):
                break                                  # previous side: not this session
            track = tracklist.get_track_at(i)
            if not track or not track.duration:
                break
            cumulative += track.duration
            time_into_anchor = elapsed - cumulative
            if -tolerance <= time_into_anchor <= anchor_dur + tolerance:
                best_start = i
            if cumulative > elapsed + tolerance:
                break

        if best_start >= anchor_index:
            return []

        result: list[tuple[TrackInfo, float]] = []
        ts = self._album.music_start
        for i in range(best_start, anchor_index):
            track = tracklist.get_track_at(i)
            if not track:
                break
            result.extend(self._try_scrobble(i, ts, force=True))
            self._album.starts[i] = ts
            ts += track.duration if track.duration else FALLBACK_TRACK_DURATION
        self._set_current(anchor_index, ts)

        logger.info("Session: retroactive backfill — %d tracks from track %d "
                    "(%.0fs before first ID)", len(result), best_start + 1, elapsed)
        for track, _ in result:
            logger.info("  ↳ %s — %s", track.artist, track.title)
        return result

    def _backfill_from_start(self, anchor_index: int,
                             anchor_start: float) -> list[tuple[TrackInfo, float]]:
        """Backfill when the anchor track's start is known: the music before it
        ran from music_start to anchor_start. Whole tracks fill back from the
        anchor; the track the needle dropped into counts only if the part that
        played was enough. Stops at a side boundary."""
        al = self._album
        assert al is not None
        before = anchor_start - al.music_start
        tracklist = al.tracklist
        filled: list[tuple[int, float]] = []             # (index, start), newest first
        cumulative = 0.0
        tolerance = before * VINYL_DRIFT + NEEDLE_TOLERANCE
        for i in range(anchor_index - 1, -1, -1):
            if before - cumulative < self.min_play_seconds:
                break                                   # whatever is left is too short to count
            if tracklist.is_side_end(i):
                break                                   # previous side: not this session
            track = tracklist.get_track_at(i)
            if not track:
                break
            duration = track.duration if track.duration else FALLBACK_TRACK_DURATION
            if cumulative + duration <= before + tolerance:
                cumulative += duration
                filled.append((i, anchor_start - cumulative))
                continue
            played = before - cumulative                 # the needle dropped into this one
            if self.played_enough(track, played):
                filled.append((i, al.music_start))
            break

        result: list[tuple[TrackInfo, float]] = []
        for i, ts in reversed(filled):
            al.starts[i] = ts
            result.extend(self._try_scrobble(i, ts, force=True))
        self._set_current(anchor_index, anchor_start, exact=True)
        if result:
            logger.info("Session: retroactive backfill — %d tracks before track %d "
                        "(%.0fs of music before it, by offset)", len(result),
                        anchor_index + 1, before)
            for track, _ in result:
                logger.info("  ↳ %s — %s", track.artist, track.title)
        return result

    def _fill_forward(self, from_index: int, to_index: int,
                      until: float) -> tuple[list[tuple[TrackInfo, float]], float, bool]:
        """Scrobble the tracks from from_index up to (not including) to_index
        that the clock had room for between the current track's start and
        `until` (when the anchored track began, or now when nobody knows).

        The record need not follow the tracklist (a bootleg cut, a skipped
        track, the needle moved), so a track is filled only while the elapsed
        time still covers it in full. The first one that does not fit is
        scrobbled only if it played enough; the rest never played. Returns
        (scrobbles, seconds consumed, whether every gap track fit).
        """
        if not self._album:
            return [], 0.0, False
        al = self._album
        result: list[tuple[TrackInfo, float]] = []
        budget = until - al.current_track_start
        tolerance = budget * VINYL_DRIFT + NEEDLE_TOLERANCE
        ts = al.current_track_start
        consumed = 0.0
        for i in range(from_index, to_index):
            track = al.tracklist.get_track_at(i)
            if not track:
                break
            duration = track.duration if track.duration else FALLBACK_TRACK_DURATION
            if consumed + duration > budget + tolerance:
                played = max(0.0, budget - consumed)
                counted = self.played_enough(track, played)
                if counted:
                    result.extend(self._try_scrobble(i, ts))
                    al.starts[i] = ts
                logger.info("Session: %.0fs since track %d started: %s heard %.0fs (%s), "
                            "%d gap track(s) not filled",
                            budget, from_index + 1, track.title, played,
                            "counts" if counted else "too short",
                            to_index - i - (1 if counted else 0))
                return result, consumed, False
            result.extend(self._try_scrobble(i, ts, force=True))
            al.starts[i] = ts
            ts += duration
            consumed += duration
        return result, consumed, True

    # ── Compilation mode ──────────────────────────────────────────────────────

    def _return_to_album(self, resolved: Resolved,
                         now: float) -> list[tuple[TrackInfo, float]] | None:
        """Compilation mode was entered from an album; the album's own track is
        heard again: the switch was a confusion, go back where we were."""
        al = self._abandoned
        if al is None or not resolved.has_album or resolved.album_id != al.album_id:
            return None
        idx = al.tracklist.find_track_index(resolved.track.title)
        if idx is None:
            return None
        result = self._finalize_comp(now)
        self.mode = SessionMode.ALBUM
        self._album = al
        self._abandoned = None
        start = self._offset_start(al.tracklist.get_track_at(idx) or resolved.track, now,
                                   floor=al.current_track_start)
        heard = min(now, self._first_seen(resolved.track, now))
        self._set_current(idx, start if start is not None else heard, exact=start is not None)
        al.exhausted = False
        al.side_done = False
        logger.info("Session: 🔒 back on %s / %s at track %d (the compilation switch was a "
                    "confusion)", al.tracklist.artist, al.tracklist.album_name, idx + 1)
        return result

    def _handle_compilation_id(self, track: TrackInfo, now: float) -> list[tuple[TrackInfo, float]]:
        """A new confirmed track in a mix: finish the previous one, start this one."""
        if self._comp_current and self._dedup_key(track) == self._dedup_key(self._comp_current):
            return []
        result = self._finalize_comp(now)
        self._comp_current = track
        self._comp_start = self._first_seen(track, now)
        logger.info("Session: 🎛  COMPILATION now — %s — %s", track.artist, track.title)
        return result

    def _finalize_comp(self, now: float) -> list[tuple[TrackInfo, float]]:
        cur = self._comp_current
        if cur is None:
            return []
        self._comp_current = None
        if self._already_scrobbled(cur):
            return []
        if not self.played_enough(cur, now - self._comp_start):
            return []
        self._mark_scrobbled(cur)
        self._comp_count += 1
        logger.info("Session: 🎛  COMPILATION scrobble #%d — %s — %s",
                    self._comp_count, cur.artist, cur.title)
        return [(cur, self._comp_start)]

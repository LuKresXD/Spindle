"""Vinyl scrobble session: album lock + compilation mode.

Two modes:

  ALBUM       locked to one album (and, when the pressing is known, one side).
              Uses the tracklist + durations to infer and scrobble tracks the
              fingerprinter misses: backfill (before the first ID), forward
              fill (between IDs) and timing advance.

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

# ── Compilation detection ─────────────────────────────────────────────────────
MISMATCH_THRESHOLD = 2      # consecutive different-artist IDs → switch to COMPILATION
CONFIRM_THRESHOLD  = 2      # times a track must be identified before acting on it


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
class AlbumState:
    """All state for one album-mode session."""
    album_id:            str
    tracklist:           AlbumTracklist
    music_start:         float           # wall-clock time music first started
    anchors:             list = field(default_factory=list)
    current_index:       int   = 0       # best estimate of current track
    current_track_start: float = 0.0     # estimated wall-clock start of current track
    scrobbled:           set   = field(default_factory=set)  # track indices scrobbled
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
    ):
        self.min_play_seconds  = min_play_seconds
        self.min_play_fraction = min_play_fraction
        self.chunk_duration    = chunk_duration
        self._clock            = clock

        self.mode: SessionMode | None = None   # None = no session yet

        self._album: AlbumState | None = None
        self._mismatch_count: int = 0
        self._comp_count: int = 0
        self._comp_current: TrackInfo | None = None
        self._comp_start: float = 0.0

        # Session-wide dedup: (artist_lower, normalized_title)
        self._session_scrobbled: set[tuple[str, str]] = set()

        # Confirmation gate: (artist_lower, title_lower) → (hits, first_seen)
        self._confirm: dict[tuple[str, str], tuple[int, float]] = {}

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

        if not self._is_confirmed(track, now):
            return []

        if self.mode == SessionMode.COMPILATION:
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
        buffer = min(max(duration * VINYL_DRIFT, ADVANCE_BUFFER_MIN), ADVANCE_BUFFER_MAX)
        if elapsed < duration + buffer:
            return []

        result: list[tuple[TrackInfo, float]] = []
        result.extend(self._try_scrobble(al.current_index, al.current_track_start))

        if al.tracklist.is_side_end(al.current_index):
            al.side_done = True
            logger.info("Session: end of side %s — waiting for the flip",
                        al.tracklist.side_at(al.current_index))
            return result

        next_idx = al.current_index + 1
        next_track = al.tracklist.get_track_at(next_idx)
        if next_track:
            al.current_index = next_idx
            al.current_track_start = self._clock()
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
        elapsed = min(self._clock() - self._album.current_track_start, current.duration)
        return (elapsed, current.duration)

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
        self._comp_count = 0
        self._comp_current = None
        self._comp_start = 0.0
        self._session_scrobbled.clear()
        self._confirm.clear()

    # ── Confirmation gate ────────────────────────────────────────────────────

    def _is_confirmed(self, track: TrackInfo, now: float) -> bool:
        """True once this track has been identified CONFIRM_THRESHOLD times.

        Real tracks are identified again and again while they play; a
        one-off false fingerprint match never reaches the threshold.
        """
        key = (track.artist.lower(), track.title.lower())
        hits, first = self._confirm.get(key, (0, now))
        hits += 1
        self._confirm[key] = (hits, first)
        if hits < CONFIRM_THRESHOLD:
            logger.debug("Session: confirm %d/%d — '%s — %s'", hits, CONFIRM_THRESHOLD,
                         track.artist, track.title)
            return False
        return True

    def _first_seen(self, track: TrackInfo, default: float) -> float:
        key = (track.artist.lower(), track.title.lower())
        return self._confirm.get(key, (0, default))[1]

    # ── Session-level dedup ───────────────────────────────────────────────────

    def _dedup_key(self, track: TrackInfo) -> tuple[str, str]:
        return (track.artist.lower(), normalize_title(track.title))

    def _already_scrobbled(self, track: TrackInfo) -> bool:
        return self._dedup_key(track) in self._session_scrobbled

    def _mark_scrobbled(self, track: TrackInfo) -> None:
        self._session_scrobbled.add(self._dedup_key(track))

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
            current_track_start=music_start,
        )
        self.mode = SessionMode.ALBUM
        self._mismatch_count = 0

        pos = tracklist.position_at(track_index)
        logger.info("Session: 🔒 ALBUM — %s / %s  (track %d/%d%s, first ID: '%s', %s%s)",
                    tracklist.artist, tracklist.album_name, track_index + 1,
                    len(tracklist.tracks), f" {pos}" if pos else "", resolved.track.title,
                    tracklist.source, ", owned" if tracklist.owned else "")

        return self._retroactive_backfill(track_index, now)

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
            return self._apply_anchor(track_index, now)

        # Not on the locked album. Same artist: catalogue confusion, ignore.
        # Different artist: maybe a compilation.
        if same_artist(resolved.track.artist, al.tracklist.artist):
            logger.info("Session: '%s' is not on '%s' (same artist) — ignoring",
                        resolved.track.title, al.tracklist.album_name)
            return []

        self._mismatch_count += 1
        logger.info("Session: mismatch %d/%d — '%s — %s' while locked on '%s'",
                    self._mismatch_count, MISMATCH_THRESHOLD,
                    resolved.track.artist, resolved.track.title, al.tracklist.artist)
        if self._mismatch_count < MISMATCH_THRESHOLD:
            return []

        logger.info("Session: 🎛  switching to COMPILATION mode (different artists on '%s')",
                    al.tracklist.album_name)
        result = self._finalize_album_current(now)
        self.mode = SessionMode.COMPILATION
        self._album = None
        self._mismatch_count = 0
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
        start = old.current_track_start if same_track else now
        scrobbled: set[int] = set()
        for i in old.scrobbled:
            t = old.tracklist.get_track_at(i)
            j = new_tl.find_track_index(t.title) if t else None
            if j is not None:
                scrobbled.add(j)
        self._album = AlbumState(
            album_id=resolved.album_id, tracklist=new_tl, music_start=old.music_start,
            anchors=old.anchors + [Anchor(idx, now)], current_index=idx,
            current_track_start=start, scrobbled=scrobbled,
        )
        self._mismatch_count = 0
        logger.info("Session: 🔁 re-locked — %s / %s → %s (%d tracks, %s%s), now at track %d",
                    new_tl.artist, old.tracklist.album_name, new_tl.album_name,
                    len(new_tl.tracks), new_tl.source, ", owned" if new_tl.owned else "",
                    idx + 1)
        return result

    def _apply_anchor(self, track_index: int, now: float) -> list[tuple[TrackInfo, float]]:
        assert self._album is not None
        al = self._album
        al.anchors.append(Anchor(track_index, now))
        al.exhausted = False
        result: list[tuple[TrackInfo, float]] = []

        if track_index == al.current_index:
            logger.debug("Session: anchor confirms track %d ✓", track_index + 1)
            if al.side_done:
                # the side "ended" by timing but the same track is still being heard:
                # the timing model ran fast; keep showing it, the flip will come
                pass
        elif track_index > al.current_index:
            gap = self._fill_forward(al.current_index, track_index)
            result.extend(gap)
            al.current_index = track_index
            al.current_track_start = now
            al.side_done = False
            logger.info("Session: anchor → track %d (filled %d gap tracks)",
                        track_index + 1, len(gap))
        else:
            logger.warning("Session: anchor behind prediction (%d < %d), re-syncing",
                           track_index + 1, al.current_index + 1)
            al.current_index = track_index
            al.current_track_start = now
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

    def _retroactive_backfill(self, anchor_index: int,
                              anchor_time: float) -> list[tuple[TrackInfo, float]]:
        """Infer and scrobble tracks that played before the first identification.

        elapsed = anchor_time - music_start. Walk backwards from anchor - 1
        summing durations; the earliest start track for which the remaining
        time falls inside the anchor track (± tolerance) is the needle-drop
        position. Never crosses a side boundary: the music before this side
        started was a different silence-bounded session.
        """
        if not self._album or anchor_index == 0:
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
            ts += track.duration if track.duration else FALLBACK_TRACK_DURATION
        self._album.current_track_start = ts

        logger.info("Session: retroactive backfill — %d tracks from track %d "
                    "(%.0fs before first ID)", len(result), best_start + 1, elapsed)
        for track, _ in result:
            logger.info("  ↳ %s — %s", track.artist, track.title)
        return result

    def _fill_forward(self, from_index: int, to_index: int) -> list[tuple[TrackInfo, float]]:
        """Scrobble tracks from from_index up to (not including) to_index."""
        if not self._album:
            return []
        result: list[tuple[TrackInfo, float]] = []
        ts = self._album.current_track_start
        for i in range(from_index, to_index):
            track = self._album.tracklist.get_track_at(i)
            if not track:
                break
            result.extend(self._try_scrobble(i, ts, force=True))
            ts += track.duration if track.duration else FALLBACK_TRACK_DURATION
        return result

    # ── Compilation mode ──────────────────────────────────────────────────────

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

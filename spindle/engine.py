"""The player: one `tick()` every STEP seconds, one `ScreenState` out.

This is the old 400-line main loop pulled apart into named steps with the
loop state as attributes instead of locals. The engine does not record
audio itself (that is `ContinuousCapture`, running in its own thread) and
does not draw (that is `Display`); it decides what is happening and tells
everyone.

Tick outline:
  1. level = loudness of the last STEP seconds
  2. quiet for >= silence.min_silence_seconds  -> end the session (IDLE)
     loud after quiet                           -> music started (LISTENING)
  3. while music plays:
       a. timing advance for a locked album (session.check_advance)
       b. identify the last window, at a lower cadence once an album is locked
       c. feed the session, scrobble, update now-playing, notify
  4. publish ScreenState
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from .art import Art, ArtProvider
from .capture import DEFAULT_STEP, ContinuousCapture
from .config import SpindleConfig
from .fingerprint import TrackInfo
from .history import ScrobbleHistory
from .notify import Notifier
from .scrobbler import Scrobbler
from .session import ScrobbleSession, SessionMode
from .spotify import SpotifyClient, SpotifyTrack
from .state import Phase, ScreenState, TrackRow

logger = logging.getLogger(__name__)

IdentifyFn = Callable[[Path], TrackInfo | None]

# Once an album is locked the timing model carries the session; fresh
# fingerprints only serve as anchors, so there is no need to hit AcoustID
# (and Shazam on every miss) every 2 s.
LOCKED_IDENTIFY_INTERVAL = 8.0


class Extras:
    """Optional enrichments the screen shows. Plugged in by cli; all may be None."""

    def __init__(self) -> None:
        self.position: Callable[[str, str, str], tuple[str, str]] | None = None
        self.album_playcount: Callable[[str, str], int | None] | None = None
        self.year: Callable[[str, str], str] | None = None


class Player:
    def __init__(
        self,
        cfg: SpindleConfig,
        capture: ContinuousCapture,
        identify: IdentifyFn,
        *,
        session: ScrobbleSession | None,
        spotify: SpotifyClient | None,
        scrobbler: Scrobbler | None,
        notifier: Notifier,
        history: ScrobbleHistory,
        art: ArtProvider | None = None,
        extras: Extras | None = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
        step: float = DEFAULT_STEP,
    ) -> None:
        self.cfg = cfg
        self.capture = capture
        self.identify = identify
        self.session = session
        self.spotify = spotify
        self.scrobbler = scrobbler
        self.notifier = notifier
        self.history = history
        self.art = art
        self.extras = extras or Extras()
        self.dry_run = dry_run
        self.clock = clock
        self.step = step

        self.state = ScreenState()
        self.phase = Phase.IDLE
        self.level_db = -120.0
        self.music_since: float | None = None      # when the current music run began
        self.silent_since: float | None = None     # when the current quiet run began
        self.last_identify_at = 0.0

        # what we believe is playing (any mode)
        self.current_track: TrackInfo | None = None
        self.current_art: Art | None = None
        self.track_start = 0.0                      # simple mode only
        self.track_scrobbled = False
        self._playcount_cache: dict[tuple[str, str], int | None] = {}

    # ── public ───────────────────────────────────────────────────────────────

    def tick(self) -> ScreenState:
        now = self.clock()
        self.level_db = self.capture.level_db(self.step)
        quiet = self.level_db < self.cfg.silence.threshold_db

        if quiet:
            self._quiet_tick(now)
        else:
            self._music_tick(now)

        self.state = self._build_state(now)
        return self.state

    def close(self) -> None:
        """Flush what is still playing (process shutdown)."""
        if self.session:
            for t, ts in self.session.on_silence():
                self._scrobble(t, ts)
        elif self.current_track and not self.track_scrobbled:
            self._finalize_simple(self.current_track, self.track_start)

    # ── silence / music transitions ──────────────────────────────────────────

    def _quiet_tick(self, now: float) -> None:
        if self.silent_since is None:
            self.silent_since = now
        if self.phase == Phase.IDLE:
            return
        if now - self.silent_since < self.cfg.silence.min_silence_seconds:
            return                                   # a pause, not the end
        self._end_session(now)

    def _end_session(self, now: float) -> None:
        logger.info("Silence: session over (%.0fs quiet)", now - (self.silent_since or now))
        if self.session:
            al = self.session.album_state
            al_artist = al.tracklist.artist if al else None
            al_album = al.tracklist.album_name if al else None
            comp_count = self.session.comp_scrobbled_count   # read before reset()
            for t, ts in self.session.on_silence():
                self._scrobble(t, ts)
            if al_artist and al and al.scrobbled:
                self.notifier.side_finished(al_artist, al_album or "",
                                            len(al.scrobbled), al.current_index + 1)
            elif comp_count > 0:
                self.notifier.compilation_finished(comp_count)
        elif self.current_track and not self.track_scrobbled:
            self._finalize_simple(self.current_track, self.track_start)

        self.phase = Phase.IDLE
        self.music_since = None
        self.current_track = None
        self.current_art = None
        self.track_scrobbled = False
        self.last_identify_at = 0.0

    def _music_tick(self, now: float) -> None:
        self.silent_since = None
        if self.phase == Phase.IDLE:
            self.music_since = now - self.step
            self.phase = Phase.LISTENING
            logger.info("Music started (%.1f dB)", self.level_db)

        self._timing_advance()
        self._maybe_identify(now)

        if self.current_track is not None:
            self.phase = Phase.PLAYING

    # ── album-lock timing ────────────────────────────────────────────────────

    def _timing_advance(self) -> None:
        s = self.session
        if not (s and s.is_locked()):
            return
        al = s.album_state
        prev_idx = al.current_index if al else -1
        advanced = s.check_advance()
        for t, ts in advanced:
            self._scrobble(t, ts)
        predicted = s.get_current_track()
        if not predicted:
            return
        self.current_track = predicted
        self.track_scrobbled = True
        self._now_playing(predicted)
        al = s.album_state
        if advanced and al and al.current_index != prev_idx:
            self.notifier.track_advanced(
                al.tracklist.artist, al.tracklist.album_name, predicted.title,
                al.current_index + 1, len(al.tracklist.tracks),
            )

    # ── identification ───────────────────────────────────────────────────────

    def _maybe_identify(self, now: float) -> None:
        locked = bool(self.session and self.session.is_locked())
        if locked and now - self.last_identify_at < LOCKED_IDENTIFY_INTERVAL:
            return
        if self.capture.buffered_seconds() < self.capture.window_seconds:
            return
        wav = self.capture.window_wav()
        if wav is None:
            return
        self.last_identify_at = now
        try:
            track = self.identify(wav)
        finally:
            wav.unlink(missing_ok=True)

        if not track:
            if locked:
                predicted = self.session.get_current_track() if self.session else None
                if predicted:
                    self._now_playing(predicted)
            return

        self._apply_identification(track, now)

    def _apply_identification(self, track: TrackInfo, now: float) -> None:
        spotify_result: SpotifyTrack | None = None
        if self.spotify:
            spotify_result = self.spotify.lookup(track.artist, track.title)
            if spotify_result:
                track = spotify_result.track

        self._update_art(track, spotify_result)

        s = self.session
        if s and spotify_result and not self.dry_run:
            was_locked = s.is_locked()
            was_mode = s.mode
            for t, ts in s.on_identified(spotify_result, self.music_since or now):
                self._scrobble(t, ts, backfill=True)
            if s.is_locked() and not was_locked:
                al = s.album_state
                if al:
                    self.notifier.album_locked(
                        al.tracklist.artist, al.tracklist.album_name,
                        al.current_index + 1, len(al.tracklist.tracks), track.title,
                        art_url=spotify_result.album_art_url or None,
                    )
            if s.mode == SessionMode.COMPILATION and was_mode != SessionMode.COMPILATION:
                logger.info("Session: compilation mode active (per-track scrobble only)")

        if self.dry_run:
            self._log_dry_run(track)
            self.current_track = (s.get_current_track() if s and s.is_locked() else None) or track
            return

        if s and s.is_locked():
            predicted = s.get_current_track() or track
            self._now_playing(predicted)
            self.current_track = predicted
            self.track_scrobbled = True
        elif s and s.mode == SessionMode.COMPILATION:
            self._now_playing(track)
            self.current_track = track
            self.track_scrobbled = True
        else:
            self._simple_mode(track, now)

    def _simple_mode(self, track: TrackInfo, now: float) -> None:
        """No Spotify (or no match): scrobble by elapsed time alone."""
        cur = self.current_track
        if cur is None or _key(track) != _key(cur):
            if cur and not self.track_scrobbled:
                self._finalize_simple(cur, self.track_start)
            self.current_track = track
            self.track_start = now
            self.track_scrobbled = False
            logger.info("Now playing: %s - %s", track.artist, track.title)
            self._now_playing(track)
        else:
            if not self.track_scrobbled and self._eligible(cur, self.track_start, now):
                self._scrobble(cur, self.track_start)
                self.track_scrobbled = True
            self._now_playing(track)

    def _update_art(self, track: TrackInfo, spotify_result: SpotifyTrack | None) -> None:
        if not self.art:
            return
        album = track.album or (spotify_result.album_name if spotify_result else "") or ""
        if self.current_art and self._art_key == (track.artist, album):
            return
        url = spotify_result.album_art_url if spotify_result else ""
        got = self.art.get(track.artist, album, url)
        if got:
            self.current_art = got
            self._art_key = (track.artist, album)

    _art_key: tuple[str, str] = ("", "")

    # ── scrobbling plumbing ──────────────────────────────────────────────────

    def _eligible(self, t: TrackInfo, start: float, now: float) -> bool:
        played = now - start
        if played < self.cfg.scrobble.min_play_seconds:
            return False
        return not (t.duration and played < t.duration * self.cfg.scrobble.min_play_fraction)

    def _scrobble(self, t: TrackInfo, timestamp: float, backfill: bool = False) -> None:
        if self.scrobbler and not self.dry_run:
            self.scrobbler.scrobble(t, timestamp=int(timestamp))
            self.history.log(t, timestamp, source="backfill" if backfill else "live")
            self.notifier.track_scrobbled(t, is_backfill=backfill)
            self._playcount_cache.pop((t.artist.lower(), (t.album or "").lower()), None)

    def _now_playing(self, t: TrackInfo) -> None:
        if self.scrobbler and not self.dry_run:
            self.scrobbler.update_now_playing(t)

    def _finalize_simple(self, t: TrackInfo, start: float) -> None:
        if self.dry_run:
            return
        if self._eligible(t, start, self.clock()):
            self._scrobble(t, start)

    def _log_dry_run(self, track: TrackInfo) -> None:
        line = f"{track.artist} - {track.title}"
        if track.album:
            line += f" [{track.album}]"
        line += f" (via {track.source}, {track.confidence:.0%})"
        s = self.session
        if s and s.is_locked():
            progress = s.get_progress()
            if progress:
                line += f" album-lock {progress[0]:.0f}s/{progress[1]:.0f}s"
        if s and s.mode == SessionMode.COMPILATION:
            line += " mode=COMPILATION"
        logger.info("DRY RUN: %s", line)

    # ── screen state ─────────────────────────────────────────────────────────

    def _build_state(self, now: float) -> ScreenState:
        st = ScreenState(phase=self.phase, level_db=self.level_db, updated_at=now)
        if self.phase != Phase.PLAYING or self.current_track is None:
            return st

        t = self.current_track
        st.artist, st.title, st.album = t.artist, t.title, t.album or ""
        if self.current_art:
            st.art, st.art_source = self.current_art.data, self.current_art.source

        s = self.session
        al = s.album_state if s and s.mode == SessionMode.ALBUM else None
        if al:
            st.artist = al.tracklist.artist or st.artist
            st.album = al.tracklist.album_name or st.album
            st.tracklist = [
                TrackRow(title=tr.title, duration=tr.duration, scrobbled=i in al.scrobbled)
                for i, tr in enumerate(al.tracklist.tracks)
            ]
            st.current_index = al.current_index
            st.year = al.tracklist.year
            cur = al.tracklist.get_track_at(al.current_index)
            st.track_duration = float(cur.duration) if cur and cur.duration else None
            st.track_elapsed = max(0.0, now - al.current_track_start)
            st.scrobbled = al.current_index in al.scrobbled
            st.session_scrobbles = len(al.scrobbled)
        else:
            st.track_duration = float(t.duration) if t.duration else None
            if self.track_start:
                st.track_elapsed = max(0.0, now - self.track_start)
            st.scrobbled = self.track_scrobbled
            st.session_scrobbles = s.comp_scrobbled_count if s else int(self.track_scrobbled)

        self._enrich(st)
        return st

    def _enrich(self, st: ScreenState) -> None:
        ex = self.extras
        try:
            if ex.position and st.album:
                side, pos = ex.position(st.artist, st.album, st.title)
                st.side, st.position = side, pos
                if st.tracklist and pos:
                    for row in st.tracklist:
                        _, rpos = ex.position(st.artist, st.album, row.title)
                        row.position = rpos
            if ex.album_playcount and st.album:
                key = (st.artist.lower(), st.album.lower())
                if key not in self._playcount_cache:
                    self._playcount_cache[key] = ex.album_playcount(st.artist, st.album)
                st.album_playcount = self._playcount_cache[key]
            if ex.year and st.album and not st.year:
                st.year = ex.year(st.artist, st.album)
        except Exception as e:  # enrichment must never take the screen down
            logger.debug("Extras failed: %s", e)


def _key(t: TrackInfo) -> str:
    return f"{t.artist.lower()} - {t.title.lower()}"

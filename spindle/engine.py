"""The player: one `tick()` every STEP seconds, one `ScreenState` out.

The engine decides what is happening and tells everyone; it does not record
(`ContinuousCapture`), draw (`Display`), or wait on the network. Everything
slow runs elsewhere and reports back:

  * identification + resolution + cover art: `IdentifyWorker` (own thread)
  * Last.fm calls: the `Scrobbler`'s outbox thread
  * Discogs: its own refresh thread; lookups here are memory-only
  * play counts: a one-worker executor, results read from a cache

Tick outline:
  1. level = loudness of the last STEP seconds (capture down → hold)
  2. quiet for >= silence.min_silence_seconds  -> end the session (IDLE)
     loud after quiet (with hysteresis)        -> music started (LISTENING)
  3. while music plays:
       a. timing advance for a locked album (session.check_advance)
       b. drain finished identifications, apply each at the time its audio ended
       c. submit a new window to the worker when it is idle (slower once locked)
  4. publish ScreenState
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .art import Art, ArtProvider
from .capture import DEFAULT_STEP, ContinuousCapture, is_runout
from .config import SpindleConfig
from .fingerprint import TrackInfo
from .history import ScrobbleHistory
from .resolve import ResolveContext, Resolved, Resolver
from .scrobbler import Scrobbler
from .session import (
    FALLBACK_TRACK_DURATION,
    ScrobbleSession,
    SessionMode,
    _artist_key,
    normalize_title,
)
from .state import Phase, ScreenState, TrackRow
from .titles import clean_album, split_title

logger = logging.getLogger(__name__)

IdentifyFn = Callable[[Path], TrackInfo | None]

# Once an album is locked the timing model carries the session; fresh
# fingerprints only serve as anchors, so there is no need to hit AcoustID
# (and Shazam on every miss) every 2 s.
LOCKED_IDENTIFY_INTERVAL = 8.0
UNRESOLVED_HOLD_S = 90.0      # a hit nobody knows must be the only answer this long before it shows
RUNOUT_TICKS = 3              # consecutive ticks of once-a-revolution thumps that end a side
HOT_INPUT_DB = -6.0          # RMS this high means the capture is clipping

# Loud is not enough to be music: an empty turntable (motor, preamp, mains) can
# sit above the threshold for as long as it spins, with a spectrum that never
# moves. Music scores well above this on `capture.motion()`; the measured
# empty-turntable noise scored 0.05-0.07, the steadiest music 0.29, and a dense
# drum-and-bass side (Pendulum) started at 0.15: the gate sits well under that.
MUSIC_MOTION_MIN = 0.11
# A record that really is this steady (a drone, a test tone) would never pass
# the gate, so a steady input still gets a lookup now and then, backing off
# while nothing is recognised; a hit turns the run into a session.
NOISE_IDENTIFY_FIRST = 20.0
NOISE_IDENTIFY_MAX = 300.0
# A track scrobbled from this record this recently still shows its tick after the
# flip (the session itself resets on silence so a second play of a side counts).
RECENT_PLAY_WINDOW = 6 * 3600.0
RECENT_PLAY_REFRESH = 30.0


# ── Identification worker ────────────────────────────────────────────────────

@dataclass
class IdentifyJob:
    wav: Path
    ctx: ResolveContext
    at: float                       # wall-clock time the audio window ended
    art_hint: str = ""


@dataclass
class IdentifyResult:
    at: float
    raw: TrackInfo | None
    resolved: Resolved | None = None
    art: Art | None = None
    took: float = 0.0


class IdentifyWorker:
    """One job in flight at a time: identify -> resolve -> cover art, off the tick thread."""

    def __init__(self, identify: IdentifyFn, resolver: Resolver | None,
                 art: ArtProvider | None = None, threaded: bool = True):
        self.identify = identify
        self.resolver = resolver
        self.art = art
        self.threaded = threaded
        self._jobs: queue.Queue[IdentifyJob | None] = queue.Queue(maxsize=1)
        self._results: queue.Queue[IdentifyResult] = queue.Queue()
        self._busy = threading.Event()
        self._thread: threading.Thread | None = None
        if threaded:
            self._thread = threading.Thread(target=self._run, name="identify", daemon=True)
            self._thread.start()

    @property
    def busy(self) -> bool:
        return self._busy.is_set()

    def submit(self, job: IdentifyJob) -> bool:
        """Accept the job if idle. The worker owns (and deletes) the WAV from here on."""
        if self._busy.is_set():
            return False
        self._busy.set()
        if not self.threaded:
            self._results.put(self._work(job))
            self._busy.clear()
            return True
        self._jobs.put(job)
        return True

    def results(self) -> list[IdentifyResult]:
        out = []
        while True:
            try:
                out.append(self._results.get_nowait())
            except queue.Empty:
                return out

    def close(self) -> None:
        if self._thread:
            self._jobs.put(None)
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            try:
                self._results.put(self._work(job))
            except Exception as e:  # never kill the worker
                logger.error("identify worker: %s", e, exc_info=True)
                self._results.put(IdentifyResult(job.at, None))
            finally:
                self._busy.clear()

    def _work(self, job: IdentifyJob) -> IdentifyResult:
        t0 = time.monotonic()
        try:
            raw = self.identify(job.wav)
        finally:
            job.wav.unlink(missing_ok=True)
        if raw is None:
            return IdentifyResult(job.at, None, took=time.monotonic() - t0)
        resolved = self.resolver.resolve(raw, job.ctx) if self.resolver else Resolved(raw)
        art = None
        if self.art is not None:
            album = resolved.track.album or (resolved.tracklist.album_name
                                             if resolved.tracklist else "") or ""
            try:
                art = self.art.get(resolved.track.artist, album, resolved.art_url)
            except Exception as e:
                logger.debug("Art fetch failed: %s", e)
        return IdentifyResult(job.at, raw, resolved, art, time.monotonic() - t0)


# ── Extras ───────────────────────────────────────────────────────────────────

class Extras:
    """Optional screen enrichments, plugged in by cli; each may be None."""

    def __init__(self) -> None:
        self.position: Callable[[str, str, str], tuple[str, str]] | None = None
        self.album_playcount: Callable[[str, str], int | None] | None = None
        self.year: Callable[[str, str], str] | None = None


# ── Player ───────────────────────────────────────────────────────────────────

@dataclass
class _Playing:
    """What we believe is playing right now (any mode)."""
    track: TrackInfo | None = None
    since: float = 0.0              # when this track (or the music, if unknown) started
    scrobbled: bool = False
    simple: bool = False            # came from simple mode (no album known)
    art: Art | None = None
    art_key: tuple = field(default_factory=lambda: ("", ""))



def _track_row(tr: TrackInfo, position: str, scrobbled: bool) -> TrackRow:
    parts = split_title(tr.title, tr.artists or [tr.artist])
    return TrackRow(title=tr.title, duration=tr.duration, position=position, scrobbled=scrobbled,
                    base=parts.base, featured=list(parts.featured), version=parts.version)


class Player:
    def __init__(
        self,
        cfg: SpindleConfig,
        capture: ContinuousCapture,
        worker: IdentifyWorker,
        *,
        session: ScrobbleSession | None,
        scrobbler: Scrobbler | None,
        history: ScrobbleHistory,
        extras: Extras | None = None,
        dry_run: bool = False,
        clock: Callable[[], float] = time.time,
        step: float = DEFAULT_STEP,
    ) -> None:
        self.cfg = cfg
        self.capture = capture
        self.worker = worker
        self.session = session
        self.scrobbler = scrobbler
        self.history = history
        self.extras = extras or Extras()
        self.dry_run = dry_run
        self.clock = clock
        self.step = step

        self.state = ScreenState()
        self.phase = Phase.IDLE
        self.level_db = -120.0
        self.music_since: float | None = None      # when the current music run began
        self.silent_since: float | None = None     # when the current quiet run began
        self.last_submit_at = 0.0
        self._loud_ticks = 0
        self._hot_warned = False
        self._capture_down_logged = False
        self.motion: float | None = None           # last spectral-motion reading at the gate
        self.noise_since: float | None = None      # loud-but-steady input began (not music)
        self._noise_identify_at = 0.0
        self._noise_identify_interval = NOISE_IDENTIFY_FIRST
        self._id_disagreed = False                 # last ID named a track the clock is not on
        self._held: tuple[str, float] | None = None   # (key, first seen) of a hit nobody knows
        self._runout_ticks = 0                     # consecutive ticks that sounded like a lead-out
        self._recent_titles: set[str] = set()      # this artist's titles scrobbled recently
        self._recent_key = ""
        self._recent_at = 0.0
        # called with a summary dict when a session ends (the touch display's end card)
        self.side_listeners: list[Callable[[dict], None]] = []

        self.playing = _Playing()
        self._playcount_cache: dict[tuple[str, str], int | None] = {}
        self._playcount_pending: set[tuple[str, str]] = set()
        self._bg = ThreadPoolExecutor(max_workers=1, thread_name_prefix="enrich")

    # ── compatibility accessors (bot, tests) ─────────────────────────────────

    @property
    def current_track(self) -> TrackInfo | None:
        return self.playing.track

    @property
    def track_scrobbled(self) -> bool:
        return self.playing.scrobbled

    # ── public ───────────────────────────────────────────────────────────────

    def tick(self) -> ScreenState:
        now = self.clock()
        if not getattr(self.capture, "running", True):
            # no audio is arriving: hold everything rather than hearing "silence"
            if not self._capture_down_logged:
                logger.warning("Capture is down: holding state until audio returns")
                self._capture_down_logged = True
            return self._publish(now)
        self._capture_down_logged = False

        self.level_db = self.capture.level_db(self.step)
        sil = self.cfg.silence
        quiet = self.level_db < sil.threshold_db

        # Hysteresis: a run-out groove or surface noise sits right around the
        # threshold, so STARTING a session needs threshold + margin for a few
        # consecutive ticks, while ENDING one only needs the plain threshold
        # for min_silence_seconds.
        if self.phase == Phase.IDLE:
            if self.level_db >= sil.threshold_db + sil.start_margin_db:
                self._loud_ticks = min(self._loud_ticks + 1, max(1, sil.start_ticks))
            else:
                self._loud_ticks = 0
            if self._loud_ticks < max(1, sil.start_ticks):
                self._quiet_tick(now)
                return self._publish(now)
            # loud enough for long enough: music, or a turntable running empty, or the
            # needle thumping round the lead-out after a side?
            if not self._music_like() or self._in_runout(now):
                self._noise_tick(now)
                return self._publish(now)

        if quiet:
            self._quiet_tick(now)
        elif self.phase != Phase.IDLE and self._in_runout(now) and self._side_can_end():
            # the needle reached the lead-out: the side is over now, not when it is lifted
            logger.info("Run-out: side over (needle in the lead-out, %.1f dB)", self.level_db)
            self._end_session(now)
            self._noise_tick(now)                  # and the thumps that follow are not a session
        else:
            self._music_tick(now)
        return self._publish(now)

    def _publish(self, now: float) -> ScreenState:
        self.state = self._build_state(now)
        return self.state

    def close(self) -> None:
        """Flush what is still playing (process shutdown)."""
        now = self.clock()
        if self.session:
            for t, ts in self.session.on_silence():
                self._scrobble(t, ts)
        self._finalize_simple(now)
        self._bg.shutdown(wait=False)

    # ── silence / music transitions ──────────────────────────────────────────

    def _quiet_tick(self, now: float) -> None:
        if self.silent_since is None:
            self.silent_since = now
        if self.phase == Phase.IDLE:
            if self.noise_since is not None:
                logger.info("Steady noise stopped after %.0fs", now - self.noise_since)
                self._end_noise_run()
            return
        # a pause, not the end: unless the clock already ran the side's last track out, in
        # which case the first quiet tick is the end (Luka, 2026-11-08: "it knows the track
        # ended and hears it, switch instantly")
        if now - self.silent_since < self.cfg.silence.min_silence_seconds \
                and not self._side_over_by_clock():
            return
        self._end_session(now)

    def _in_runout(self, now: float) -> bool:
        """RUNOUT_TICKS consecutive ticks whose loudness repeats once per revolution and is
        quiet in between (see capture.revolution_pulse). A capture without the reading
        never reports one."""
        reader = getattr(self.capture, "runout", None)
        if reader is None:
            return False
        pulse, duty = reader()
        if is_runout(pulse, duty):
            self._runout_ticks += 1
            if self._runout_ticks == 1:
                logger.info("Input thumps once a revolution (pulse %.2f, duty %.2f): the lead-out",
                            pulse, duty)
        else:
            self._runout_ticks = 0
        # one tick is enough once the clock has run the side's last track out
        need = 1 if self._side_over_by_clock() else RUNOUT_TICKS
        return self._runout_ticks >= need

    def _side_over_by_clock(self) -> bool:
        """The timing model has already run the side's last track to its end."""
        s = self.session
        al = s.album_state if s and s.mode == SessionMode.ALBUM else None
        return bool(al and (al.side_done or al.exhausted))

    def _side_can_end(self) -> bool:
        """A lead-out only comes at the end of a side. With a pressing the clock has to be
        on the side's last track (or past it); with a streaming tracklist (no sides) the
        current track must be at least half played, or the last one; without a lock any
        time is plausible."""
        s = self.session
        al = s.album_state if s and s.mode == SessionMode.ALBUM else None
        if al is None:
            return True
        if al.side_done or al.exhausted:
            return True
        tl = al.tracklist
        if tl.has_sides:
            return tl.is_side_end(al.current_index)
        cur = tl.get_track_at(al.current_index)
        dur = cur.duration if cur and cur.duration else FALLBACK_TRACK_DURATION
        return (al.current_index >= len(tl.tracks) - 1
                or self.clock() - al.current_track_start >= 0.5 * dur)

    def _music_like(self) -> bool:
        """Does the recent audio move like music? Fail open: with no reading
        (too little audio buffered) music must never be kept off the screen."""
        self.motion = self.capture.motion()
        return self.motion is None or self.motion >= MUSIC_MOTION_MIN

    def _noise_tick(self, now: float) -> None:
        """Loud, but the spectrum stands still: the turntable is running with
        nothing on it (or something equally steady). The screen stays black and
        no session starts; a slow lookup keeps running in case it is a record
        that really sounds like that, and a hit starts the session from the
        moment the input got loud."""
        self.silent_since = None
        if self.noise_since is None:
            self.noise_since = now - self.step * max(1, self._loud_ticks)
            self._noise_identify_interval = NOISE_IDENTIFY_FIRST
            self._noise_identify_at = self.noise_since + NOISE_IDENTIFY_FIRST
            logger.info("Input is steady noise (%.1f dB RMS, motion %.2f < %.2f): "
                        "not music, staying idle", self.level_db, self.motion or 0.0,
                        MUSIC_MOTION_MIN)
        for res in self.worker.results():
            if res.resolved is not None:
                logger.info("Steady input identified as %s — %s: music after all",
                            res.resolved.track.artist, res.resolved.track.title)
                self.music_since = self.noise_since
                self._end_noise_run()
                self.phase = Phase.LISTENING
                self._apply_result(res)
                if self.playing.track is not None:
                    self.phase = Phase.PLAYING
                return
        if now >= self._noise_identify_at and self._maybe_submit(now):
            self._noise_identify_interval = min(self._noise_identify_interval * 2,
                                                NOISE_IDENTIFY_MAX)
            self._noise_identify_at = now + self._noise_identify_interval

    def _end_noise_run(self) -> None:
        self.noise_since = None
        self._noise_identify_at = 0.0
        self._noise_identify_interval = NOISE_IDENTIFY_FIRST

    def _end_session(self, now: float) -> None:
        logger.info("Silence: session over (%.0fs quiet)", now - (self.silent_since or now))
        # late identifications belong to the session that just ended; drop them
        for res in self.worker.results():
            logger.debug("Dropping identification that arrived after silence (%s)",
                         res.raw.title if res.raw else "no match")
        if self.session:
            al = self.session.album_state
            al_scrobbled = len(al.scrobbled) if al else 0
            for t, ts in self.session.on_silence():
                self._scrobble(t, ts)
            if al is not None:
                al_scrobbled = len(al.scrobbled)
            if al is not None and al_scrobbled:
                self._announce_side_end(al, al_scrobbled)
            if self.session.deck_speed_samples >= 3:
                logger.info("Deck speed: %+.1f%% (%d offset pairs)",
                            (self.session.deck_speed() - 1) * 100,
                            self.session.deck_speed_samples)
        self._finalize_simple(now)

        self.phase = Phase.IDLE
        self._loud_ticks = 0
        self._hot_warned = False
        self.music_since = None
        self._end_noise_run()
        self.playing = _Playing()
        self.last_submit_at = 0.0
        self._id_disagreed = False
        self._held = None
        # _runout_ticks is kept: the thumps that ended the side go on, and the idle gate
        # must know them for what they are from its first tick
        self._recent_at = 0.0                      # the side that just ended adds ticks

    def _music_tick(self, now: float) -> None:
        self.silent_since = None
        if self.phase == Phase.IDLE:
            # the session began when the loud run began (a steady start that
            # then came alive still counts from its first loud tick), not now
            if self.noise_since is not None:
                self.music_since = self.noise_since
                self._end_noise_run()
            else:
                self.music_since = now - self.step * max(1, self._loud_ticks)
            self.phase = Phase.LISTENING
            logger.info("Music started (%.1f dB, motion %.2f)", self.level_db,
                        self.motion if self.motion is not None else float("nan"))
        if self.level_db > HOT_INPUT_DB and not self._hot_warned:
            self._hot_warned = True
            logger.warning("Input is hot (%.1f dB RMS): the capture is probably clipping; "
                           "lower the preamp or adapter gain", self.level_db)

        self._timing_advance()
        for res in self.worker.results():
            self._apply_result(res)
        self._maybe_submit(now)
        for res in self.worker.results():          # a synchronous worker answers at once
            self._apply_result(res)

        if self.playing.track is not None:
            self.phase = Phase.PLAYING

    # ── album-lock timing ────────────────────────────────────────────────────

    def _timing_advance(self) -> None:
        s = self.session
        if not (s and s.is_locked()):
            return
        advanced = s.check_advance()
        for t, ts in advanced:
            self._scrobble(t, ts)
        predicted = s.get_current_track()
        if not predicted:
            return
        self._set_playing(predicted, s.current_track_start() or self.clock(), scrobbled=True)
        self._now_playing(predicted)

    # ── identification ───────────────────────────────────────────────────────

    def _maybe_submit(self, now: float) -> bool:
        """Hand the current window to the identify worker when it is free.
        Returns True when a job was submitted."""
        if self.worker.busy:
            return False
        settled = bool(self.session and (self.session.is_locked()
                                         or self.session.mode == SessionMode.COMPILATION))
        # settled (album or mix), or a hit nobody knows (held or accepted): identify slowly
        # (the same answer twenty times in forty seconds got Shazam to time out, 2026-11-08).
        # A known album still waiting for its confirming ID keeps the fast cadence. One
        # hurried window right after a hit that disagreed with the clock (the record
        # skipped, the needle moved).
        slow = settled or self._held is not None
        if slow and not self._id_disagreed \
                and now - self.last_submit_at < LOCKED_IDENTIFY_INTERVAL:
            return False
        self._id_disagreed = False
        if self.capture.buffered_seconds() < self.capture.window_seconds:
            return False
        wav = self.capture.window_wav()
        if wav is None:
            return False
        ctx = ResolveContext(
            locked_album_id=self.session.locked_album_id if self.session else "",
            locked_artist=self.session.locked_artist if self.session else "",
        )
        if self.worker.submit(IdentifyJob(wav, ctx, now)):
            self.last_submit_at = now
            return True
        wav.unlink(missing_ok=True)
        return False

    def _apply_result(self, res: IdentifyResult) -> None:
        if res.took > 15:
            logger.warning("Identification took %.0fs", res.took)
        if res.resolved is None:
            if self.session and self.session.is_locked():
                predicted = self.session.get_current_track()
                if predicted:
                    self._now_playing(predicted)
            return
        self._apply_identification(res.resolved, res.at, res.art)

    def _hold_unresolved(self, resolved: Resolved, at: float) -> bool:
        """True while a hit that neither Spotify nor the collection knows is kept off the
        screen and Last.fm. Shazam's catalogue carries user uploads (mashups, "Air Max
        mixes") that fingerprint like the record on the platter; one of them was on the
        screen and scrobbled within a minute of the needle drop (2026-11-08). Such a hit
        has to be the only answer for UNRESOLVED_HOLD_S first; a hit the catalogues know,
        or any track of an established mix, passes at once."""
        s = self.session
        in_mix = bool(s and s.mode == SessionMode.COMPILATION)
        if resolved.has_album or resolved.reason != "none" or in_mix or self.dry_run:
            self._held = None
            return False
        key = _key(resolved.track)
        if self._held is None or self._held[0] != key:
            self._held = (key, at)
            logger.info("Holding %s - %s: not on Spotify or in the collection; shown after "
                        "%.0fs of the same answer", resolved.track.artist, resolved.track.title,
                        UNRESOLVED_HOLD_S)
        return at - self._held[1] < UNRESOLVED_HOLD_S

    def _apply_identification(self, resolved: Resolved, at: float, art: Art | None) -> None:
        track = resolved.track
        s = self.session

        if self._hold_unresolved(resolved, at):
            return
        held_since = self._held[1] if self._held is not None else None

        if s and resolved.has_album and not self.dry_run:
            was_locked = s.is_locked()
            was_mode = s.mode
            out = s.on_identified(resolved, self.music_since or at, at=at)
            al = s.album_state if s.is_locked() else None
            if al is not None:
                idx = al.tracklist.find_track_index(track.title)
                self._id_disagreed = idx is not None and idx != al.current_index
            if s.is_locked() and not was_locked:
                self._finalize_simple(at)             # a simple-mode track gave way to the lock
            for t, ts in out:
                self._scrobble(t, ts, backfill=True)
            if s.mode == SessionMode.COMPILATION and was_mode != SessionMode.COMPILATION:
                logger.info("Session: compilation mode active (per-track scrobble only)")
        elif s and s.mode == SessionMode.COMPILATION and not self.dry_run:
            for t, ts in s.on_identified(resolved, self.music_since or at, at=at):
                self._scrobble(t, ts)

        if self.dry_run:
            self._log_dry_run(track)
            cur = (s.get_current_track() if s and s.is_locked() else None) or track
            self._set_playing(cur, at)
            self._take_art(art, track)
            return

        if s and s.is_locked():
            predicted = s.get_current_track() or track
            self._set_playing(predicted, s.current_track_start() or at, scrobbled=True)
            self._now_playing(predicted)
        elif s and s.mode == SessionMode.COMPILATION:
            cur = s.get_current_track() or track
            self._set_playing(cur, s.current_track_start() or at, scrobbled=True)
            self._now_playing(cur)
        else:
            self._simple_mode(track, at, since=held_since)
        self._take_art(art, track)

    def _take_art(self, art: Art | None, identified: TrackInfo) -> None:
        """Keep the cover only when the identified track is the one we believe is
        playing: a sample at the end of a song (the O'Jays inside Drake, 2026-11-06)
        identifies as its source, the session rightly ignores it, and the screen
        must not change its cover either."""
        if art is None:
            return
        cur = self.playing.track
        if cur is None:
            return
        same = (normalize_title(cur.title) == normalize_title(identified.title)
                and _artist_key(cur.artist) == _artist_key(identified.artist))
        if same or (cur.album and identified.album
                    and cur.album.lower() == identified.album.lower()):
            self.playing.art = art

    def _simple_mode(self, track: TrackInfo, now: float, since: float | None = None) -> None:
        """No album known: scrobble by elapsed time alone. `since` is when the track was
        first heard when that is earlier than now (a hit held back until it persisted)."""
        p = self.playing
        cur = p.track
        if cur is None or not p.simple or _key(track) != _key(cur):
            self._finalize_simple(now)
            self._set_playing(track, now if since is None else since, scrobbled=False, simple=True)
            logger.info("Now playing: %s - %s", track.artist, track.title)
            self._now_playing(track)
        else:
            if not p.scrobbled and self._eligible(cur, p.since, now):
                self._scrobble(cur, p.since)
                p.scrobbled = True
            self._now_playing(track)

    def _set_playing(self, track: TrackInfo, since: float, scrobbled: bool = False,
                     simple: bool = False) -> None:
        p = self.playing
        if p.track is None or _key(p.track) != _key(track):
            p.since = since
        p.track = track
        p.scrobbled = scrobbled
        p.simple = simple

    # ── scrobbling plumbing ──────────────────────────────────────────────────

    def _eligible(self, t: TrackInfo, start: float, now: float) -> bool:
        played = now - start
        if played < self.cfg.scrobble.min_play_seconds:
            return False
        if t.duration:
            return played >= min(t.duration * self.cfg.scrobble.min_play_fraction, 240)
        return True

    def _scrobble(self, t: TrackInfo, timestamp: float, backfill: bool = False) -> None:
        if self.scrobbler and not self.dry_run:
            self.scrobbler.scrobble(t, timestamp=int(timestamp))
            self.history.log(t, timestamp, source="backfill" if backfill else "live")
            self._playcount_cache.pop((t.artist.lower(), (t.album or "").lower()), None)

    def _now_playing(self, t: TrackInfo) -> None:
        if self.scrobbler and not self.dry_run:
            self.scrobbler.update_now_playing(t)

    def _finalize_simple(self, now: float) -> None:
        """Scrobble a simple-mode track that is ending (silence, lock, next track)."""
        p = self.playing
        if self.dry_run or p.track is None or not p.simple or p.scrobbled:
            return
        if self._eligible(p.track, p.since, now):
            self._scrobble(p.track, p.since)
        p.scrobbled = True

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
        st = ScreenState(phase=self.phase, level_db=self.level_db, updated_at=now,
                         motion=self.motion, steady_noise=self.noise_since is not None)
        t = self.playing.track
        if self.phase != Phase.PLAYING or t is None:
            return st

        st.artist, st.title, st.album = t.artist, t.title, t.album or ""
        parts = split_title(t.title, t.artists or [t.artist])
        st.base, st.featured, st.version = parts.base, list(parts.featured), parts.version
        if self.playing.art:
            st.art, st.art_source = self.playing.art.data, self.playing.art.source

        s = self.session
        al = s.album_state if s and s.mode == SessionMode.ALBUM else None
        if al:
            tl = al.tracklist
            st.artist = tl.artist or st.artist
            st.album = tl.album_name or st.album
            recent = self._recently_scrobbled(tl.artist, now)
            st.tracklist = [
                _track_row(tr, tl.position_at(i),
                           i in al.scrobbled or normalize_title(tr.title) in recent)
                for i, tr in enumerate(tl.tracks)
            ]
            st.current_index = al.current_index
            st.year = tl.year
            cur = tl.get_track_at(al.current_index)
            st.track_duration = float(cur.duration) if cur and cur.duration else None
            st.track_elapsed = max(0.0, (now - al.current_track_start) * s.deck_speed())
            st.start_exact = al.start_exact
            st.scrobbled = st.tracklist[al.current_index].scrobbled if st.tracklist else False
            if s.deck_speed_samples >= 3:
                st.deck_speed = s.deck_speed()
            st.session_scrobbles = len(al.scrobbled)
            st.position = tl.position_at(al.current_index)
            st.side = tl.side_at(al.current_index)
            st.owned = tl.owned
        else:
            st.track_duration = float(t.duration) if t.duration else None
            if self.playing.since:
                st.track_elapsed = max(0.0, now - self.playing.since)
            st.scrobbled = self.playing.scrobbled
            st.session_scrobbles = s.comp_scrobbled_count if s else int(self.playing.scrobbled)

        self._enrich(st)
        st.album_display = clean_album(st.album)
        return st

    def _announce_side_end(self, al: Any, scrobbled: int) -> None:
        """Tell the display listeners which side ended and what comes next."""
        tl = al.tracklist
        side = tl.side_at(al.current_index) if al.current_index >= 0 else ""
        next_side, next_title = "", ""
        if side:
            for i in range(al.current_index + 1, len(tl.tracks)):
                s_i = tl.side_at(i)
                if s_i and s_i != side:
                    next_side, next_title = s_i, tl.tracks[i].title
                    break
        info = {"artist": tl.artist, "album": tl.album_name, "side": side,
                "scrobbled": scrobbled, "next_side": next_side, "next_title": next_title,
                "at": self.clock()}
        for fn in self.side_listeners:
            try:
                fn(info)
            except Exception as e:  # a display problem must not touch scrobbling
                logger.debug("side listener failed: %s", e)

    def _recently_scrobbled(self, artist: str, now: float) -> set[str]:
        """Normalised titles by `artist` scrobbled within RECENT_PLAY_WINDOW, from
        the history file (refreshed every RECENT_PLAY_REFRESH seconds)."""
        key = artist.lower()
        if key != self._recent_key or now - self._recent_at >= RECENT_PLAY_REFRESH:
            try:
                titles = self.history.titles_since(artist, now - RECENT_PLAY_WINDOW)
            except Exception as e:  # the screen must not depend on the history file
                logger.debug("History lookup failed: %s", e)
                titles = set()
            self._recent_titles = {normalize_title(t) for t in titles}
            self._recent_key, self._recent_at = key, now
        return self._recent_titles

    def _enrich(self, st: ScreenState) -> None:
        ex = self.extras
        try:
            if ex.position and st.album and not st.position:
                side, pos = ex.position(st.artist, st.album, st.title)
                st.side, st.position = side, pos
                if st.tracklist and pos:
                    for row in st.tracklist:
                        if not row.position:
                            _, rpos = ex.position(st.artist, st.album, row.title)
                            row.position = rpos
            if ex.album_playcount and st.album:
                key = (st.artist.lower(), st.album.lower())
                if key in self._playcount_cache:
                    st.album_playcount = self._playcount_cache[key]
                elif key not in self._playcount_pending:
                    self._playcount_pending.add(key)
                    self._bg.submit(self._fetch_playcount, key, st.artist, st.album)
            if ex.year and st.album and not st.year:
                st.year = ex.year(st.artist, st.album)
        except Exception as e:  # enrichment must never take the screen down
            logger.debug("Extras failed: %s", e)

    def _fetch_playcount(self, key: tuple[str, str], artist: str, album: str) -> None:
        try:
            self._playcount_cache[key] = self.extras.album_playcount(artist, album)  # type: ignore[misc]
        except Exception as e:
            logger.debug("Playcount lookup failed: %s", e)
            self._playcount_cache[key] = None
        finally:
            self._playcount_pending.discard(key)


def _key(t: TrackInfo) -> str:
    return f"{t.artist.lower()} - {t.title.lower()}"

"""Last.fm scrobbling via pylast: an outbox thread, an offline queue, and
honest error handling.

The engine never waits on Last.fm: `scrobble()` and `update_now_playing()`
drop a job on the outbox and return. The worker sends jobs in order. When
the network is down the scrobble goes to a persistent JSON queue that is
retried later; when Last.fm REJECTS a scrobble (bad parameters, auth), it is
logged, reported, and dropped rather than retried forever.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pylast
import requests

from .config import LastFMConfig, ScrobbleConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

DEFAULT_QUEUE_PATH = Path.home() / ".local" / "share" / "spindle" / "scrobble_queue.json"

QUEUE_FLUSH_INTERVAL = 60      # seconds between flush attempts triggered by a success
NOW_PLAYING_COOLDOWN = 30      # seconds between now-playing updates for the same track
CANON_CACHE_MAX = 200

# Last.fm status codes that mean "try again later" (everything else is final).
_RETRYABLE_STATUS = {
    str(pylast.STATUS_OFFLINE),                  # 11 service offline
    str(pylast.STATUS_TEMPORARILY_UNAVAILABLE),  # 16
    str(pylast.STATUS_RATE_LIMIT_EXCEEDED),      # 29
    str(pylast.STATUS_OPERATION_FAILED),         # 8
}


class Rejected(Exception):
    """Last.fm refused the scrobble for good; do not retry."""


def _classify(e: Exception) -> str:
    """'retry' for connectivity / temporary trouble, 'reject' for final refusals."""
    if isinstance(e, pylast.WSError):
        return "retry" if str(getattr(e, "status", "")) in _RETRYABLE_STATUS else "reject"
    if isinstance(e, (pylast.NetworkError, pylast.MalformedResponseError,
                      requests.RequestException, OSError, TimeoutError)):
        return "retry"
    return "retry"       # unknown: better a duplicate attempt than a lost play


def canonicalize_track(track: TrackInfo, network: pylast.LastFMNetwork) -> TrackInfo:
    """Canonicalize artist/title using Last.fm corrections + proper caps + duration."""
    try:
        try:
            artist_obj = network.get_artist(track.artist)
            corrected_artist = artist_obj.get_correction() or track.artist
        except Exception:
            corrected_artist = track.artist

        title = track.title
        duration = track.duration
        t = pylast.Track(corrected_artist, title, network)
        with contextlib.suppress(Exception):
            title = t.get_correction() or title
        with contextlib.suppress(Exception):
            title = t.get_title(properly_capitalized=True) or title
        if not duration:
            with contextlib.suppress(Exception):
                dur_ms = t.get_duration()
                if isinstance(dur_ms, int) and dur_ms > 0:
                    duration = int(dur_ms / 1000)

        if corrected_artist != track.artist or title != track.title or duration != track.duration:
            return TrackInfo(title=title, artist=corrected_artist, album=track.album,
                             duration=duration, mbid=track.mbid, source=track.source,
                             confidence=track.confidence)
        return track
    except Exception:
        return track


class ScrobbleQueue:
    """Persistent JSON queue for offline scrobbles (atomic writes)."""

    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_QUEUE_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._queue: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, list):
                    if data:
                        logger.info("Loaded %d queued scrobbles from %s", len(data), self.path)
                    return data
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load scrobble queue: %s", e)
        return []

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._queue, indent=2))
            tmp.replace(self.path)
        except OSError as e:
            logger.error("Failed to save scrobble queue: %s", e)

    def add(self, artist: str, title: str, album: str, duration: int, timestamp: int) -> None:
        with self._lock:
            self._queue.append({"artist": artist, "title": title, "album": album,
                                "duration": duration, "timestamp": timestamp})
            self._save()
            n = len(self._queue)
        logger.info("Queued offline: %s — %s (queue size: %d)", artist, title, n)

    def pop_all(self) -> list[dict]:
        with self._lock:
            if not self._queue:
                return []
            entries = list(self._queue)
            self._queue.clear()
            self._save()
            return entries

    def put_back(self, entries: list[dict]) -> None:
        with self._lock:
            self._queue = entries + self._queue
            self._save()

    def __len__(self) -> int:
        return len(self._queue)


class Scrobbler:
    """Last.fm auth, scrobbling, now-playing, dedup, offline queue (I/O on an outbox thread)."""

    def __init__(self, lastfm_cfg: LastFMConfig, scrobble_cfg: ScrobbleConfig,
                 queue_path: Path | None = None, threaded: bool = True,
                 on_error: Callable[[str], None] | None = None):
        self.cfg = lastfm_cfg
        self.scrobble_cfg = scrobble_cfg
        self.network: pylast.LastFMNetwork | None = None
        self.threaded = threaded
        self.on_error = on_error
        self._queue = ScrobbleQueue(queue_path)
        self._last_scrobble: str | None = None
        self._last_scrobble_time: float = 0
        self._last_flush_attempt: float = 0
        self._last_np_key: str | None = None
        self._last_np_time: float = 0
        self._canon: dict[tuple[str, str], TrackInfo] = {}
        self._outbox: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self.sent = 0
        self.failed = 0

    # ── lifecycle ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        if not self.cfg.api_key or not self.cfg.api_secret:
            raise ValueError("Last.fm API key and secret are required")
        kwargs: dict = {"api_key": self.cfg.api_key, "api_secret": self.cfg.api_secret,
                        "username": self.cfg.username}
        if self.cfg.session_key:
            kwargs["session_key"] = self.cfg.session_key
        else:
            kwargs["password_hash"] = self.cfg.password_hash
        self.network = pylast.LastFMNetwork(**kwargs)
        logger.info("Connected to Last.fm as %s", self.cfg.username)
        if self.threaded and self._thread is None:
            self._thread = threading.Thread(target=self._run, name="lastfm", daemon=True)
            self._thread.start()
        if len(self._queue):
            self.flush_queue()

    def close(self, timeout: float = 10.0) -> None:
        """Let the outbox drain (shutdown), then stop the thread."""
        if self._thread:
            self._outbox.put(None)
            self._thread.join(timeout=timeout)
            self._thread = None

    # ── public ───────────────────────────────────────────────────────────────

    def update_now_playing(self, track: TrackInfo) -> None:
        if not self.network or not self.scrobble_cfg.now_playing:
            return
        np_key = f"{track.artist.lower()}|||{track.title.lower()}"
        now = time.time()
        if np_key == self._last_np_key and now - self._last_np_time < NOW_PLAYING_COOLDOWN:
            return
        self._last_np_key, self._last_np_time = np_key, now
        self._enqueue(("np", track))

    def scrobble(self, track: TrackInfo, timestamp: int | None = None) -> bool:
        """Queue a scrobble. True when accepted, False when deduped / not connected."""
        if not self.network:
            logger.error("Not connected to Last.fm")
            return False
        track_key = f"{track.artist} - {track.title}"
        now = time.time()
        if (self._last_scrobble == track_key
                and now - self._last_scrobble_time < self.scrobble_cfg.dedup_window):
            logger.debug("Skipping duplicate: %s", track_key)
            return False
        self._last_scrobble, self._last_scrobble_time = track_key, now
        self._enqueue(("scrobble", track, timestamp or int(now)))
        return True

    def album_playcount(self, artist: str, album: str) -> int | None:
        """How many times this user has scrobbled tracks from the album (blocking read)."""
        if not self.network or not artist or not album:
            return None
        try:
            count = self.network.get_album(artist, album).get_userplaycount()
            return int(count) if count is not None else None
        except Exception as e:
            logger.debug("Album playcount lookup failed: %s", e)
            return None

    def flush_queue(self) -> None:
        """Retry queued offline scrobbles (on the outbox thread)."""
        self._enqueue(("flush",))

    def queue_size(self) -> int:
        return len(self._queue)

    def canonicalize(self, track: TrackInfo) -> TrackInfo:
        if not self.network:
            return track
        # Spotify- and Discogs-sourced names are authoritative; Last.fm's correction
        # database is often stale ("Travis Scott" → "Travi$ Scott").
        if track.source.startswith("spotify") or track.source == "discogs":
            return track
        key = (track.artist.lower(), track.title.lower())
        if key in self._canon:
            return self._canon[key]
        out = canonicalize_track(track, self.network)
        if len(self._canon) >= CANON_CACHE_MAX:
            self._canon.pop(next(iter(self._canon)))
        self._canon[key] = out
        return out

    @staticmethod
    def hash_password(password: str) -> str:
        return hashlib.md5(password.encode()).hexdigest()

    # ── outbox ───────────────────────────────────────────────────────────────

    def _enqueue(self, job: tuple) -> None:
        if self.threaded and self._thread is not None:
            self._outbox.put(job)
        else:
            self._handle(job)

    def _run(self) -> None:
        while True:
            job = self._outbox.get()
            if job is None:
                return
            try:
                self._handle(job)
            except Exception as e:  # the outbox must survive anything
                logger.error("Last.fm outbox: %s", e, exc_info=True)

    def _handle(self, job: tuple) -> None:
        kind = job[0]
        if kind == "np":
            self._do_now_playing(job[1])
        elif kind == "scrobble":
            self._do_scrobble(job[1], job[2])
        elif kind == "flush":
            self._do_flush()

    def _do_now_playing(self, track: TrackInfo) -> None:
        assert self.network is not None
        track = self.canonicalize(track)
        try:
            self.network.update_now_playing(artist=track.artist, title=track.title,
                                            album=track.album or "", duration=track.duration or 0)
            logger.info("Now playing: %s - %s", track.artist, track.title)
        except Exception as e:
            logger.debug("Failed to update now playing: %s", e)

    def _do_scrobble(self, track: TrackInfo, ts: int) -> None:
        assert self.network is not None
        track = self.canonicalize(track)
        track_key = f"{track.artist} - {track.title}"
        try:
            self.network.scrobble(artist=track.artist, title=track.title, timestamp=ts,
                                  album=track.album or "", duration=track.duration or 0)
            self.sent += 1
            logger.info("Scrobbled: %s", track_key)
            self._maybe_flush()
        except Exception as e:
            if _classify(e) == "retry":
                logger.warning("Scrobble failed (queued for retry): %s", e)
                self._queue.add(track.artist, track.title, track.album or "",
                                track.duration or 0, ts)
            else:
                self.failed += 1
                logger.error("Scrobble REJECTED by Last.fm, dropped: %s — %s", track_key, e)
                if self.on_error:
                    self.on_error(f"Last.fm rejected {track_key}: {e}")

    def _maybe_flush(self) -> None:
        if len(self._queue) and time.time() - self._last_flush_attempt >= QUEUE_FLUSH_INTERVAL:
            self._do_flush()

    def _do_flush(self) -> int:
        """Send queued scrobbles in order. Stops at the first connectivity error
        (the network is down: no point hammering the rest); drops rejected ones."""
        assert self.network is not None
        self._last_flush_attempt = time.time()
        entries = self._queue.pop_all()
        if not entries:
            return 0
        logger.info("Flushing %d queued scrobbles...", len(entries))
        sent = 0
        for i, entry in enumerate(entries):
            try:
                self.network.scrobble(artist=entry["artist"], title=entry["title"],
                                      timestamp=entry["timestamp"], album=entry.get("album", ""),
                                      duration=entry.get("duration", 0))
                sent += 1
                self.sent += 1
                logger.info("  ✓ %s — %s", entry["artist"], entry["title"])
            except Exception as e:
                if _classify(e) == "retry":
                    logger.warning("  ✗ %s — %s: %s (network; %d left for later)",
                                   entry["artist"], entry["title"], e, len(entries) - i)
                    self._queue.put_back(entries[i:])
                    break
                self.failed += 1
                logger.error("  ✗ %s — %s REJECTED, dropped: %s", entry["artist"],
                             entry["title"], e)
        logger.info("Flushed %d/%d (remaining: %d)", sent, len(entries), len(self._queue))
        return sent

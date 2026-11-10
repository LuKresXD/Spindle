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
SAME_PLAY_SECONDS = 30         # the same track starting again this close is one play sent twice
AUTH_NOTICE_EVERY = 3600       # seconds between "Last.fm refuses the credentials" notices
NOW_PLAYING_COOLDOWN = 30      # seconds between now-playing updates for the same track
CANON_CACHE_MAX = 200

# Last.fm status codes that mean "try again later" (everything else is final).
_RETRYABLE_STATUS = {
    str(pylast.STATUS_OFFLINE),                  # 11 service offline
    str(pylast.STATUS_TEMPORARILY_UNAVAILABLE),  # 16
    str(pylast.STATUS_RATE_LIMIT_EXCEEDED),      # 29
    str(pylast.STATUS_OPERATION_FAILED),         # 8
}


# Last.fm status codes that mean "the credentials are wrong", not "this play is wrong": the
# play is kept for when the configuration is fixed (4 auth failed, 9 invalid session key,
# 10 invalid API key, 13 invalid signature, 14 unauthorised token, 26 suspended key).
_AUTH_STATUS = {"4", "9", "10", "13", "14", "26"}


class Rejected(Exception):
    """Last.fm refused the scrobble for good; do not retry."""


_IGNORED_WHY = {"1": "artist ignored", "2": "track ignored", "3": "timestamp too old",
                "4": "timestamp too new", "5": "daily scrobble limit"}


class Ignored(Exception):
    """Last.fm answered 200 and did not count the play. pylast's scrobble() cannot tell: two
    plays sent under Discogs' spelling of an artist were logged as scrobbled and never
    appeared (2026-11-09)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(f"ignored by Last.fm ({_IGNORED_WHY.get(code, 'code ' + code)})")


def _raw_scrobble(network: pylast.LastFMNetwork, artist: str, title: str, ts: int, album: str,
                  duration: int) -> None:
    """track.scrobble through pylast's signed request, reading the accepted / ignored verdict."""
    params = {"artist[0]": artist, "track[0]": title, "timestamp[0]": str(int(ts))}
    if album:
        params["album[0]"] = album
    if duration:
        params["duration[0]"] = str(int(duration))
    req = pylast._Request(network, "track.scrobble", params)
    req.sign_it()
    doc = req.execute(False)
    try:
        ignored = int(doc.getElementsByTagName("scrobbles")[0].getAttribute("ignored") or 0)
        msgs = doc.getElementsByTagName("ignoredMessage")
        code = msgs[0].getAttribute("code") if msgs else ""
    except (IndexError, ValueError, AttributeError):
        return                                   # an answer without a verdict: taken as sent
    if ignored:
        raise Ignored(code or "?")


def _classify(e: Exception) -> str:
    """'retry' for connectivity / temporary trouble, 'auth' when Last.fm refuses the
    credentials (the play is kept), 'reject' for final refusals of the play itself."""
    if isinstance(e, pylast.WSError):
        status = str(getattr(e, "status", ""))
        if status in _AUTH_STATUS:
            return "auth"
        return "retry" if status in _RETRYABLE_STATUS else "reject"
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

    def snapshot(self) -> list[dict]:
        """The queued entries, oldest first. They stay on disk until each is removed: a
        power cut in the middle of a flush must not cost the ones not sent yet."""
        with self._lock:
            return list(self._queue)

    def remove(self, entry: dict) -> None:
        with self._lock:
            for i, e in enumerate(self._queue):
                if e is entry or e == entry:
                    del self._queue[i]
                    self._save()
                    return

    def __len__(self) -> int:
        return len(self._queue)


class Scrobbler:
    """Last.fm auth, scrobbling, now-playing, dedup, offline queue (I/O on an outbox thread)."""

    def __init__(self, lastfm_cfg: LastFMConfig, scrobble_cfg: ScrobbleConfig,
                 queue_path: Path | None = None, threaded: bool = True,
                 on_error: Callable[[str, str], None] | None = None,
                 on_recovered: Callable[[int], None] | None = None):
        self.cfg = lastfm_cfg
        self.scrobble_cfg = scrobble_cfg
        self.network: pylast.LastFMNetwork | None = None
        self.threaded = threaded
        self.on_error = on_error              # (title, detail): something dropped for good
        self.on_recovered = on_recovered      # (count): the offline queue got through
        self._queue = ScrobbleQueue(queue_path)
        self._last_scrobble: str | None = None
        self._last_scrobble_ts: int = 0
        self._last_auth_notice: float = 0
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
        ts = int(timestamp or time.time())
        # The same track starting again INSIDE its previous play is that play, sent twice (a
        # session that broke on a pause and resumed); Last.fm ignores those too. A start a full
        # track later is another play: the needle went back. (The old rule compared send times
        # within 300 s and dropped an immediate replay of anything under five minutes.)
        same_play = max(SAME_PLAY_SECONDS, int(track.duration or 0))
        if self._last_scrobble == track_key and 0 <= ts - self._last_scrobble_ts < same_play:
            logger.info("Not sending %s again: the same play (started %ds after the last one)",
                        track_key, ts - self._last_scrobble_ts)
            return False
        self._last_scrobble, self._last_scrobble_ts = track_key, ts
        self._enqueue(("scrobble", track, ts))
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
        if track.source.startswith(("spotify", "deezer")) or track.source == "discogs":
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
            self._send(track.artist, track.title, ts, track.album or "", track.duration or 0)
            self.sent += 1
            logger.info("Scrobbled: %s", track_key)
            self._maybe_flush()
        except Ignored as e:
            self.failed += 1
            logger.warning("Scrobble NOT counted, %s: %s", e, track_key)
            if self.on_error:
                self.on_error("Last.fm did not count a scrobble", f"{track_key} · {e}")
        except Exception as e:
            kind = _classify(e)
            if kind in ("retry", "auth"):
                logger.warning("Scrobble failed (queued for retry): %s", e)
                self._queue.add(track.artist, track.title, track.album or "",
                                track.duration or 0, ts)
                if kind == "auth":
                    self._auth_notice(e)
            else:
                self.failed += 1
                logger.error("Scrobble REJECTED by Last.fm, dropped: %s — %s", track_key, e)
                if self.on_error:
                    self.on_error("Last.fm rejected a scrobble", f"{track_key} · {e}")

    def _send(self, artist: str, title: str, ts: int, album: str, duration: int) -> None:
        """One scrobble to Last.fm. Raises Ignored when it answered but did not count it."""
        assert self.network is not None
        if isinstance(self.network, pylast.LastFMNetwork):
            _raw_scrobble(self.network, artist, title, int(ts), album, int(duration or 0))
        else:                                    # a stand-in network (tests)
            self.network.scrobble(artist=artist, title=title, timestamp=ts, album=album,
                                  duration=duration)

    def _auth_notice(self, e: Exception) -> None:
        """Tell the log channel, at most once an hour, that Last.fm refuses the credentials."""
        now = time.time()
        if self.on_error and now - self._last_auth_notice >= AUTH_NOTICE_EVERY:
            self._last_auth_notice = now
            self.on_error("Last.fm refuses the credentials",
                          f"plays are being queued until this is fixed · {e}")

    def _maybe_flush(self) -> None:
        if len(self._queue) and time.time() - self._last_flush_attempt >= QUEUE_FLUSH_INTERVAL:
            self._do_flush()

    def _do_flush(self) -> int:
        """Send queued scrobbles in order. Stops at the first connectivity error
        (the network is down: no point hammering the rest); drops rejected ones."""
        assert self.network is not None
        self._last_flush_attempt = time.time()
        entries = self._queue.snapshot()
        if not entries:
            return 0
        logger.info("Flushing %d queued scrobbles...", len(entries))
        sent = 0
        for i, entry in enumerate(entries):
            if not all(k in entry for k in ("artist", "title", "timestamp")):
                logger.error("  ✗ malformed queue entry dropped: %r", entry)
                self._queue.remove(entry)
                continue
            try:
                self._send(entry["artist"], entry["title"], entry["timestamp"],
                           entry.get("album", ""), entry.get("duration", 0))
                self._queue.remove(entry)              # off the disk only once Last.fm has it
                sent += 1
                self.sent += 1
                logger.info("  ✓ %s — %s", entry["artist"], entry["title"])
            except Exception as e:
                kind = _classify(e)
                if kind in ("retry", "auth"):
                    logger.warning("  ✗ %s — %s: %s (%d left for later)",
                                   entry["artist"], entry["title"], e, len(entries) - i)
                    if kind == "auth":
                        self._auth_notice(e)
                    break
                self._queue.remove(entry)
                self.failed += 1
                logger.error("  ✗ %s — %s REJECTED, dropped: %s", entry["artist"],
                             entry["title"], e)
        logger.info("Flushed %d/%d (remaining: %d)", sent, len(entries), len(self._queue))
        if sent and self.on_recovered:
            self.on_recovered(sent)
        return sent

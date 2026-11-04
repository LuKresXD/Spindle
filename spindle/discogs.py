"""The user's Discogs collection: the records that are actually on the shelf.

Two jobs:

  1. A prior for identification. When a fingerprint comes back as a track
     that exists on several albums (the album, a single, three compilations),
     the pressing the user owns is the one on the platter. When Spotify has
     never heard of the record at all, the Discogs tracklist is still enough
     to lock an album and scrobble a side.
  2. Vinyl positions ("A3 · SIDE A") and side boundaries for the screen and
     the timing model.

All network work happens on a background thread: the collection is fetched
at start and refreshed daily, and every release's tracklist is pulled once
(a pressing never changes) and cached under ~/.cache/spindle/discogs/.
Lookups from the engine are memory-only and never block on the API.

Discogs API: personal token, 60 requests/min authenticated.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .fingerprint import TrackInfo
from .tracklist import AlbumTracklist, norm_title, side_of, titles_match

logger = logging.getLogger(__name__)

API = "https://api.discogs.com"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "spindle" / "discogs"
COLLECTION_TTL = 24 * 3600
_UA = "spindle/0.2 +https://github.com/LuKresXD/Spindle"
_MIN_GAP = 1.1          # seconds between API calls (stay well under 60/min)
_RETRY_IN = 600         # seconds before retrying a failed collection refresh


def clean_artist(name: str) -> str:
    """"Travis Scott (2)" -> "Travis Scott"; "Various" -> "Various Artists"."""
    s = re.sub(r"\s*\(\d+\)$", "", name or "").strip()
    return "Various Artists" if s.lower() == "various" else s


def _norm(s: str) -> str:
    s = s.lower().replace("'", "").replace("’", "")
    s = re.sub(r"\s*\(\d+\)$", "", s)                       # Discogs "Artist (2)" disambiguation
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(deluxe|edition|remaster(?:ed)?|expanded|anniversary|version|lp|ep)\b", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    s = re.sub(r"[^a-z0-9Ѐ-ӿ]+", " ", s)
    return " ".join(s.split())


def _same(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def same_album(a: str, b: str) -> bool:
    """Album titles: exact after normalisation ("Whole Lotta Red " == "Whole Lotta Red")."""
    a, b = _norm(a), _norm(b)
    return bool(a) and a == b


def parse_duration(s: str) -> int | None:
    """"3:45" -> 225, "1:02:03" -> 3723, "" -> None."""
    if not s:
        return None
    parts = s.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total or None


@dataclass
class Release:
    id: int
    artist: str
    title: str
    year: int = 0
    formats: list = field(default_factory=list)      # ["Vinyl"]
    tracks: list = field(default_factory=list)       # [{"position","title","duration","artist"}]
    loaded: bool = False

    @property
    def positions(self) -> list[tuple[str, str]]:
        return [(t.get("position", ""), t.get("title", "")) for t in self.tracks]

    @property
    def vinyl(self) -> bool:
        return not self.formats or any("vinyl" in f.lower() for f in self.formats)

    def to_dict(self) -> dict:
        return {"id": self.id, "artist": self.artist, "title": self.title, "year": self.year,
                "formats": self.formats, "tracks": self.tracks, "loaded": self.loaded}

    @staticmethod
    def from_dict(r: dict) -> Release:
        tracks = r.get("tracks")
        if tracks is None:                              # v1 cache: [(position, title)]
            tracks = [{"position": p, "title": t, "duration": None, "artist": ""}
                      for p, t in r.get("positions", [])]
        return Release(int(r["id"]), r["artist"], r["title"], int(r.get("year") or 0),
                       list(r.get("formats") or []), tracks, bool(r.get("loaded")))


class DiscogsCollection:
    def __init__(self, token: str, username: str, cache_dir: Path | None = None,
                 session: requests.Session | None = None):
        self.token = token
        self.username = username
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA,
                                   "Authorization": f"Discogs token={token}"})
        self._releases: list[Release] = []
        self._fetched_at = 0.0
        self._last_call = 0.0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_error: str | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Discogs cache dir unavailable (%s)", e)
        self._load_cache()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the background refresh + tracklist prefetch loop."""
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="discogs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_now(self) -> None:
        """Ask the background thread to refresh immediately (bot /collection refresh)."""
        with self._lock:
            self._fetched_at = 0.0
        self._wake.set()

    @property
    def ready(self) -> bool:
        return bool(self._releases)

    def stats(self) -> dict:
        with self._lock:
            return {
                "releases": len(self._releases),
                "loaded": sum(1 for r in self._releases if r.loaded),
                "fetched_at": self._fetched_at,
                "error": self.last_error,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_if_stale()
                self._prefetch_tracklists()
            except Exception as e:  # the thread must survive anything
                logger.warning("Discogs background: %s", e)
            self._wake.wait(timeout=300)
            self._wake.clear()

    # ── sync (blocking) helpers for callers that want them ───────────────────

    def sync(self) -> None:
        """Blocking refresh + prefetch (tests, one-off scripts)."""
        self._refresh_if_stale()
        self._prefetch_tracklists()

    # ── queries (memory only, never block on the network) ────────────────────

    def releases_for_artist(self, artist: str) -> list[Release]:
        with self._lock:
            return [r for r in self._releases if _same(r.artist, artist)]

    def find_release(self, artist: str, album: str) -> Release | None:
        """The owned pressing of an album, exact title first, vinyl first."""
        if not album:
            return None
        cands = self.releases_for_artist(artist)
        exact = [r for r in cands if same_album(r.title, album)]
        loose = exact or [r for r in cands if _same(r.title, album)]
        if not loose:
            return None
        loose.sort(key=lambda r: (not r.vinyl, not r.loaded, r.id))
        return loose[0]

    def releases_with_track(self, artist: str, title: str) -> list[Release]:
        """Owned releases (tracklist known) that carry this track; vinyl first."""
        out = []
        for r in self.releases_for_artist(artist):
            if not r.loaded:
                continue
            if any(titles_match(t.get("title", ""), title) for t in r.tracks):
                out.append(r)
        out.sort(key=lambda r: (not r.vinyl, r.id))
        return out

    def owned_album_titles(self, artist: str) -> list[str]:
        return [r.title for r in self.releases_for_artist(artist)]

    def tracklist(self, rel: Release) -> AlbumTracklist | None:
        """The pressing as an AlbumTracklist (None until its tracklist is loaded)."""
        if not rel.loaded or not rel.tracks:
            return None
        artist = clean_artist(rel.artist)
        album = rel.title.strip()
        tracks, positions = [], []
        for t in rel.tracks:
            tracks.append(TrackInfo(
                title=t.get("title", ""), artist=clean_artist(t.get("artist") or "") or artist,
                album=album, duration=t.get("duration"), source="discogs", confidence=1.0,
            ))
            positions.append(t.get("position", "") or "")
        return AlbumTracklist(
            album_id=f"dg:{rel.id}", album_name=album, artist=artist, tracks=tracks,
            release_date=str(rel.year) if rel.year else "", positions=positions,
            source="discogs", discogs_release_id=rel.id, owned=True,
        )

    def position(self, artist: str, album: str, title: str) -> tuple[str, str]:
        """(side, position) for a track, e.g. ("A", "A3"); ("", "") when unknown."""
        try:
            rel = self.find_release(artist, album)
            if rel is None or not rel.loaded:
                return ("", "")
            want = norm_title(title)
            pos = ""
            for t in rel.tracks:
                if norm_title(t.get("title", "")) == want:
                    pos = t.get("position", "")
                    break
            if not pos:
                for t in rel.tracks:
                    if titles_match(t.get("title", ""), title):
                        pos = t.get("position", "")
                        break
            return (side_of(pos), pos) if pos else ("", "")
        except Exception as e:  # never let Discogs trouble reach the screen
            logger.debug("Discogs position failed: %s", e)
            return ("", "")

    # ── network (background thread) ──────────────────────────────────────────

    def _refresh_if_stale(self) -> None:
        with self._lock:
            fresh = self._releases and time.time() - self._fetched_at < COLLECTION_TTL
        if fresh:
            return
        try:
            releases = self._fetch_collection()
        except requests.RequestException as e:
            self.last_error = str(e)
            logger.warning("Discogs collection refresh failed: %s", e)
            with self._lock:
                self._fetched_at = time.time() - COLLECTION_TTL + _RETRY_IN
            return
        with self._lock:
            self._releases = releases
            self._fetched_at = time.time()
            self.last_error = None
            self._save_cache()
        logger.info("Discogs: %d releases in collection", len(releases))

    def _fetch_collection(self) -> list[Release]:
        with self._lock:
            by_id = {r.id: r for r in self._releases}
        out: list[Release] = []
        page = 1
        while True:
            data = self._get(f"/users/{self.username}/collection/folders/0/releases",
                             params={"per_page": 100, "page": page})
            for item in data.get("releases", []):
                info = item.get("basic_information", {})
                rid = int(info.get("id", 0))
                artists = ", ".join(a.get("name", "") for a in info.get("artists", []))
                rel = by_id.get(rid) or Release(rid, artists, info.get("title", ""))
                rel.artist, rel.title = artists, info.get("title", rel.title)
                rel.year = int(info.get("year") or rel.year or 0)
                rel.formats = [f.get("name", "") for f in info.get("formats", [])] or rel.formats
                out.append(rel)
            pages = data.get("pagination", {}).get("pages", 1)
            if page >= pages:
                break
            page += 1
        return out

    def _prefetch_tracklists(self) -> None:
        """Load every unloaded release, one API call each, rate limited."""
        while not self._stop.is_set():
            with self._lock:
                pending = [r for r in self._releases if not r.loaded]
            if not pending:
                return
            rel = pending[0]
            try:
                self._load_release(rel)
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning("Discogs: tracklist for %s failed: %s", rel.title, e)
                return                                 # network trouble: try again next loop
            if len(pending) == 1:
                logger.info("Discogs: all %d tracklists loaded", len(self._releases))

    def _load_release(self, rel: Release) -> None:
        data = self._get(f"/releases/{rel.id}")
        tracks = []
        for t in data.get("tracklist", []):
            kind = t.get("type_", "track")
            pos = t.get("position", "") or ""
            if kind == "index" and t.get("sub_tracks"):
                for st in t["sub_tracks"]:
                    if st.get("type_", "track") == "track":
                        tracks.append(_track_dict(st, pos))
                continue
            if kind != "track" or not pos:
                continue
            tracks.append(_track_dict(t, pos))
        with self._lock:
            rel.tracks = tracks
            rel.loaded = True
            if not rel.year:
                rel.year = int(data.get("year") or 0)
            self._save_cache()
        logger.debug("Discogs: loaded %s — %s (%d tracks)", rel.artist, rel.title, len(tracks))

    def _get(self, path: str, params: dict | None = None) -> dict:
        wait = _MIN_GAP - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = self._http.get(API + path, params=params, timeout=15)
        self._last_call = time.time()
        if resp.status_code == 429:
            time.sleep(5)
            resp = self._http.get(API + path, params=params, timeout=15)
            self._last_call = time.time()
        resp.raise_for_status()
        return resp.json()

    # ── disk cache ───────────────────────────────────────────────────────────

    def _cache_file(self) -> Path:
        return self.cache_dir / f"collection-{self.username}.json"

    def _load_cache(self) -> None:
        try:
            d = json.loads(self._cache_file().read_text())
            self._fetched_at = float(d.get("fetched_at", 0))
            self._releases = [Release.from_dict(r) for r in d.get("releases", [])]
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        try:
            tmp = self._cache_file().with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "fetched_at": self._fetched_at,
                "releases": [r.to_dict() for r in self._releases],
            }))
            tmp.replace(self._cache_file())
        except OSError as e:
            logger.debug("Discogs cache write failed: %s", e)


def _track_dict(t: dict, position: str) -> dict:
    artists = t.get("artists") or []
    return {
        "position": t.get("position", "") or position,
        "title": t.get("title", ""),
        "duration": parse_duration(t.get("duration", "")),
        "artist": ", ".join(a.get("name", "") for a in artists) if artists else "",
    }

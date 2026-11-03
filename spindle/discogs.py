"""Vinyl positions from the user's Discogs collection.

The Spotify tracklist knows track numbers; a record knows sides. Luka's
Discogs collection holds the actual pressings he owns, each with a
tracklist whose positions read "A1", "B3" and so on. We match the playing
album (artist + title) to a release in the collection and map track titles
to positions, so the screen can say "A3 · SIDE A" instead of "TRACK 03".

Discogs API: personal token, 60 requests/min authenticated. The collection
listing is cached for a day; each release's tracklist is fetched once and
kept forever (a pressing does not change). Cache: ~/.cache/spindle/discogs/.
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

logger = logging.getLogger(__name__)

API = "https://api.discogs.com"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "spindle" / "discogs"
COLLECTION_TTL = 24 * 3600
_UA = "spindle/0.2 +https://github.com/LuKresXD/Spindle"
_MIN_GAP = 1.1          # seconds between API calls (stay well under 60/min)


def _norm(s: str) -> str:
    s = s.lower().replace("'", "").replace("\u2019", "")   # let's == lets
    s = re.sub(r"\s*\(\d+\)$", "", s)                       # Discogs "Artist (2)" disambiguation
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(deluxe|edition|remaster(?:ed)?|expanded|anniversary|version|lp|ep)\b", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _same(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


@dataclass
class Release:
    id: int
    artist: str
    title: str
    positions: list[tuple[str, str]] = field(default_factory=list)   # (position, title)
    loaded: bool = False


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
        self._lock = threading.Lock()
        self._position_cache: dict[tuple[str, str, str], tuple[str, str]] = {}
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Discogs cache dir unavailable (%s)", e)
        self._load_cache()

    # ── public ───────────────────────────────────────────────────────────────

    def position(self, artist: str, album: str, title: str) -> tuple[str, str]:
        """(side, position) for a track, e.g. ("A", "A3"); ("", "") when unknown."""
        key = (artist.lower(), album.lower(), title.lower())
        if key in self._position_cache:
            return self._position_cache[key]
        result = ("", "")
        try:
            rel = self.find_release(artist, album)
            if rel is not None:
                self._ensure_tracklist(rel)
                pos = self._match_position(rel, title)
                if pos:
                    side = re.match(r"[A-Za-z]+", pos)
                    result = (side.group(0).upper() if side else "", pos)
        except Exception as e:  # never let Discogs trouble reach the screen
            logger.debug("Discogs position failed: %s", e)
        self._position_cache[key] = result
        return result

    def find_release(self, artist: str, album: str) -> Release | None:
        self._refresh_collection()
        want = _norm(album)
        exact = [r for r in self._releases if _same(r.artist, artist) and _norm(r.title) == want]
        if exact:
            return exact[0]
        loose = [r for r in self._releases if _same(r.artist, artist) and _same(r.title, album)]
        return loose[0] if loose else None

    # ── collection ───────────────────────────────────────────────────────────

    def _refresh_collection(self) -> None:
        with self._lock:
            if self._releases and time.time() - self._fetched_at < COLLECTION_TTL:
                return
            try:
                self._releases = self._fetch_collection(self._releases)
                self._fetched_at = time.time()
                self._save_cache()
                logger.info("Discogs: %d releases in collection", len(self._releases))
            except requests.RequestException as e:
                logger.warning("Discogs collection refresh failed: %s", e)
                self._fetched_at = time.time() - COLLECTION_TTL + 600   # retry in 10 min

    def _fetch_collection(self, known: list[Release]) -> list[Release]:
        by_id = {r.id: r for r in known}
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
                out.append(rel)
            pages = data.get("pagination", {}).get("pages", 1)
            if page >= pages:
                break
            page += 1
        return out

    def _ensure_tracklist(self, rel: Release) -> None:
        if rel.loaded:
            return
        with self._lock:
            if rel.loaded:
                return
            data = self._get(f"/releases/{rel.id}")
            rel.positions = [
                (t.get("position", ""), t.get("title", ""))
                for t in data.get("tracklist", [])
                if t.get("type_", "track") == "track" and t.get("position")
            ]
            rel.loaded = True
            self._save_cache()

    def _match_position(self, rel: Release, title: str) -> str:
        want = _norm(title)
        if not want:
            return ""
        for pos, t in rel.positions:
            if _norm(t) == want:
                return pos
        for pos, t in rel.positions:
            if _same(t, title):
                return pos
        return ""

    # ── http ─────────────────────────────────────────────────────────────────

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
            self._releases = [
                Release(int(r["id"]), r["artist"], r["title"],
                        [tuple(p) for p in r.get("positions", [])], bool(r.get("loaded")))
                for r in d.get("releases", [])
            ]
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        try:
            self._cache_file().write_text(json.dumps({
                "fetched_at": self._fetched_at,
                "releases": [
                    {"id": r.id, "artist": r.artist, "title": r.title,
                     "positions": r.positions, "loaded": r.loaded}
                    for r in self._releases
                ],
            }))
        except OSError as e:
            logger.debug("Discogs cache write failed: %s", e)

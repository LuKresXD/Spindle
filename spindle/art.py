"""Cover art at a size that holds up on a 1080p panel.

Spotify's largest album image is 640 px; the screen shows the cover at
960 px, so that always arrived soft. Apple's catalog serves the same art at
1200 px (and larger) through the free iTunes Search API, no key needed.
Order: Apple by artist + album (exact-ish match required) -> Deezer
(explicit albums are missing from Apple's public search; Deezer has them,
1000 px native and its image service resizes on request) -> Spotify URL
(whatever size it offers) -> nothing.

Results are cached on disk (`~/.cache/spindle/art/`) keyed by artist +
album, so a record that is played every week costs one download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

ITUNES_SEARCH = "https://itunes.apple.com/search"
DEEZER_SEARCH = "https://api.deezer.com/search/album"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "spindle" / "art"
ITUNES_SIZE = 1200
DEEZER_SIZE = 1200
NEGATIVE_TTL = 600        # seconds before a failed lookup is tried again
_UA = "spindle/0.2 (+https://github.com/LuKresXD/Spindle)"


@dataclass
class Art:
    data: bytes
    source: str          # "itunes" | "deezer" | "spotify"
    url: str = ""


def _norm(s: str) -> str:
    s = s.lower().replace("'", "").replace("\u2019", "")   # let's == lets
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)                    # (deluxe), [remastered]
    s = re.sub(r"\b(deluxe|edition|remaster(?:ed)?|expanded|bonus|version)\b", " ", s)
    # Cyrillic stays: stripped, every all-Cyrillic artist and album came out empty, so the
    # matchers never matched and all of them shared one cache key (one cover for every Кино record)
    s = re.sub(r"[^a-z0-9Ѐ-ӿ]+", " ", s)
    return " ".join(s.split())


def _similar(a: str, b: str) -> bool:
    """Same artist / same album after normalisation. Exact only: a substring
    test once picked "Madvillainy Instrumentals" for "Madvillainy"."""
    a, b = _norm(a), _norm(b)
    return bool(a) and a == b


def _artist_ok(candidate: str, wanted: str) -> bool:
    """Artist names may carry collaborators ("Madvillain", "Kanye West & Ty Dolla $ign")."""
    c, w = _norm(candidate), _norm(wanted)
    return bool(c) and bool(w) and (c == w or w in c or c in w)


class ArtProvider:
    def __init__(self, cache_dir: Path | None = None, itunes: bool = True,
                 deezer: bool = True, session: requests.Session | None = None):
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self.itunes = itunes
        self.deezer = deezer
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA})
        self._mem: dict[str, tuple[Art | None, float]] = {}   # key -> (art, retry_after)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Art cache dir unavailable (%s); memory cache only", e)

    # ── public ───────────────────────────────────────────────────────────────

    def get(self, artist: str, album: str, spotify_url: str = "") -> Art | None:
        """Cover for an album: memory, then disk, then the network. A miss is
        remembered for NEGATIVE_TTL only, so a flaky moment does not leave a
        record without its cover until the next restart."""
        key = self._key(artist, album)
        hit = self._mem.get(key)
        if hit is not None:
            art, retry_after = hit
            if art is not None or time.time() < retry_after:
                return art
        art = self._from_disk(key)
        if art is None:
            art = self._fetch(artist, album, spotify_url)
            if art is not None:
                self._to_disk(key, art)
        self._mem[key] = (art, time.time() + NEGATIVE_TTL)
        return art

    # ── fetching ─────────────────────────────────────────────────────────────

    def _fetch(self, artist: str, album: str, spotify_url: str) -> Art | None:
        if self.itunes and artist and album:
            url = self.itunes_url(artist, album)
            if url:
                data = self._download(url)
                if data:
                    logger.info("Art: Apple %dpx for %s - %s", ITUNES_SIZE, artist, album)
                    return Art(data, "itunes", url)
        if self.deezer and artist and album:
            for url in self.deezer_urls(artist, album):
                data = self._download(url)
                if data:
                    logger.info("Art: Deezer for %s - %s", artist, album)
                    return Art(data, "deezer", url)
        if spotify_url:
            data = self._download(spotify_url)
            if data:
                logger.info("Art: Spotify fallback for %s - %s", artist, album)
                return Art(data, "spotify", spotify_url)
        return None

    def itunes_url(self, artist: str, album: str) -> str | None:
        """Find the album on Apple's catalog and return a 1200 px artwork URL."""
        try:
            params: dict[str, str] = {"term": f"{artist} {album}", "entity": "album", "limit": "10"}
            resp = self._http.get(ITUNES_SEARCH, params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("results", [])
        except (requests.RequestException, ValueError) as e:
            logger.debug("Art: iTunes search failed: %s", e)
            return None
        for r in results:
            if (_artist_ok(r.get("artistName", ""), artist)
                    and _similar(r.get("collectionName", ""), album)):
                url = r.get("artworkUrl100", "")
                if url:
                    return re.sub(r"\d+x\d+bb", f"{ITUNES_SIZE}x{ITUNES_SIZE}bb", url)
        logger.debug("Art: no Apple match for %s - %s (%d results)", artist, album, len(results))
        return None

    def deezer_urls(self, artist: str, album: str) -> list[str]:
        """Candidate Deezer cover URLs, largest first (resized, then native xl)."""
        try:
            params: dict[str, str] = {"q": f'artist:"{artist}" album:"{album}"', "limit": "10"}
            resp = self._http.get(DEEZER_SEARCH, params=params, timeout=10)
            resp.raise_for_status()
            results = resp.json().get("data", [])
        except (requests.RequestException, ValueError) as e:
            logger.debug("Art: Deezer search failed: %s", e)
            return []
        for r in results:
            if (_artist_ok(r.get("artist", {}).get("name", ""), artist)
                    and _similar(r.get("title", ""), album)):
                xl = r.get("cover_xl") or r.get("cover_big") or ""
                if not xl:
                    continue
                big = re.sub(r"/\d+x\d+-", f"/{DEEZER_SIZE}x{DEEZER_SIZE}-", xl)
                return [big, xl] if big != xl else [xl]
        logger.debug("Art: no Deezer match for %s - %s (%d results)", artist, album, len(results))
        return []

    def _download(self, url: str) -> bytes | None:
        try:
            resp = self._http.get(url, timeout=20)
            resp.raise_for_status()
            if not resp.content or len(resp.content) < 1024:
                return None
            return resp.content
        except requests.RequestException as e:
            logger.debug("Art: download failed %s: %s", url, e)
            return None

    # ── disk cache ───────────────────────────────────────────────────────────

    @staticmethod
    def _key(artist: str, album: str) -> str:
        return hashlib.sha1(f"{_norm(artist)}|{_norm(album)}".encode()).hexdigest()[:20]

    def _from_disk(self, key: str) -> Art | None:
        meta = self.cache_dir / f"{key}.json"
        blob = self.cache_dir / f"{key}.img"
        try:
            if meta.exists() and blob.exists():
                m = json.loads(meta.read_text())
                return Art(blob.read_bytes(), m.get("source", ""), m.get("url", ""))
        except (OSError, ValueError):
            pass
        return None

    def _to_disk(self, key: str, art: Art) -> None:
        try:
            (self.cache_dir / f"{key}.img").write_bytes(art.data)
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps({"source": art.source, "url": art.url}))
        except OSError as e:
            logger.debug("Art: cache write failed: %s", e)

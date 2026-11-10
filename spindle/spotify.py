"""Spotify Web API: track candidates, canonical names, album tracklists.

Client-credentials flow (no user login). `search_tracks()` returns every
candidate for an artist + title so the resolver can pick the right album
with more context than Spotify's popularity order (the user's record
collection, the album that is already locked, album vs single vs
compilation). `lookup()` is the context-free convenience wrapper.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

import requests

from .config import SpotifyConfig
from .fingerprint import TrackInfo
from .names import same_artist
from .tracklist import AlbumTracklist, titles_match

__all__ = ["AlbumTracklist", "SpotifyCandidate", "SpotifyClient", "SpotifyTrack", "EDITION_RE"]

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"
ALBUM_URL = "https://api.spotify.com/v1/albums"

# Deluxe / anniversary / remaster editions: fine to fall back to, never preferred.
# The marker only counts as a qualifier: in brackets, after a dash or colon, or closing the
# name as "... Edition" / "... Remaster(ed)". A plain title may carry the word (Sade's
# "Love Deluxe" is the studio album, 2026-11-08).
_EDITION_MARK = (r"(?:super\s+deluxe|deluxe|anniversary|remaster(?:ed)?|expanded|bonus|"
                 r"special\s+edition|\d{4}\s+(?:remaster|edition|mix))")
EDITION_RE = re.compile(
    rf"[\(\[][^\)\]]*\b{_EDITION_MARK}\b[^\)\]]*[\)\]]"
    rf"|[-–—:]\s*[^-–—:]*\b{_EDITION_MARK}\b"
    r"|\b(?:\w+\s+edition|remaster(?:ed)?|\d{4}\s+(?:remaster|edition|mix))\s*$",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\s*[\(\[](feat\.?|ft\.?|featuring)\b[^\)\]]*[\)\]]", re.IGNORECASE)

_LOOKUP_CACHE_MAX = 500


@dataclass
class SpotifyCandidate:
    """One search hit, flattened."""
    name: str
    artists: list = field(default_factory=list)   # list[str], primary first
    album_id: str = ""
    album_name: str = ""
    album_type: str = ""                          # "album" | "single" | "compilation"
    total_tracks: int = 0
    release_date: str = ""
    duration: int | None = None                   # seconds
    art_url: str = ""
    rank: int = 0                                 # Spotify's own order (0 = top)
    provider: str = "spotify"                     # which catalogue answered ("spotify" | "deezer")

    @property
    def artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def to_track(self) -> TrackInfo:
        return TrackInfo(title=self.name, artist=self.artist, album=self.album_name,
                         artists=list(self.artists),
                         duration=self.duration, source=f"{self.provider}_lookup", confidence=1.0)


@dataclass
class SpotifyTrack:
    """Context-free best match (kept for callers that only need a canonical name)."""
    track: TrackInfo
    album_id: str = ""
    album_name: str = ""
    track_number: int = 0
    disc_number: int = 1
    total_tracks: int = 0
    album_art_url: str = ""


class SpotifyClient:
    def __init__(self, cfg: SpotifyConfig, session: requests.Session | None = None):
        self.cfg = cfg
        self._http = session or requests.Session()
        self._token: str | None = None
        self._token_expiry: float = 0
        self._album_cache: dict[str, AlbumTracklist] = {}
        self._search_cache: dict[str, list[SpotifyCandidate]] = {}
        self._backoff_until: float = 0

    @property
    def available(self) -> bool:
        """False while a rate limit's Retry-After runs (it can be a day): the catalogue then
        asks its second source instead of getting nothing."""
        return time.time() >= self._backoff_until

    # ── auth ─────────────────────────────────────────────────────────────────

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 30:
            return self._token
        resp = self._http.post(
            TOKEN_URL, data={"grant_type": "client_credentials"},
            auth=(self.cfg.client_id, self.cfg.client_secret), timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        logger.debug("Spotify token refreshed (expires in %ds)", data["expires_in"])
        return self._token

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._get_token()}"}

    def _get(self, url: str, params: dict | None = None) -> dict | None:
        """GET with the 429 backoff. None when backing off or on failure."""
        if time.time() < self._backoff_until:
            logger.debug("Spotify: backing off (rate limited)")
            return None
        try:
            resp = self._http.get(url, params=params, headers=self._headers(), timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                retry_after = int(e.response.headers.get("Retry-After", 30))
                self._backoff_until = time.time() + retry_after
                logger.warning("Spotify rate limited: backing off %ds", retry_after)
            else:
                logger.warning("Spotify request failed: %s", e)
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.warning("Spotify request failed: %s", e)
        return None

    # ── search ───────────────────────────────────────────────────────────────

    def search_tracks(self, artist: str, title: str,
                      also: tuple[str, ...] = ()) -> list[SpotifyCandidate]:
        """All plausible candidates for a fingerprinted artist + title, Spotify order.
        (`also`, the artist's other names, is the resolver's to apply: nothing is filtered
        by artist here.)

        Field-filtered search first (`track: artist:`); when that finds nothing
        (feat. tracks whose primary artist differs, odd spellings) a plain
        query is tried and filtered by title.
        """
        key = f"{artist.lower()}|||{title.lower()}"
        if key in self._search_cache:
            return self._search_cache[key]

        clean_title = _FEAT_RE.sub("", title).strip() or title
        items = self._search_items(f"track:{clean_title} artist:{artist}")
        if items is None:
            return []                                   # transient: do not cache
        if not items:
            plain = self._search_items(f"{clean_title} {artist}")
            if plain is None:
                return []
            items = [t for t in plain if titles_match(t.get("name", ""), title)]
        cands = [_candidate(t, i) for i, t in enumerate(items)]
        if len(self._search_cache) >= _LOOKUP_CACHE_MAX:
            self._search_cache.pop(next(iter(self._search_cache)))
        self._search_cache[key] = cands
        if not cands:
            logger.debug("Spotify: no results for '%s - %s'", artist, title)
        return cands

    def _search_items(self, query: str) -> list[dict] | None:
        data = self._get(SEARCH_URL, {"q": query, "type": "track", "limit": "10"})
        if data is None:
            return None
        return data.get("tracks", {}).get("items", []) or []

    def lookup(self, artist: str, title: str) -> SpotifyTrack | None:
        """Best context-free match: right artist, a real album over singles/compilations,
        the standard edition over deluxe ones, then Spotify's own order."""
        cands = self.search_tracks(artist, title)
        if not cands:
            return None

        def score(c: SpotifyCandidate) -> tuple:
            return (
                int(artist_matches(c.artists, artist)),
                int(titles_match(c.name, title)),
                {"album": 2, "single": 1}.get(c.album_type, 0),
                int(not EDITION_RE.search(c.album_name)),
                -c.rank,
            )

        best = max(cands, key=score)
        logger.info("Spotify canonical: %s — %s [%s] (was: %s — %s)",
                    best.artist, best.name, best.album_name, artist, title)
        return SpotifyTrack(track=best.to_track(), album_id=best.album_id,
                            album_name=best.album_name, total_tracks=best.total_tracks,
                            album_art_url=best.art_url)

    # ── albums ───────────────────────────────────────────────────────────────

    def get_album_tracklist(self, album_id: str) -> AlbumTracklist | None:
        """Full tracklist (all pages, discs in order). Cached per album id."""
        if album_id in self._album_cache:
            return self._album_cache[album_id]
        data = self._get(f"{ALBUM_URL}/{album_id}")
        if data is None:
            return None
        album_name = data.get("name", "")
        album_artist = data["artists"][0]["name"] if data.get("artists") else ""
        items: list[dict] = list(data.get("tracks", {}).get("items", []) or [])
        next_url = data.get("tracks", {}).get("next")
        pages = 0
        while next_url and pages < 10:                  # 50 tracks per page
            page = self._get(next_url)
            if page is None:
                break
            items.extend(page.get("items", []) or [])
            next_url = page.get("next")
            pages += 1

        tracks = []
        for item in items:
            artist_name = item["artists"][0]["name"] if item.get("artists") else album_artist
            duration_s = int(item.get("duration_ms", 0) / 1000) or None
            tracks.append(TrackInfo(
                title=item.get("name", ""), artist=artist_name, album=album_name,
                duration=duration_s, source="spotify_album", confidence=1.0,
                artists=[a.get("name", "") for a in item.get("artists", []) or []],
            ))
        tl = AlbumTracklist(
            album_id=album_id, album_name=album_name, artist=album_artist, tracks=tracks,
            release_date=data.get("release_date", "") or "", source="spotify",
        )
        logger.info("Fetched album tracklist: %s — %s (%d tracks)", album_artist, album_name,
                    len(tracks))
        self._album_cache[album_id] = tl
        return tl

    def fetch_art(self, url: str) -> bytes | None:
        """Download album art bytes (kept for the notifier; the screen uses art.py)."""
        if not url:
            return None
        try:
            resp = self._http.get(url, timeout=15)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            logger.warning("Failed to fetch album art: %s", e)
            return None


# ── helpers ──────────────────────────────────────────────────────────────────

def _candidate(t: dict, rank: int) -> SpotifyCandidate:
    album = t.get("album", {}) or {}
    images = album.get("images", []) or []
    art = max(images, key=lambda i: i.get("width", 0)).get("url", "") if images else ""
    return SpotifyCandidate(
        name=t.get("name", ""),
        artists=[a.get("name", "") for a in t.get("artists", []) or []],
        album_id=album.get("id", ""),
        album_name=album.get("name", ""),
        album_type=(album.get("album_type") or "").lower(),
        total_tracks=int(album.get("total_tracks", 0) or 0),
        release_date=album.get("release_date", "") or "",
        duration=int(t.get("duration_ms", 0) / 1000) or None,
        art_url=art,
        rank=rank,
    )


def artist_matches(candidates: list[str], wanted: str) -> bool:
    """Fingerprint artists come as "A", "A & B", "A feat. B"; a catalogue lists them
    separately. One whole name in common is a match ("Ye" is not "Yeat")."""
    return any(same_artist(c, wanted) for c in candidates if c)

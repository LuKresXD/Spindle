"""Deezer's public API as the second catalogue, and `Catalogue`, which asks Spotify first.

Spotify is the first source of canonical names, durations and album tracklists, and for a
long time it was the only one: a rate limit on the app (a day long, 2026-11-09), revoked
credentials or an outage then left every record that was not already in memory with its
Discogs titles and four-minute guesses for track lengths. Deezer's search and album
endpoints need no key, answer with the same shape of data (explicit titles intact, Cyrillic
intact) and allow 50 requests in 5 seconds.

`DeezerClient` speaks the two calls the resolver needs, with the same return types as
`SpotifyClient`; its album ids carry a "dz:" prefix. `Catalogue` routes: Spotify while it is
available and has an answer, Deezer otherwise.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

from .fingerprint import TrackInfo
from .spotify import _FEAT_RE, SpotifyCandidate, artist_matches
from .tracklist import AlbumTracklist, titles_match

logger = logging.getLogger(__name__)

API = "https://api.deezer.com"
PREFIX = "dz:"
_UA = "spindle/0.5 (+https://github.com/LuKresXD/Spindle)"
_SEARCH_LIMIT = 15
_ALBUMS_PER_SEARCH = 5        # distinct albums whose type and size are looked up for the ranking
_CACHE_MAX = 500
_QUOTA_BACKOFF = 6.0          # Deezer's quota window is 5 s
_RECORD_TYPES = {"album": "album", "single": "single", "ep": "single", "compile": "compilation"}
_VARIOUS = ("various artists", "various", "multi-artistes", "multi interpretes")


def _primary(album_artist: str, track_artist: str, title: str) -> str:
    """Whose track this is. Deezer sometimes files a track under one of its guests (1Train on
    LONG.LIVE.A$AP is "Yelawolf"): a track artist the title itself lists as a feature, on a
    single artist's album, is a guest; the track is the album artist's."""
    if not album_artist or album_artist.lower() in _VARIOUS:
        return track_artist or album_artist
    if not track_artist or track_artist.lower() == album_artist.lower():
        return album_artist
    m = _FEAT_RE.search(title or "")
    if m and track_artist.lower() in m.group(0).lower():
        return album_artist
    return track_artist


class DeezerClient:
    def __init__(self, session: requests.Session | None = None):
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA})
        self._albums: dict[str, dict] = {}                 # album id -> album JSON
        self._tracklists: dict[str, AlbumTracklist] = {}
        self._search_cache: dict[str, list[SpotifyCandidate]] = {}
        self._backoff_until = 0.0

    @property
    def available(self) -> bool:
        return time.time() >= self._backoff_until

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        """GET, None on any failure. Deezer reports quota and errors inside a 200."""
        if not self.available:
            return None
        try:
            resp = self._http.get(API + path, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("Deezer request failed: %s", e)
            return None
        err = data.get("error") if isinstance(data, dict) else None
        if err:
            if err.get("code") == 4:                       # quota: 50 requests / 5 s
                self._backoff_until = time.time() + _QUOTA_BACKOFF
            logger.warning("Deezer: %s", err.get("message") or err)
            return None
        return data

    # ── search ───────────────────────────────────────────────────────────────

    def search_tracks(self, artist: str, title: str) -> list[SpotifyCandidate]:
        """Candidates for a fingerprinted artist + title, Deezer's order, already filtered
        to the ones whose title and artist match (its plain search is generous)."""
        key = f"{artist.lower()}|||{title.lower()}"
        if key in self._search_cache:
            return self._search_cache[key]
        clean_title = _FEAT_RE.sub("", title).strip() or title
        data = self._get("/search", {"q": f"{artist} {clean_title}", "limit": _SEARCH_LIMIT})
        if data is None:
            return []                                      # transient: do not cache
        cands: list[SpotifyCandidate] = []
        albums_asked = 0
        for i, t in enumerate(data.get("data", []) or []):
            name = t.get("title", "") or ""
            who = (t.get("artist") or {}).get("name", "") or ""
            album = t.get("album") or {}
            aid = str(album.get("id") or "")
            if not aid or not titles_match(name, title):
                continue
            meta: dict[str, Any] = self._albums.get(aid) or {}
            if not meta and albums_asked < _ALBUMS_PER_SEARCH:
                albums_asked += 1
                meta = self._album(aid) or {}
            album_artist = ((meta.get("artist") or {}).get("name", "") or "") if meta else ""
            # the track is his when its artist is, or when it sits on his album (a guest's name
            # in the artist field must not hide it)
            if not artist_matches([who], artist) and not (
                    album_artist and album_artist.lower() not in _VARIOUS
                    and artist_matches([album_artist], artist)):
                continue
            who = _primary(album_artist, who, name)
            cands.append(SpotifyCandidate(
                name=name, artists=[who], album_id=PREFIX + aid,
                album_name=meta.get("title") or album.get("title", "") or "",
                album_type=_RECORD_TYPES.get((meta.get("record_type") or "").lower(), "album"),
                total_tracks=int(meta.get("nb_tracks") or 0),
                release_date=meta.get("release_date", "") or "",
                duration=int(t.get("duration") or 0) or None,
                art_url=album.get("cover_xl") or album.get("cover_big") or "",
                rank=i, provider="deezer",
            ))
        if len(self._search_cache) >= _CACHE_MAX:
            self._search_cache.pop(next(iter(self._search_cache)))
        self._search_cache[key] = cands
        return cands

    # ── albums ───────────────────────────────────────────────────────────────

    def _album(self, aid: str) -> dict | None:
        if aid in self._albums:
            return self._albums[aid]
        data = self._get(f"/album/{aid}")
        if data is None:
            return None
        if len(self._albums) >= _CACHE_MAX:
            self._albums.pop(next(iter(self._albums)))
        self._albums[aid] = data
        return data

    def get_album_tracklist(self, album_id: str) -> AlbumTracklist | None:
        """Full tracklist in play order (discs in order). Cached per album id."""
        if album_id in self._tracklists:
            return self._tracklists[album_id]
        aid = album_id.removeprefix(PREFIX)
        album = self._album(aid)
        if album is None:
            return None
        items = list((album.get("tracks") or {}).get("data", []) or [])
        if int(album.get("nb_tracks") or 0) > len(items):  # the inline list is capped
            page = self._get(f"/album/{aid}/tracks", {"limit": 500})
            if page is not None:
                items = list(page.get("data", []) or [])
        album_name = album.get("title", "") or ""
        album_artist = (album.get("artist") or {}).get("name", "") or ""
        tracks = []
        for item in items:
            who = _primary(album_artist, (item.get("artist") or {}).get("name", "") or "",
                           item.get("title", "") or "")
            tracks.append(TrackInfo(
                title=item.get("title", "") or "", artist=who, album=album_name,
                duration=int(item.get("duration") or 0) or None, source="deezer_album",
                confidence=1.0, artists=[who],
            ))
        if not tracks:
            return None
        tl = AlbumTracklist(
            album_id=PREFIX + aid, album_name=album_name, artist=album_artist, tracks=tracks,
            release_date=album.get("release_date", "") or "", source="deezer",
        )
        logger.info("Fetched album tracklist from Deezer: %s — %s (%d tracks)", album_artist,
                    album_name, len(tracks))
        self._tracklists[album_id] = tl
        return tl


class Catalogue:
    """The resolver's one catalogue: Spotify while it answers, Deezer when it cannot or has
    nothing. Either may be None."""

    def __init__(self, primary: Any = None, fallback: Any = None):
        self.primary = primary
        self.fallback = fallback
        self._fallback_logged = 0.0

    def search_tracks(self, artist: str, title: str) -> list[SpotifyCandidate]:
        cands: list[SpotifyCandidate] = []
        primary_up = self.primary is not None and getattr(self.primary, "available", True)
        if primary_up:
            cands = self.primary.search_tracks(artist, title)
        if not cands and self.fallback is not None:
            cands = self.fallback.search_tracks(artist, title)
            if cands and not primary_up and time.time() - self._fallback_logged > 1800:
                self._fallback_logged = time.time()
                logger.warning("Spotify is unavailable: names and track lengths come from "
                               "Deezer for now")
        return cands

    def get_album_tracklist(self, album_id: str) -> AlbumTracklist | None:
        if album_id.startswith(PREFIX):
            return self.fallback.get_album_tracklist(album_id) if self.fallback else None
        return self.primary.get_album_tracklist(album_id) if self.primary else None

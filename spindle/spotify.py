"""Spotify API lookup for canonical track/artist names.

Uses client credentials flow (no user auth needed) to search for tracks
and return Spotify's canonical artist + title format, e.g.:
  - Artist: "Playboi Carti" (primary only, not "Playboi Carti & Nicki Minaj")
  - Title:  "Poke It Out (with Nicki Minaj)"
"""

import logging
import time
from typing import Optional

import requests

from .config import SpotifyConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"


class SpotifyClient:
    def __init__(self, cfg: SpotifyConfig):
        self.cfg = cfg
        self._token: Optional[str] = None
        self._token_expiry: float = 0

    def _get_token(self) -> str:
        """Get (or refresh) a client credentials token."""
        if self._token and time.time() < self._token_expiry - 30:
            return self._token

        resp = requests.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.cfg.client_id, self.cfg.client_secret),
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._token_expiry = time.time() + data["expires_in"]
        logger.debug("Spotify token refreshed (expires in %ds)", data["expires_in"])
        return self._token

    def lookup(self, artist: str, title: str) -> Optional[TrackInfo]:
        """Search Spotify for a track and return canonical artist/title.

        Returns a TrackInfo with Spotify's canonical names, or None if not found.
        """
        try:
            token = self._get_token()
            query = f"track:{title} artist:{artist}"
            resp = requests.get(
                SEARCH_URL,
                params={"q": query, "type": "track", "limit": 5},
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            tracks = data.get("tracks", {}).get("items", [])
            if not tracks:
                logger.debug("Spotify: no results for '%s - %s'", artist, title)
                return None

            # Find best match — prefer exact artist name match
            best = None
            for t in tracks:
                spotify_artist = t["artists"][0]["name"]
                spotify_title = t["name"]
                # Check if it's a plausible match
                if (
                    artist.lower().split("&")[0].strip() in spotify_artist.lower()
                    or spotify_artist.lower() in artist.lower()
                ):
                    best = t
                    break

            if best is None:
                best = tracks[0]  # Fall back to top result

            spotify_artist = best["artists"][0]["name"]
            spotify_title = best["name"]
            spotify_album = best.get("album", {}).get("name")
            spotify_duration = int(best.get("duration_ms", 0) / 1000) or None

            logger.info(
                "Spotify canonical: %s — %s (was: %s — %s)",
                spotify_artist, spotify_title, artist, title,
            )

            return TrackInfo(
                title=spotify_title,
                artist=spotify_artist,
                album=spotify_album,
                duration=spotify_duration,
                source="spotify_lookup",
                confidence=1.0,
            )

        except Exception as e:
            logger.warning("Spotify lookup failed: %s", e)
            return None

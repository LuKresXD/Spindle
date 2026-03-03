"""Last.fm scrobbling via pylast."""

import hashlib
import logging
import time
from typing import Optional

import pylast

from .config import LastFMConfig, ScrobbleConfig
from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)


class Scrobbler:
    """Handles Last.fm authentication and scrobbling with dedup.

    Important: before scrobbling, we canonicalize artist/title using Last.fm
    corrections + proper capitalization so scrobbles "stack" with Spotify
    (which typically uses the corrected/canonical names).
    """

    def __init__(self, lastfm_cfg: LastFMConfig, scrobble_cfg: ScrobbleConfig):
        self.cfg = lastfm_cfg
        self.scrobble_cfg = scrobble_cfg
        self.network: Optional[pylast.LastFMNetwork] = None
        self._last_scrobble: Optional[str] = None  # "artist - title"
        self._last_scrobble_time: float = 0

    def connect(self) -> None:
        """Authenticate with Last.fm."""
        if not self.cfg.api_key or not self.cfg.api_secret:
            raise ValueError("Last.fm API key and secret are required")

        self.network = pylast.LastFMNetwork(
            api_key=self.cfg.api_key,
            api_secret=self.cfg.api_secret,
            username=self.cfg.username,
            password_hash=self.cfg.password_hash,
        )
        logger.info("Connected to Last.fm as %s", self.cfg.username)

    def update_now_playing(self, track: TrackInfo) -> None:
        """Update 'Now Playing' on Last.fm."""
        if not self.network or not self.scrobble_cfg.now_playing:
            return

        track = self.canonicalize(track)

        try:
            self.network.update_now_playing(
                artist=track.artist,
                title=track.title,
                album=track.album or "",
                duration=track.duration or 0,
            )
            logger.info("Now playing: %s - %s", track.artist, track.title)
        except Exception as e:
            logger.error("Failed to update now playing: %s", e)

    def scrobble(self, track: TrackInfo, timestamp: Optional[int] = None) -> bool:
        """Scrobble a track to Last.fm.

        Returns True if scrobbled, False if skipped (dedup) or failed.
        """
        if not self.network:
            logger.error("Not connected to Last.fm")
            return False

        track = self.canonicalize(track)

        # Dedup check (after canonicalization)
        track_key = f"{track.artist} - {track.title}"
        now = time.time()
        if (
            self._last_scrobble == track_key
            and now - self._last_scrobble_time < self.scrobble_cfg.dedup_window
        ):
            logger.debug(
                "Skipping duplicate scrobble: %s (within %ds window)",
                track_key,
                self.scrobble_cfg.dedup_window,
            )
            return False

        try:
            self.network.scrobble(
                artist=track.artist,
                title=track.title,
                timestamp=timestamp or int(now),
                album=track.album or "",
                duration=track.duration or 0,
            )
            self._last_scrobble = track_key
            self._last_scrobble_time = now
            logger.info("Scrobbled: %s", track_key)
            return True

        except Exception as e:
            logger.error("Scrobble failed: %s", e)
            return False

    def canonicalize(self, track: TrackInfo) -> TrackInfo:
        """Return a TrackInfo with Last.fm-corrected artist/title + duration.

        This improves stacking with Spotify scrobbles and keeps display text nice.

        - Applies track.get_correction() (e.g., "mrbrownstone" → "Mr. Brownstone")
        - Applies artist.get_correction() when available
        - Applies proper capitalization via Track.get_title(...)
        - Attempts to fill duration via Track.get_duration() (ms → s)
        """
        if not self.network:
            return track

        try:
            # Correct artist spelling
            artist_obj = self.network.get_artist(track.artist)
            corrected_artist = artist_obj.get_correction() or track.artist
        except Exception:
            corrected_artist = track.artist

        title = track.title
        duration = track.duration

        try:
            t = pylast.Track(corrected_artist, title, self.network)

            # Correct title spelling (returns corrected title string or None)
            corrected_title = t.get_correction() or title
            title = corrected_title

            # Proper capitalization
            try:
                title = t.get_title(properly_capitalized=True) or title
            except Exception:
                pass

            # Duration in ms
            if not duration:
                try:
                    dur_ms = t.get_duration()
                    if isinstance(dur_ms, int) and dur_ms > 0:
                        duration = int(dur_ms / 1000)
                except Exception:
                    pass

        except Exception:
            pass

        if corrected_artist != track.artist or title != track.title or duration != track.duration:
            return TrackInfo(
                title=title,
                artist=corrected_artist,
                album=track.album,
                duration=duration,
                mbid=track.mbid,
                source=track.source,
                confidence=track.confidence,
            )

        return track

    @staticmethod
    def hash_password(password: str) -> str:
        """Generate MD5 hash of password for pylast auth."""
        return hashlib.md5(password.encode()).hexdigest()

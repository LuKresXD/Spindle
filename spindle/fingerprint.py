"""Audio fingerprinting: AcoustID (Chromaprint) first, ShazamIO as the fallback.

`Identifier` is the long-lived object the engine's worker thread uses: it
keeps one asyncio loop and one Shazam client instead of building both for
every window, and applies the manual corrections file.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

import acoustid
import yaml

from .config import AcoustIDConfig, FingerprintConfig

logger = logging.getLogger(__name__)

SHAZAM_TIMEOUT = 10.0


@dataclass
class TrackInfo:
    """Identified track metadata."""
    title: str
    artist: str
    album: str | None = None
    duration: int | None = None  # seconds
    mbid: str | None = None  # MusicBrainz recording ID
    source: str = "acoustid"  # acoustid | shazam | spotify_lookup | spotify_album | discogs
    confidence: float = 0.0
    # Seconds into the track at the START of the identified audio window (Shazam reports
    # it; AcoustID does not). Two identifications whose offsets advance with the clock
    # pin the track's position; a repeated section can make a single one lie.
    offset: float | None = None


# ── Corrections ──────────────────────────────────────────────────────────────
# corrections.yaml: a list of
#   {match: {artist, title}, replace: {artist, title[, album]} | null}
# null → drop the match entirely (a sample that fingerprints as its source, etc.)

DEFAULT_CORRECTIONS_PATH: Path = Path.home() / "spindle" / "corrections.yaml"


class Corrections:
    def __init__(self, path: Path | None = None):
        self.path = path or DEFAULT_CORRECTIONS_PATH
        self._entries: list[dict] | None = None
        self._mtime = 0.0

    def entries(self) -> list[dict]:
        """Loaded once, reloaded when the file changes (edit without a restart)."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._entries is None:
                self._entries = []
            return self._entries
        if self._entries is None or mtime != self._mtime:
            try:
                with open(self.path) as f:
                    self._entries = yaml.safe_load(f) or []
                self._mtime = mtime
                logger.info("Loaded %d fingerprint corrections", len(self._entries))
            except Exception as e:
                logger.error("Failed to load %s: %s", self.path, e)
                self._entries = self._entries or []
        return self._entries

    def apply(self, track: TrackInfo) -> TrackInfo | None:
        """Corrected track, None when the match is blocked, or the track unchanged."""
        for entry in self.entries():
            match = entry.get("match", {}) or {}
            if (track.artist.lower() == match.get("artist", "").lower()
                    and track.title.lower() == match.get("title", "").lower()):
                replace = entry.get("replace")
                if replace is None:
                    logger.info("Correction: dropping '%s — %s' (blocked)",
                                track.artist, track.title)
                    return None
                logger.info("Correction: '%s — %s' → '%s — %s'", track.artist, track.title,
                            replace.get("artist", track.artist), replace.get("title", track.title))
                return TrackInfo(
                    title=replace.get("title", track.title),
                    artist=replace.get("artist", track.artist),
                    album=replace.get("album", track.album),
                    duration=track.duration, mbid=None,
                    source=track.source, confidence=track.confidence, offset=track.offset,
                )
        return track


_default_corrections = Corrections()


def apply_corrections(track: TrackInfo) -> TrackInfo | None:
    return _default_corrections.apply(track)


# ── AcoustID ─────────────────────────────────────────────────────────────────

def _best_recording(recordings: list[dict]) -> dict:
    """AcoustID returns every recording sharing the fingerprint, in no
    particular order; the one with the most submissions is the real one."""
    def score(r: dict) -> tuple:
        return (r.get("sources", 0), bool(r.get("artists")), bool(r.get("title")))
    return max(recordings, key=score)


def _best_album(release_groups: list[dict], artist: str) -> str | None:
    """Prefer a studio album by the same artist over compilations/singles.
    (The resolver re-decides this with the user's collection; this is the hint.)"""
    if not release_groups:
        return None
    def score(rg: dict) -> tuple:
        rtype = (rg.get("type") or "").lower()
        secondary = [s.lower() for s in rg.get("secondarytypes", [])]
        rg_artists = [a.get("name", "").lower() for a in rg.get("artists", [])]
        same_artist = artist.lower() in rg_artists if rg_artists else True
        return (same_artist, "compilation" not in secondary and "live" not in secondary,
                rtype == "album", rtype != "single")
    return max(release_groups, key=score).get("title")


def identify_acoustid(wav_path: Path, acoustid_cfg: AcoustIDConfig,
                      fp_cfg: FingerprintConfig) -> TrackInfo | None:
    """Identify a track with AcoustID/Chromaprint; None when nothing matches."""
    try:
        results = acoustid.match(acoustid_cfg.api_key, str(wav_path), parse=False)
        if results.get("status") != "ok":
            logger.warning("AcoustID API error: %s", results)
            return None
        ranked = sorted(results.get("results", []), key=lambda r: -float(r.get("score", 0)))
        for result in ranked:
            score = float(result.get("score", 0))
            if score < fp_cfg.min_confidence:
                break
            recordings = result.get("recordings", [])
            if not recordings:
                continue
            rec = _best_recording(recordings)
            artists = rec.get("artists") or []
            artist_name = artists[0].get("name", "Unknown") if artists else "Unknown"
            return TrackInfo(
                title=rec.get("title", "Unknown"), artist=artist_name,
                album=_best_album(rec.get("releasegroups", []), artist_name),
                duration=rec.get("duration"), mbid=rec.get("id"),
                source="acoustid", confidence=score,
            )
        logger.debug("AcoustID: no match above confidence threshold")
        return None
    except Exception as e:
        logger.error("AcoustID lookup failed: %s", e)
        return None


# ── Shazam ───────────────────────────────────────────────────────────────────

def _shazam_album(track: dict) -> str | None:
    for section in track.get("sections", []) or []:
        for item in section.get("metadata", []) or []:
            if (item.get("title") or "").lower() == "album" and item.get("text"):
                return item["text"]
    sections = track.get("sections") or []
    if sections:
        meta = sections[0].get("metadata") or []
        if meta and meta[0].get("text"):
            return meta[0]["text"]
    return None


def _shazam_offset(result: dict) -> float | None:
    """Where in the track the sample begins, from the first match (seconds)."""
    for m in result.get("matches") or []:
        off = m.get("offset")
        if isinstance(off, int | float) and off >= 0:
            return float(off)
        break
    return None


def _shazam_track(result: dict) -> TrackInfo | None:
    track = result.get("track")
    if not track:
        return None
    return TrackInfo(
        title=track.get("title", "Unknown"), artist=track.get("subtitle", "Unknown"),
        album=_shazam_album(track), source="shazam", confidence=1.0,
        offset=_shazam_offset(result),
    )


async def identify_shazam(wav_path: Path, shazam=None) -> TrackInfo | None:
    """Identify a track with ShazamIO; None when nothing matches."""
    try:
        if shazam is None:
            from shazamio import Shazam
            shazam = Shazam()
        result = await asyncio.wait_for(shazam.recognize(str(wav_path)), timeout=SHAZAM_TIMEOUT)
        track = _shazam_track(result)
        if track is None:
            logger.debug("ShazamIO: no match")
        return track
    except ImportError:
        logger.error("shazamio not installed — pip install shazamio")
        return None
    except asyncio.TimeoutError:
        logger.warning("ShazamIO lookup timed out (>%.0fs) — skipping", SHAZAM_TIMEOUT)
        return None
    except Exception as e:
        logger.error("ShazamIO lookup failed: %s", e)
        return None


# ── Identifier ───────────────────────────────────────────────────────────────

class Identifier:
    """AcoustID then Shazam, with one event loop and one Shazam client for the
    life of the process. Call from a single thread (the engine's worker)."""

    def __init__(self, acoustid_cfg: AcoustIDConfig, fp_cfg: FingerprintConfig,
                 corrections: Corrections | None = None):
        self.acoustid_cfg = acoustid_cfg
        self.fp_cfg = fp_cfg
        self.corrections = corrections or _default_corrections
        self._loop: asyncio.AbstractEventLoop | None = None
        self._shazam = None

    def _ensure_shazam(self):
        if self._loop is None:
            self._loop = asyncio.new_event_loop()
        if self._shazam is None:
            try:
                from shazamio import Shazam
                self._shazam = Shazam()
            except ImportError:
                logger.error("shazamio not installed — pip install shazamio")
                self.fp_cfg.shazam_fallback = False
        return self._shazam

    def identify(self, wav_path: Path) -> TrackInfo | None:
        track = identify_acoustid(wav_path, self.acoustid_cfg, self.fp_cfg)
        if track:
            logger.info("Identified via AcoustID: %s - %s (%.0f%%)",
                        track.artist, track.title, track.confidence * 100)
            return self.corrections.apply(track)

        if self.fp_cfg.shazam_fallback:
            shazam = self._ensure_shazam()
            if shazam is not None and self._loop is not None:
                track = self._loop.run_until_complete(identify_shazam(wav_path, shazam))
                if track:
                    logger.info("Identified via ShazamIO: %s - %s", track.artist, track.title)
                    return self.corrections.apply(track)

        logger.info("Could not identify track")
        return None

    def close(self) -> None:
        if self._loop is not None:
            with contextlib.suppress(Exception):
                self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()
            self._loop = None


def identify(wav_path: Path, acoustid_cfg: AcoustIDConfig,
             fp_cfg: FingerprintConfig) -> TrackInfo | None:
    """One-shot convenience (scripts, tests): a throwaway Identifier."""
    ident = Identifier(acoustid_cfg, fp_cfg)
    try:
        return ident.identify(wav_path)
    finally:
        ident.close()

"""Turn a fingerprint hit into the album that is actually playing.

A fingerprint says "artist, title" and sometimes an album. The same
recording sits on the album, on a single, on three compilations and on a
deluxe edition, and the identifier picks whichever the service lists first.
The resolver chooses with context:

  1. the pressing the user owns (Discogs collection) beats everything
  2. the album the session is already locked onto beats a new one
  3. album > single > compilation; standard edition > deluxe/remaster
  4. Spotify's own order breaks ties

When the chosen Spotify album is one the user owns, the tracklist is merged
with the pressing: record order and side positions from Discogs, names and
durations from Spotify. When Spotify does not know the record but the
collection does, the Discogs tracklist alone carries the album lock.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from .fingerprint import TrackInfo
from .spotify import EDITION_RE, SpotifyCandidate, artist_matches
from .tracklist import AlbumTracklist, box_part, covers, merge, side_of, titles_match

logger = logging.getLogger(__name__)

MERGE_MIN_COVERAGE = 0.5    # the Spotify edition must contain at least half the pressing


class _Spotify(Protocol):
    def search_tracks(self, artist: str, title: str) -> list[SpotifyCandidate]: ...
    def get_album_tracklist(self, album_id: str) -> AlbumTracklist | None: ...


class _Collection(Protocol):
    def releases_for_artist(self, artist: str) -> list: ...
    def find_release(self, artist: str, album: str): ...
    def releases_with_track(self, artist: str, title: str) -> list: ...
    def tracklist(self, rel) -> AlbumTracklist | None: ...


@dataclass
class ResolveContext:
    """What the session already believes; biases the choice, never forces it."""
    locked_album_id: str = ""
    locked_artist: str = ""


@dataclass
class Resolved:
    track: TrackInfo                        # canonical names for scrobbling
    album_id: str = ""                      # "" = no album known (simple mode)
    tracklist: AlbumTracklist | None = None
    art_url: str = ""
    owned: bool = False
    reason: str = ""                        # how the album was chosen (logs)
    offset: float | None = None             # the fingerprint's position in the track (s)

    @property
    def has_album(self) -> bool:
        return bool(self.album_id and self.tracklist and self.tracklist.tracks)


class Resolver:
    def __init__(self, spotify: _Spotify | None, collection: _Collection | None):
        self.spotify = spotify
        self.collection = collection

    def resolve(self, track: TrackInfo, ctx: ResolveContext | None = None) -> Resolved:
        res = self._resolve(track, ctx)
        res.offset = track.offset           # canonical names from the album, position from the ID
        return res

    def _resolve(self, track: TrackInfo, ctx: ResolveContext | None = None) -> Resolved:
        ctx = ctx or ResolveContext()
        owned_titles = self._owned_titles(track.artist)

        cand = self._best_spotify(track, ctx, owned_titles)
        if cand is not None:
            return self._from_spotify(cand, track)

        rel = self._owned_release_with(track)
        if rel is not None and self.collection is not None:
            tl = self.collection.tracklist(rel)
            if tl is not None:
                idx = tl.find_track_index(track.title)
                canonical = tl.tracks[idx] if idx is not None else TrackInfo(
                    title=track.title, artist=tl.artist, album=tl.album_name,
                    duration=track.duration, source=track.source, confidence=track.confidence)
                logger.info("Resolved via Discogs only: %s — %s [%s]", canonical.artist,
                            canonical.title, tl.album_name)
                return Resolved(canonical, tl.album_id, tl, "", True, "discogs")

        return Resolved(track, "", None, "", False, "none")

    # ── Spotify ──────────────────────────────────────────────────────────────

    def _best_spotify(self, track: TrackInfo, ctx: ResolveContext,
                      owned_titles: list[str]) -> SpotifyCandidate | None:
        if self.spotify is None:
            return None
        cands = self.spotify.search_tracks(track.artist, track.title)
        if not cands:
            return None
        from .discogs import same_album

        def score(c: SpotifyCandidate) -> tuple:
            return (
                int(titles_match(c.name, track.title)),
                int(artist_matches(c.artists, track.artist)),
                int(any(same_album(c.album_name, t) for t in owned_titles)),
                int(bool(ctx.locked_album_id) and c.album_id == ctx.locked_album_id),
                {"album": 2, "single": 1}.get(c.album_type, 0),
                int(not EDITION_RE.search(c.album_name)),
                -c.rank,
            )

        best = max(cands, key=score)
        if not titles_match(best.name, track.title):
            logger.debug("Spotify: no candidate titled like '%s' (best: '%s')",
                         track.title, best.name)
            return None
        if not artist_matches(best.artists, track.artist):
            logger.debug("Spotify: no candidate by %s (best: %s)", track.artist, best.artists)
            return None
        return best

    def _from_spotify(self, cand: SpotifyCandidate, track: TrackInfo) -> Resolved:
        canonical = cand.to_track()
        tl = self.spotify.get_album_tracklist(cand.album_id) if self.spotify else None
        reason = f"spotify {cand.album_type or 'album'}"
        owned = False
        if tl is not None and self.collection is not None:
            rel = (self.collection.find_release(cand.artist, cand.album_name)
                   or self.collection.find_release(track.artist, cand.album_name))
            if rel is None:
                # the owned record may carry a different title than the streaming edition
                for r in self.collection.releases_with_track(cand.artist, cand.name):
                    rel = r
                    break
            if rel is not None:
                owned = True
                pressing = self.collection.tracklist(rel)
                if pressing is not None and pressing.tracks:
                    cov = covers(tl, pressing)
                    part = None
                    if cov < MERGE_MIN_COVERAGE:
                        # a box set: the album sits on a few of the pressing's sides
                        part = box_part(pressing, tl, MERGE_MIN_COVERAGE)
                        if part is not None:
                            pressing, cov = part, covers(tl, part)
                    if cov >= MERGE_MIN_COVERAGE:
                        tl = merge(tl, pressing)
                        if part is not None:
                            sides = f"{side_of(part.positions[0])}-{side_of(part.positions[-1])}"
                            reason = f"spotify+discogs ({cov:.0%} of sides {sides} of the box)"
                        else:
                            reason = f"spotify+discogs ({cov:.0%} of pressing)"
                    else:
                        reason = f"spotify (owned pressing differs, {cov:.0%} overlap)"
                else:
                    tl.owned = True
                    reason = "spotify (owned, pressing not loaded yet)"
        logger.info("Resolved: %s — %s [%s] via %s", canonical.artist, canonical.title,
                    cand.album_name, reason)
        return Resolved(canonical, cand.album_id, tl, cand.art_url, owned, reason)

    # ── Discogs ──────────────────────────────────────────────────────────────

    def _owned_titles(self, artist: str) -> list[str]:
        if self.collection is None:
            return []
        try:
            return [r.title for r in self.collection.releases_for_artist(artist)]
        except Exception as e:
            logger.debug("Collection lookup failed: %s", e)
            return []

    def _owned_release_with(self, track: TrackInfo):
        if self.collection is None:
            return None
        try:
            rels = self.collection.releases_with_track(track.artist, track.title)
        except Exception as e:
            logger.debug("Collection lookup failed: %s", e)
            return None
        if not rels:
            return None
        if track.album:
            from .discogs import same_album
            for r in rels:
                if same_album(r.title, track.album):
                    return r
        return rels[0]

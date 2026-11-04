"""Album tracklists, independent of where they came from.

A session locks onto an `AlbumTracklist`. Spotify gives the streaming
edition (canonical names, durations, release date); the user's Discogs
collection gives the pressing that is actually on the platter (track order,
side positions, sometimes durations). `merge()` combines the two: the
record's order and positions win, Spotify fills in names and durations.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

from .fingerprint import TrackInfo

_FUZZY_MIN = 0.86


def norm_title(s: str) -> str:
    """Loose title key: case, punctuation, apostrophes, edition tags, feat. credits."""
    s = s.lower().replace("'", "").replace("’", "")
    s = re.sub(r"[\(\[](feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\[](remaster(?:ed)?|bonus|album version|explicit)[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"\s[-–—]\s*(remaster(?:ed)?|bonus track|album version).*$", " ", s)
    s = re.sub(r"[^a-z0-9Ѐ-ӿ]+", " ", s)
    return " ".join(s.split())


def titles_match(a: str, b: str) -> bool:
    """Equal after normalisation, or near-equal (one typo / stray punctuation)."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _FUZZY_MIN


def side_of(position: str) -> str:
    """Side letter of a vinyl position: "A3" -> "A", "2A" -> "A", "B" -> "B";
    CD-style "1-4" / "12" / "" -> ""."""
    m = re.match(r"\s*(?:\d+\s*[.-]?\s*)?([A-Za-z]{1,2})\s*[.-]?\s*\d*\s*$", position or "")
    return m.group(1).upper() if m else ""


@dataclass
class AlbumTracklist:
    """One album's tracks, in the order they play."""
    album_id: str
    album_name: str
    artist: str
    tracks: list = field(default_factory=list)      # list[TrackInfo], in play order
    release_date: str = ""                          # "2004-03-23" / "2004"
    positions: list = field(default_factory=list)   # list[str] parallel to tracks ("A3"), or []
    source: str = "spotify"                         # "spotify" | "discogs" | "merged"
    discogs_release_id: int | None = None
    owned: bool = False                             # in the user's Discogs collection

    @property
    def year(self) -> str:
        return self.release_date[:4] if self.release_date else ""

    def get_track_at(self, index: int) -> TrackInfo | None:
        if 0 <= index < len(self.tracks):
            return self.tracks[index]
        return None

    def find_track_index(self, title: str) -> int | None:
        """Index by exact title (case-insensitive), else by normalised/fuzzy title."""
        t = title.lower()
        for i, tr in enumerate(self.tracks):
            if tr.title.lower() == t:
                return i
        want = norm_title(title)
        for i, tr in enumerate(self.tracks):
            if norm_title(tr.title) == want:
                return i
        for i, tr in enumerate(self.tracks):
            if titles_match(tr.title, title):
                return i
        return None

    # ── sides ────────────────────────────────────────────────────────────────

    def position_at(self, index: int) -> str:
        if 0 <= index < len(self.positions):
            return self.positions[index] or ""
        return ""

    def side_at(self, index: int) -> str:
        return side_of(self.position_at(index))

    @property
    def has_sides(self) -> bool:
        return any(side_of(p) for p in self.positions)

    def is_side_end(self, index: int) -> bool:
        """True when `index` is the last track of its side (the run-out groove follows).

        Unknown when there are no positions: returns False so timing keeps advancing.
        """
        side = self.side_at(index)
        if not side:
            return False
        nxt = self.side_at(index + 1)
        return nxt != side

    def total_duration(self) -> int:
        return sum(t.duration or 0 for t in self.tracks)


# ── Discogs ↔ Spotify merge ──────────────────────────────────────────────────

def merge(streaming: AlbumTracklist, pressing: AlbumTracklist) -> AlbumTracklist:
    """The pressing's order and positions, the streaming edition's names and durations.

    Tracks on the record that the streaming edition lacks keep their Discogs
    title and duration (which may be None). Streaming tracks absent from the
    record are dropped: they are not on the platter.
    """
    used: set[int] = set()
    tracks: list[TrackInfo] = []
    for dt in pressing.tracks:
        match: TrackInfo | None = None
        for j, st in enumerate(streaming.tracks):
            if j in used:
                continue
            if titles_match(dt.title, st.title):
                match, used_j = st, j
                used.add(used_j)
                break
        if match is not None:
            tracks.append(TrackInfo(
                title=match.title, artist=match.artist, album=streaming.album_name,
                duration=match.duration or dt.duration, mbid=match.mbid,
                source="spotify_album", confidence=1.0,
            ))
        else:
            tracks.append(TrackInfo(
                title=dt.title, artist=dt.artist or streaming.artist, album=streaming.album_name,
                duration=dt.duration, source="discogs", confidence=1.0,
            ))
    return AlbumTracklist(
        album_id=streaming.album_id,
        album_name=streaming.album_name,
        artist=streaming.artist,
        tracks=tracks,
        release_date=streaming.release_date or pressing.release_date,
        positions=list(pressing.positions),
        source="merged",
        discogs_release_id=pressing.discogs_release_id,
        owned=True,
    )


def covers(streaming: AlbumTracklist, pressing: AlbumTracklist) -> float:
    """Fraction of the pressing's tracks found in the streaming edition (0..1)."""
    if not pressing.tracks:
        return 0.0
    hit = 0
    for dt in pressing.tracks:
        if any(titles_match(dt.title, st.title) for st in streaming.tracks):
            hit += 1
    return hit / len(pressing.tracks)

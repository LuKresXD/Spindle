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
from functools import lru_cache

from .fingerprint import TrackInfo
from .names import fold, words

_FUZZY_MIN = 0.86


@lru_cache(maxsize=8192)
def norm_title(s: str) -> str:
    """Loose title key: case, accents, punctuation, apostrophes, edition tags, feat. credits.
    Letters of every script are kept (a Greek or Japanese title used to normalise to nothing)."""
    s = fold(s).replace("'", "").replace("’", "")
    s = re.sub(r"[\(\[](feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]", " ", s)
    s = re.sub(r"[\(\[](?:\d{4}\s+)?(remaster(?:ed)?|bonus|album version|explicit)[^\)\]]*[\)\]]",
               " ", s)
    # "Bad - 2012 Remaster", "Heroes - Remastered 2017": with the year first the suffix stayed, and
    # a three-letter title then matched nothing but the compilation that carries it bare
    s = re.sub(r"\s[-–—]\s*(?:\d{4}\s+)?(remaster(?:ed)?|bonus track|album version).*$", " ", s)
    return words(s)


@lru_cache(maxsize=32768)
def titles_match(a: str, b: str) -> bool:
    """Equal after normalisation, or near-equal (one typo / stray punctuation)."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        # nothing to compare by ("?", "..."): the same only when written the same
        return bool(a.strip()) and a.strip().lower() == b.strip().lower()
    if na == nb:
        return True
    if min(len(na), len(nb)) >= 6 and (na in nb or nb in na):
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= _FUZZY_MIN


def side_of(position: str) -> str:
    """Side letter of a vinyl position: "A3" -> "A", "2A" -> "A", "B" -> "B", and the
    sub-track forms Discogs uses, "B2.1" / "A3a" / "LP1-A1" -> their side (Take Care's
    "B2.1" read as no side at all, which made B1 look like the end of side B).
    CD-style "1-4" / "12" / "CD1" / "" -> ""."""
    pos = (position or "").strip()
    if re.match(r"(?i)(cd|dvd|bd|vhs|mp3|video|digital|file)\b|(cd|dvd|bd)\d", pos):
        return ""
    m = re.match(r"(?i)(?:lp\s*\d+\s*[-.:]?\s*)?(?:\d+\s*[-.:]?\s*)?([a-z]{1,2})(?![a-z])", pos)
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

    def find_track_index(self, title: str, near: int | None = None) -> int | None:
        """Index by exact title (case-insensitive), else by normalised, else by fuzzy title.

        A title can be on a record twice (the album cut on side B, its remix closing side D).
        Without `near` the best-matching spelling wins, and the first of equals. With `near`
        (the index the needle is believed to be at) the copy on that side wins, then the
        better spelling, then the nearer one: a needle does not change sides mid-session.
        """
        exact, want = title.lower(), norm_title(title)
        found: list[tuple[int, int]] = []                # (kind of match, index)
        for i, tr in enumerate(self.tracks):
            if tr.title.lower() == exact:
                found.append((0, i))
            elif norm_title(tr.title) == want:
                found.append((1, i))
            elif titles_match(tr.title, title):
                found.append((2, i))
        if not found:
            return None
        if near is None or len(found) == 1:
            return min(found)[1]
        side = self.side_at(near)
        return min(found, key=lambda f: (bool(side) and self.side_at(f[1]) != side, f[0],
                                         abs(f[1] - near), f[1]))[1]

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
                source="spotify_album", confidence=1.0, artists=list(match.artists),
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


def box_part(pressing: AlbumTracklist, streaming: AlbumTracklist,
             min_fill: float = 0.5) -> AlbumTracklist | None:
    """The sides of a box set that carry this streaming album, as a pressing of their own.

    A box (Sade's "This Far": six records, sides A to L, one Discogs release) covers a
    streaming album with two of its sides, so the whole box never reaches the merge
    threshold. The run of sides from the first to the last that holds one of the
    album's tracks does: return just those, in the pressing's order, so the side map
    and the flips still work. None when the pressing has no sides or fewer than four
    (one record), when the album's tracks are scattered across the box (a compilation
    box), or when the run is the whole pressing already (then the normal coverage rule
    has the last word).
    """
    if not pressing.positions or len(pressing.positions) != len(pressing.tracks):
        return None
    sides = [side_of(pos) for pos in pressing.positions]
    if not all(sides):
        return None
    order = list(dict.fromkeys(sides))                  # side letters in pressing order
    hits = {side for side, t in zip(sides, pressing.tracks, strict=True)
            if any(titles_match(t.title, st.title) for st in streaming.tracks)}
    if not hits:
        return None
    first = min(order.index(side) for side in hits)
    last = max(order.index(side) for side in hits)
    keep = set(order[first:last + 1])
    if len(order) < 4 or keep == set(order):
        return None         # one record is never cut: a half-matched album is another edition
    idx = [i for i, side in enumerate(sides) if side in keep]
    part = AlbumTracklist(
        album_id=pressing.album_id, album_name=pressing.album_name, artist=pressing.artist,
        tracks=[pressing.tracks[i] for i in idx], release_date=pressing.release_date,
        positions=[pressing.positions[i] for i in idx], source=pressing.source,
        discogs_release_id=pressing.discogs_release_id, owned=pressing.owned,
    )
    return part if covers(streaming, part) >= min_fill else None


def covers(streaming: AlbumTracklist, pressing: AlbumTracklist) -> float:
    """Fraction of the pressing's tracks found in the streaming edition (0..1)."""
    if not pressing.tracks:
        return 0.0
    hit = 0
    for dt in pressing.tracks:
        if any(titles_match(dt.title, st.title) for st in streaming.tracks):
            hit += 1
    return hit / len(pressing.tracks)

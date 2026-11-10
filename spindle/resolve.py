"""Turn a fingerprint hit into the record that is actually playing.

A fingerprint says "artist, title" and sometimes an album. The same recording sits on the
album, on a single, on three compilations and on a deluxe edition, and a catalogue search
lists them in an order of its own (Deezer likes the single first). The resolver chooses with
what it knows about the shelf:

  1. The record comes first. A hit whose title is on a pressing the user owns is that
     pressing: its order and its sides are the truth. The catalogue's part is to dress it,
     and the edition that dresses it is chosen by how well the two lists agree (how much
     of the pressing it contains, and how much of it is on the pressing), never by its
     name: "Blond" on the sleeve is "Blonde" in the catalogue, the best-of that lists the
     hit first contains a third of the record, and the 40-track anthology contains all of
     it and thirty tracks that are not on it.
  2. When none of the hit's own albums contains the record (a bonus track that only ever
     came out as a single, a remix closing side D), the edition another track of the same
     pressing found is used, or the catalogue is asked about a few of its other tracks.
  3. When no edition contains it at all (the pressing differs wholesale, a compilation the
     catalogue never had), the pressing itself is the album: Discogs' list, with names and
     lengths filled in track by track from the catalogue as they are heard.
  4. A hit that is on no owned pressing is a streaming album: the one the session is locked
     on, album > single > compilation, standard > deluxe, then the catalogue's order.

One list object per pressing and edition is handed out for as long as the process lives, so
what is learned later (a length, a canonical name) reaches the session that locked earlier.
"""

from __future__ import annotations

import dataclasses
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from .fingerprint import TrackInfo
from .names import artist_names
from .spotify import EDITION_RE, SpotifyCandidate, artist_matches
from .tracklist import AlbumTracklist, box_part, covers, merge, norm_title, side_of, titles_match

logger = logging.getLogger(__name__)

MERGE_MIN_COVERAGE = 0.5    # a catalogue edition must contain at least half the pressing
MIN_MATCH = 0.5             # ... and agree with it at least this well both ways (a 40-track
                            # anthology contains a whole album; a 12-track album a whole 7")
GOOD_MATCH = 0.95           # an edition that agrees with the pressing this well ends the search
MAX_EDITIONS = 5            # catalogue albums compared with a pressing for one hit
MAX_PRESSINGS = 3           # owned records tried for one hit
PROBES = 4                  # other tracks of a pressing asked about when a hit's albums miss it
NO_EDITION_TTL = 3600       # how long "no edition contains this pressing" is believed (an
                            # outage looks the same from here)
FILL_PER_HIT = 2            # track lengths asked for per identification on a list lacking some
FILL_RETRY = 3600           # seconds before a length that was not found is asked for again


class _Catalogue(Protocol):
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
    locked_release_id: int | None = None    # the Discogs release the lock is a pressing of


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


@dataclass
class _Edition:
    """A catalogue album that contains a pressing, or the sides of a box it contains."""
    album_id: str
    tracklist: AlbumTracklist
    coverage: float                         # how much of the pressing (or part) it contains
    match: float                            # agreement both ways (F1 of coverage and its mirror)
    part: tuple[int, ...] | None = None     # box set: the pressing's indices this album is
    art_url: str = ""
    provider: str = "spotify"
    order: int = 0                          # where the catalogue's answers listed it (0 = first)


@dataclass
class _Shelf:
    """What has been learned about one owned release since the process started."""
    edition: _Edition | None = None         # the catalogue album that is the whole pressing
    asked: dict = field(default_factory=dict)    # side -> (when, edition | None): asked the
                                                 # catalogue about the record's other tracks
    lists: dict = field(default_factory=dict)    # (album id, first position) -> AlbumTracklist
    fill_tried: dict = field(default_factory=dict)   # title key -> when its length was asked


@dataclass
class _Plan:
    """One owned pressing a hit could be on, and how it would be dressed."""
    rel: object
    pressing: AlbumTracklist
    pidx: int                               # the hit's index on the pressing
    edition: _Edition | None


def _sides(pressing: AlbumTracklist, idx: tuple[int, ...]) -> AlbumTracklist:
    """The tracks of a box set at these indices, as a pressing of their own."""
    return AlbumTracklist(
        album_id=pressing.album_id, album_name=pressing.album_name, artist=pressing.artist,
        tracks=[pressing.tracks[i] for i in idx], release_date=pressing.release_date,
        positions=[pressing.positions[i] for i in idx], source=pressing.source,
        discogs_release_id=pressing.discogs_release_id, owned=pressing.owned,
    )


class Resolver:
    def __init__(self, catalogue: _Catalogue | None, collection: _Collection | None):
        self.catalogue = catalogue
        self.collection = collection
        self._shelf: dict[int, _Shelf] = {}
        try:
            params = inspect.signature(catalogue.search_tracks).parameters if catalogue else {}
        except (TypeError, ValueError):
            params = {}
        self._search_takes_names = "also" in params

    def resolve(self, track: TrackInfo, ctx: ResolveContext | None = None) -> Resolved:
        res = self._resolve(track, ctx or ResolveContext())
        res.offset = track.offset           # canonical names from the album, position from the ID
        return res

    def _resolve(self, track: TrackInfo, ctx: ResolveContext) -> Resolved:
        cands = self._candidates(track.artist, track.title, ctx)
        try:
            plan = self._pressing_for(track, cands, ctx)
            if plan is not None:
                return self._on_pressing(plan, cands)
        except Exception:
            # a fault here must cost this one answer its record, not every answer for it
            logger.exception("Resolver: %s — %s could not be placed on the shelf", track.artist,
                             track.title)
        if cands:
            return self._streaming(cands[0], track)
        return Resolved(track, "", None, "", False, "none")

    # ── the catalogue's answers for one title ────────────────────────────────

    def _candidates(self, artist: str, title: str,
                    ctx: ResolveContext | None = None) -> list[SpotifyCandidate]:
        """Catalogue tracks with this title by this artist, likeliest album first."""
        if self.catalogue is None or not artist or not title:
            return []
        # the artist under every name the shelf knows them by (Discogs: Ye is Kanye West)
        names_for = getattr(self.collection, "names_for", None)
        who = [artist, *(names_for(artist) if names_for else [])]
        if len(who) > 1 and self._search_takes_names:
            found = self.catalogue.search_tracks(artist, title, also=tuple(who[1:]))  # type: ignore[call-arg]
        else:
            found = self.catalogue.search_tracks(artist, title)
        cands = [c for c in found if titles_match(c.name, title)
                 and any(artist_matches(c.artists, a) for a in who)]
        if not cands:
            if found:
                logger.debug("Catalogue: nothing titled like '%s' by %s among %d results",
                             title, artist, len(found))
            return []
        from .discogs import same_album
        owned_titles = self._owned_titles(artist)
        locked = ctx.locked_album_id if ctx else ""

        def score(c: SpotifyCandidate) -> tuple:
            return (
                int(any(same_album(c.album_name, t) for t in owned_titles)),
                int(bool(locked) and c.album_id == locked),
                {"album": 2, "single": 1}.get(c.album_type, 0),
                int(not EDITION_RE.search(c.album_name)),
                -c.rank,
            )

        return sorted(cands, key=score, reverse=True)

    # ── a hit on an owned pressing ───────────────────────────────────────────

    def _pressing_for(self, track: TrackInfo, cands: list[SpotifyCandidate],
                      ctx: ResolveContext) -> _Plan | None:
        """The owned pressing this hit is on, if any. When the title is on several of the
        user's records: the one the session is locked on, else the one the fingerprint
        names as its album, else the one whose edition the catalogue lists first for this
        track, else the collection's order (vinyl, a copy that knows its sides)."""
        if self.collection is None:
            return None
        found: dict[int, object] = {}
        asked = {(track.artist, track.title)}
        try:
            for r in self.collection.releases_with_track(track.artist, track.title):
                found.setdefault(r.id, r)
            for c in cands[:3]:             # the catalogue's spelling of the artist and title
                if (c.artist, c.name) in asked:
                    continue
                asked.add((c.artist, c.name))
                for r in self.collection.releases_with_track(c.artist, c.name):
                    found.setdefault(r.id, r)
        except Exception as e:
            logger.debug("Collection lookup failed: %s", e)
            return None
        if not found:
            return None
        from .discogs import same_album
        rels = list(found.values())
        certain = [r for r in rels if r.id == ctx.locked_release_id]            # type: ignore[attr-defined]
        if not certain and track.album:
            certain = [r for r in rels if same_album(r.title, track.album)]     # type: ignore[attr-defined]
        plans = []
        for rel in certain or rels[:MAX_PRESSINGS]:
            plan = self._plan(rel, track, cands)
            if plan is None:
                continue
            if certain or len(rels) == 1:
                return plan
            plans.append(plan)
        if not plans:
            return None
        far = len(cands) + 1
        return min(plans, key=lambda p: p.edition.order if p.edition is not None else far)

    def _plan(self, rel, track: TrackInfo, cands: list[SpotifyCandidate]) -> _Plan | None:
        assert self.collection is not None
        pressing = self.collection.tracklist(rel)
        if pressing is None or not pressing.tracks:
            return None
        pidx = pressing.find_track_index(track.title)
        if pidx is None:
            for c in cands[:3]:
                pidx = pressing.find_track_index(c.name)
                if pidx is not None:
                    break
        if pidx is None:
            return None
        shelf = self._shelf.setdefault(rel.id, _Shelf())
        return _Plan(rel, pressing, pidx, self._edition(pressing, pidx, cands, shelf))

    def _on_pressing(self, plan: _Plan, cands: list[SpotifyCandidate]) -> Resolved:
        rel, pressing, pidx, ed = plan.rel, plan.pressing, plan.pidx, plan.edition
        shelf = self._shelf[rel.id]                                           # type: ignore[attr-defined]
        if ed is not None:
            base = _sides(pressing, ed.part) if ed.part is not None else pressing
            idx = ed.part.index(pidx) if ed.part is not None else pidx
            key = (ed.album_id, base.positions[0] if ed.part is not None else "")
            tl = shelf.lists.get(key)
            if tl is None or len(tl.tracks) != len(base.tracks):
                tl = shelf.lists[key] = merge(ed.tracklist, base)
            if ed.part is not None:
                sides = f"{side_of(base.positions[0])}-{side_of(base.positions[-1])}"
                reason = f"{ed.provider}+discogs ({ed.coverage:.0%} of sides {sides} of the box)"
            else:
                reason = f"{ed.provider}+discogs ({ed.coverage:.0%} of pressing)"
            art_url = ed.art_url
        else:
            idx = pidx
            key = ("", "")
            tl = shelf.lists.get(key)
            if tl is None or len(tl.tracks) != len(pressing.tracks):
                tl = shelf.lists[key] = pressing
            reason = "discogs"
            art_url = cands[0].art_url if cands else ""

        entry = tl.tracks[idx]
        if entry.source == "discogs":
            # the record's list had only Discogs' words for this track: the catalogue's name
            # for it, who is on it, and its length when the sleeve prints none. Only from the
            # same title: a live cut or a radio edit listed first is another recording.
            same = norm_title(entry.title)
            own = next((c for c in cands if norm_title(c.name) == same), None)
            if own is not None:
                entry.title, entry.artist, entry.artists = own.name, own.artist, list(own.artists)
                entry.duration = entry.duration or own.duration
                entry.source = f"{own.provider}_lookup"
        self._fill_lengths(tl, idx, shelf)
        if not art_url:
            art_url = getattr(rel, "cover", "") or ""
        logger.info("Resolved: %s — %s [%s] via %s", entry.artist, entry.title, tl.album_name,
                    reason)
        return Resolved(dataclasses.replace(entry), tl.album_id, tl, art_url, True, reason)

    def _edition(self, pressing: AlbumTracklist, pidx: int, cands: list[SpotifyCandidate],
                 shelf: _Shelf) -> _Edition | None:
        """The catalogue album to dress this pressing with: the one whose list agrees best
        with the pressing's. One record, one edition: a whole-pressing edition, once found,
        is every later track's too unless a better one turns up."""
        known = shelf.edition
        if known is not None and known.match >= GOOD_MATCH:
            return known
        best: _Edition | None = known
        seen: set[str] = set()
        for order, c in enumerate(cands):
            if c.album_id in seen:
                continue
            if len(seen) >= MAX_EDITIONS:
                break
            seen.add(c.album_id)
            ed = self._compare(c, pressing, pidx, order)
            if ed is None:
                continue
            if best is None or ed.match > best.match:
                best = ed
            if ed.match >= GOOD_MATCH:
                break
        if best is None or best.match < GOOD_MATCH:
            # nothing, or only a loose fit (the hit was listed on the expanded edition alone):
            # the record's other tracks may know better, and asking now keeps one album name
            # on the whole side
            asked = self._ask_for_edition(pressing, pidx, shelf)
            if asked is not None and (best is None or asked.match > best.match):
                best = asked
        if best is not None and best.part is None:
            shelf.edition = best
        return best

    def _compare(self, c: SpotifyCandidate, pressing: AlbumTracklist, pidx: int,
                 order: int) -> _Edition | None:
        """This candidate's album against the pressing (or, for a box set, against the sides
        of it the album is, when the track heard is on them). None when it contains less
        than half."""
        stl = self.catalogue.get_album_tracklist(c.album_id) if self.catalogue else None
        if stl is None or not stl.tracks:
            return None
        part = None
        base = pressing
        cov = covers(stl, pressing)
        if cov < MERGE_MIN_COVERAGE:
            cut = box_part(pressing, stl, MERGE_MIN_COVERAGE)
            if cut is None:
                return None
            part = tuple(i for i, t in enumerate(pressing.tracks)
                         if any(t is p for p in cut.tracks))
            if pidx not in part:
                return None
            base, cov = cut, covers(stl, cut)
        mirror = covers(base, stl)                   # how much of the album is on the record
        match = 2 * cov * mirror / (cov + mirror) if cov + mirror else 0.0
        if match < MIN_MATCH:
            return None
        return _Edition(c.album_id, stl, cov, match, part, c.art_url, c.provider, order)

    def _ask_for_edition(self, pressing: AlbumTracklist, pidx: int,
                         shelf: _Shelf) -> _Edition | None:
        """None of the heard track's albums is this record. Ask about a few of its other
        tracks, its neighbours on the side first: the album they are on is the record's
        edition (or, in a box set, this side's)."""
        now = time.time()
        side = pressing.side_at(pidx)
        when, memo = shelf.asked.get(side, (0.0, None))
        if memo is not None and (memo.part is None or pidx in memo.part):
            return memo
        if self.catalogue is None or now - when < NO_EDITION_TTL:
            return None
        n = len(pressing.tracks)
        near = sorted((i for i in range(n) if i != pidx and pressing.side_at(i) == side),
                      key=lambda i: (abs(i - pidx), i))
        picks = near[:PROBES - 1]
        for i in (0, n // 2, n - 1, *near[PROBES - 1:]):
            if i != pidx and i not in picks:
                picks.append(i)
        best: _Edition | None = None
        for i in picks[:PROBES]:
            t = pressing.tracks[i]
            who = (artist_names(t.artist or pressing.artist) or [""])[0]
            seen: set[str] = set()
            for c in self._candidates(who, t.title):
                if c.album_id in seen:
                    continue
                if len(seen) >= 3:
                    break
                seen.add(c.album_id)
                ed = self._compare(c, pressing, pidx, len(pressing.tracks))
                if ed is not None and (best is None or ed.match > best.match):
                    best = ed
            if best is not None and best.match >= GOOD_MATCH:
                break
        shelf.asked[side] = (now, best)
        if best is None:
            logger.info("Asked about %s — %s%s by its other tracks: no better edition",
                        pressing.artist, pressing.album_name, f" (side {side})" if side else "")
            return None
        logger.info("%s — %s is %s in the catalogue (%.0f%% of %s), found by its other tracks",
                    pressing.artist, pressing.album_name, best.tracklist.album_name,
                    best.coverage * 100, "those sides" if best.part is not None else "the pressing")
        return best

    def _fill_lengths(self, tl: AlbumTracklist, idx: int, shelf: _Shelf) -> None:
        """A few of the lengths this list lacks, the tracks after the one heard first: the
        clock needs them next. Each is asked once (and again after a while if not found)."""
        if self.catalogue is None:
            return
        now = time.time()
        n = len(tl.tracks)
        asked = 0
        for i in [*range(idx + 1, n), *range(0, idx)]:
            t = tl.tracks[i]
            if t.duration:
                continue
            key = norm_title(t.title)
            if now - shelf.fill_tried.get(key, 0.0) < FILL_RETRY:
                continue
            shelf.fill_tried[key] = now
            asked += 1
            who = (artist_names(t.artist or tl.artist) or [""])[0]
            for c in self._candidates(who, t.title):
                if c.duration and norm_title(c.name) == key:
                    t.duration = c.duration
                    break
            if asked >= FILL_PER_HIT:
                return

    # ── a hit on nothing the user owns (or on a record whose list is not loaded yet) ──

    def _streaming(self, cand: SpotifyCandidate, track: TrackInfo) -> Resolved:
        canonical = cand.to_track()
        tl = self.catalogue.get_album_tracklist(cand.album_id) if self.catalogue else None
        src = cand.provider
        reason = f"{src} {cand.album_type or 'album'}"
        owned = False
        if tl is not None and self.collection is not None:
            # owned by name: the pressing's list is not loaded yet, or it lacks this track
            # (a bonus track of the streaming edition)
            rel = (self.collection.find_release(cand.artist, cand.album_name)
                   or self.collection.find_release(track.artist, cand.album_name))
            if rel is not None:
                owned = True
                pressing = self.collection.tracklist(rel)
                if pressing is not None and pressing.tracks:
                    cov = covers(tl, pressing)
                    if cov >= MERGE_MIN_COVERAGE:
                        tl = merge(tl, pressing)
                        reason = f"{src}+discogs ({cov:.0%} of pressing)"
                    else:
                        reason = f"{src} (owned pressing differs, {cov:.0%} overlap)"
                else:
                    tl.owned = True
                    reason = f"{src} (owned, pressing not loaded yet)"
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

"""The user's Discogs collection: the records that are actually on the shelf.

Two jobs:

  1. A prior for identification. When a fingerprint comes back as a track
     that exists on several albums (the album, a single, three compilations),
     the pressing the user owns is the one on the platter. When Spotify has
     never heard of the record at all, the Discogs tracklist is still enough
     to lock an album and scrobble a side.
  2. Vinyl positions ("A3 · SIDE A") and side boundaries for the screen and
     the timing model.

All network work happens on a background thread: the collection is fetched
at start and refreshed daily, and every release's tracklist is pulled once
(a pressing never changes) and cached under ~/.cache/spindle/discogs/.
Lookups from the engine are memory-only and never block on the API.

Discogs API: personal token, 60 requests/min authenticated.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

from .fingerprint import TrackInfo
from .names import artist_keys, fold, words
from .tracklist import AlbumTracklist, norm_title, side_of, titles_match

logger = logging.getLogger(__name__)

API = "https://api.discogs.com"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "spindle" / "discogs"
COLLECTION_TTL = 24 * 3600
PRICE_TTL = 24 * 3600      # how old a record's marketplace price may get before it is asked again
_UA = "spindle/0.2 +https://github.com/LuKresXD/Spindle"
_MIN_GAP = 1.1          # seconds between API calls (stay well under 60/min)
_RETRY_IN = 600         # seconds before retrying a failed collection refresh


VARIOUS_ID = 194        # Discogs' "Various": nobody, so nothing to look up


def clean_artist(name: str) -> str:
    """"Travis Scott (2)" -> "Travis Scott"; "Future (4), Metro Boomin" -> "Future, Metro
    Boomin"; "Various" -> "Various Artists"."""
    s = re.sub(r"\s*\(\d+\)(?=\s*(?:$|[,&/+;]))", "", name or "").strip()
    return "Various Artists" if s.lower() == "various" else s


def _norm(s: str) -> str:
    """An album title as a comparison key."""
    s = fold(s).replace("$", "s").replace("'", "").replace("’", "")
    s = re.sub(r"\s*\(\d+\)$", "", s)                       # Discogs "Artist (2)" disambiguation
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)
    s = re.sub(r"\b(deluxe|edition|remaster(?:ed)?|expanded|anniversary|version|lp|ep)\b", " ", s)
    s = re.sub(r"\b(feat|ft|featuring)\b.*$", " ", s)
    return words(s)


def _same(a: str, b: str) -> bool:
    """Loose album-title test (one contains the other): "Blond" / "Blonde"."""
    a, b = _norm(a), _norm(b)
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def same_album(a: str, b: str) -> bool:
    """Album titles: exact after normalisation ("Whole Lotta Red " == "Whole Lotta Red")."""
    a, b = _norm(a), _norm(b)
    return bool(a) and a == b


def parse_duration(s: str) -> int | None:
    """"3:45" -> 225, "1:02:03" -> 3723, "" -> None."""
    if not s:
        return None
    parts = s.strip().split(":")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return None
    total = 0
    for n in nums:
        total = total * 60 + n
    return total or None


@dataclass
class Release:
    id: int
    artist: str
    title: str
    year: int = 0
    formats: list = field(default_factory=list)      # ["Vinyl"]
    tracks: list = field(default_factory=list)       # [{"position","title","duration","artist"}]
    loaded: bool = False
    added: str = ""                                   # date it entered the collection (YYYY-MM-DD)
    cover: str = ""                                   # the cover image URL Discogs lists for it
    master_id: int = 0                                # its master release, 0 = none
    released: int | None = None    # ORIGINAL release year: None = not asked yet, 0 = unknown
    price_usd: float | None = None     # the lowest copy for sale on Discogs, USD
    typical_usd: float | None = None   # Discogs' suggested price for a VG+ copy (sales history)
    for_sale: int | None = None        # copies for sale
    priced_at: float = 0.0             # when the prices were last asked (0 = never)
    artists: list = field(default_factory=list)      # the credited names, one each (Discogs' list)
    artist_ids: list = field(default_factory=list)   # their Discogs artist ids, parallel

    @property
    def positions(self) -> list[tuple[str, str]]:
        return [(t.get("position", ""), t.get("title", "")) for t in self.tracks]

    @property
    def vinyl(self) -> bool:
        return not self.formats or any("vinyl" in f.lower() for f in self.formats)

    @property
    def has_sides(self) -> bool:
        return any(side_of(t.get("position", "")) for t in self.tracks)

    def to_dict(self) -> dict:
        return {"id": self.id, "artist": self.artist, "title": self.title, "year": self.year,
                "formats": self.formats, "tracks": self.tracks, "loaded": self.loaded,
                "added": self.added, "cover": self.cover, "master_id": self.master_id,
                "released": self.released, "price_usd": self.price_usd,
                "typical_usd": self.typical_usd, "for_sale": self.for_sale,
                "priced_at": self.priced_at, "artists": self.artists,
                "artist_ids": self.artist_ids}

    @staticmethod
    def from_dict(r: dict) -> Release:
        tracks = r.get("tracks")
        if tracks is None:                              # v1 cache: [(position, title)]
            tracks = [{"position": p, "title": t, "duration": None, "artist": ""}
                      for p, t in r.get("positions", [])]
        released = r.get("released")
        num = lambda v: None if v is None else float(v)  # noqa: E731
        return Release(int(r["id"]), r["artist"], r["title"], int(r.get("year") or 0),
                       list(r.get("formats") or []), tracks, bool(r.get("loaded")),
                       str(r.get("added") or ""), str(r.get("cover") or ""),
                       int(r.get("master_id") or 0),
                       None if released is None else int(released),
                       num(r.get("price_usd")), num(r.get("typical_usd")),
                       None if r.get("for_sale") is None else int(r["for_sale"]),
                       float(r.get("priced_at") or 0),
                       [str(a) for a in r.get("artists") or []],
                       [int(a) for a in r.get("artist_ids") or []])


class DiscogsCollection:
    def __init__(self, token: str, username: str, cache_dir: Path | None = None,
                 session: requests.Session | None = None):
        self.token = token
        self.username = username
        self.cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._details: dict[int, dict] = {}
        # what else an artist is called, by Discogs artist id: name variations ("Kino" for
        # Кино) and aliases ("Kanye West" for Ye). None = asked, Discogs has no such artist.
        self._aka: dict[int, list[str]] = {}
        self._keys: dict[int, set[str]] = {}        # release id -> every key its artists answer to
        self._track_keys: dict[int, set[str]] = {}  # release id -> its tracks' own artists' keys
        self._names: dict[str, list[str]] = {}      # names_for() answers
        self._http = session or requests.Session()
        self._http.headers.update({"User-Agent": _UA,
                                   "Authorization": f"Discogs token={token}"})
        self._releases: list[Release] = []
        self._fetched_at = 0.0
        self._last_call = 0.0
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self.last_error: str | None = None
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Discogs cache dir unavailable (%s)", e)
        self._load_cache()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the background refresh + tracklist prefetch loop."""
        if self._thread:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="discogs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def refresh_now(self) -> None:
        """Ask the background thread to refresh immediately (bot /collection refresh)."""
        with self._lock:
            self._fetched_at = 0.0
        self._wake.set()

    @property
    def ready(self) -> bool:
        return bool(self._releases)

    def stats(self) -> dict:
        with self._lock:
            return {
                "releases": len(self._releases),
                "loaded": sum(1 for r in self._releases if r.loaded),
                "fetched_at": self._fetched_at,
                "error": self.last_error,
            }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._refresh_if_stale()
                self._prefetch_tracklists()
                self._prefetch_artists()
                self._prefetch_masters()
                self._prefetch_prices()
            except Exception as e:  # the thread must survive anything
                logger.warning("Discogs background: %s", e)
            self._wake.wait(timeout=300)
            self._wake.clear()

    # ── sync (blocking) helpers for callers that want them ───────────────────

    def sync(self) -> None:
        """Blocking refresh + prefetch (tests, one-off scripts)."""
        self._refresh_if_stale()
        self._prefetch_tracklists()
        self._prefetch_artists()
        self._prefetch_masters()
        self._prefetch_prices()

    # ── queries (memory only, never block on the network) ────────────────────

    def all_releases(self) -> list[Release]:
        """Every record on the shelf, newest addition first."""
        with self._lock:
            return sorted(self._releases, key=lambda r: r.added, reverse=True)

    def release_by_id(self, rid: int) -> Release | None:
        with self._lock:
            return next((r for r in self._releases if r.id == rid), None)

    def random_release(self, exclude: set[int] | None = None) -> Release | None:
        """One vinyl release drawn uniformly, never one of `exclude` while anything
        else is left (the idle screen's "pick a record for me")."""
        with self._lock:
            pool = [r for r in self._releases if r.vinyl]
        fresh = [r for r in pool if r.id not in (exclude or set())] or pool
        return random.choice(fresh) if fresh else None

    def _artist_keys(self, rel: Release) -> set[str]:
        """Every key this record's artists answer to: the credit as Discogs prints it, each
        credited name, and what Discogs says each is also called."""
        keys = self._keys.get(rel.id)
        if keys is None:
            keys = set(artist_keys(rel.artist))
            for name in rel.artists:
                keys |= artist_keys(name)
            for aid in rel.artist_ids:
                for name in self._aka.get(aid) or []:
                    keys |= artist_keys(name)
            self._keys[rel.id] = keys
        return keys

    def _forget_keys(self) -> None:
        self._keys.clear()
        self._track_keys.clear()
        self._names.clear()

    def names_for(self, artist: str) -> list[str]:
        """Every name the shelf knows this artist by: for each credited artist who answers
        to `artist`, the credited name and what Discogs says they are also called. Empty
        when the artist is on none of the user's records."""
        known = self._names.get(artist)
        if known is not None:
            return known
        want = artist_keys(artist)
        out: list[str] = []
        with self._lock:
            for r in self._releases:
                for name, aid in zip(r.artists, r.artist_ids, strict=False):
                    group = [name, *(self._aka.get(aid) or [])]
                    if name in out or not any(want & artist_keys(n) for n in group):
                        continue
                    out.extend(n for n in group if n and n not in out)
            self._names[artist] = out
        return out

    def releases_for_artist(self, artist: str) -> list[Release]:
        want = artist_keys(artist)
        with self._lock:
            return [r for r in self._releases if want & self._artist_keys(r)]

    def find_release(self, artist: str, album: str) -> Release | None:
        """The owned pressing of an album, exact title first, vinyl first."""
        if not album:
            return None
        cands = self.releases_for_artist(artist)
        exact = [r for r in cands if same_album(r.title, album)]
        loose = exact or [r for r in cands if _same(r.title, album)]
        if not loose:
            return None
        loose.sort(key=lambda r: (not r.vinyl, not r.loaded, not r.has_sides, r.id))
        return loose[0]

    def releases_with_track(self, artist: str, title: str) -> list[Release]:
        """Owned releases (tracklist known) that carry this track by this artist: the record
        is the artist's, or the track on it is (a Various Artists compilation, a guest's
        track on a label sampler). Vinyl first, then the copy that knows its sides."""
        want = artist_keys(artist)
        out = []
        with self._lock:
            for r in self._releases:
                if not r.loaded:
                    continue
                whole = bool(want & self._artist_keys(r))
                if not whole:
                    theirs = self._track_keys.get(r.id)
                    if theirs is None:
                        theirs = self._track_keys[r.id] = {
                            k for t in r.tracks if t.get("artist")
                            for k in artist_keys(t["artist"])}
                    if not want & theirs:
                        continue                # nobody on this record answers to the name
                for t in r.tracks:
                    if not whole and not (t.get("artist") and want & artist_keys(t["artist"])):
                        continue
                    if titles_match(t.get("title", ""), title):
                        out.append(r)
                        break
        out.sort(key=lambda r: (not r.vinyl, not r.has_sides, r.id))
        return out

    def owned_album_titles(self, artist: str) -> list[str]:
        return [r.title for r in self.releases_for_artist(artist)]

    def tracklist(self, rel: Release) -> AlbumTracklist | None:
        """The pressing as an AlbumTracklist (None until its tracklist is loaded)."""
        if not rel.loaded or not rel.tracks:
            return None
        artist = clean_artist(rel.artist)
        album = rel.title.strip()
        tracks, positions = [], []
        for t in rel.tracks:
            tracks.append(TrackInfo(
                title=t.get("title", ""), artist=clean_artist(t.get("artist") or "") or artist,
                album=album, duration=t.get("duration"), source="discogs", confidence=1.0,
            ))
            positions.append(t.get("position", "") or "")
        return AlbumTracklist(
            album_id=f"dg:{rel.id}", album_name=album, artist=artist, tracks=tracks,
            release_date=str(rel.year) if rel.year else "", positions=positions,
            source="discogs", discogs_release_id=rel.id, owned=True,
        )

    def position(self, artist: str, album: str, title: str) -> tuple[str, str]:
        """(side, position) for a track, e.g. ("A", "A3"); ("", "") when unknown."""
        try:
            rel = self.find_release(artist, album)
            if rel is None or not rel.loaded:
                return ("", "")
            want = norm_title(title)
            pos = ""
            for t in rel.tracks:
                if norm_title(t.get("title", "")) == want:
                    pos = t.get("position", "")
                    break
            if not pos:
                for t in rel.tracks:
                    if titles_match(t.get("title", ""), title):
                        pos = t.get("position", "")
                        break
            return (side_of(pos), pos) if pos else ("", "")
        except Exception as e:  # never let Discogs trouble reach the screen
            logger.debug("Discogs position failed: %s", e)
            return ("", "")

    # ── network (background thread) ──────────────────────────────────────────

    def _refresh_if_stale(self) -> None:
        with self._lock:
            fresh = self._releases and time.time() - self._fetched_at < COLLECTION_TTL
        if fresh:
            return
        try:
            releases = self._fetch_collection()
        except requests.RequestException as e:
            self.last_error = str(e)
            logger.warning("Discogs collection refresh failed: %s", e)
            with self._lock:
                self._fetched_at = time.time() - COLLECTION_TTL + _RETRY_IN
            return
        with self._lock:
            self._releases = releases
            self._fetched_at = time.time()
            self.last_error = None
            self._forget_keys()
            self._save_cache()
        logger.info("Discogs: %d releases in collection", len(releases))

    def _fetch_collection(self) -> list[Release]:
        with self._lock:
            by_id = {r.id: r for r in self._releases}
        out: list[Release] = []
        seen: set[int] = set()
        page = 1
        while True:
            data = self._get(f"/users/{self.username}/collection/folders/0/releases",
                             params={"per_page": 100, "page": page})
            for item in data.get("releases", []):
                info = item.get("basic_information", {})
                rid = int(info.get("id", 0))
                if rid in seen:                         # a second copy is still one record
                    continue
                seen.add(rid)
                credited = info.get("artists", []) or []
                artists = ", ".join(a.get("name", "") for a in credited)
                rel = by_id.get(rid) or Release(rid, artists, info.get("title", ""))
                rel.artist, rel.title = artists, info.get("title", rel.title)
                rel.artists = [a.get("name", "") for a in credited]
                rel.artist_ids = [int(a.get("id") or 0) for a in credited]
                rel.year = int(info.get("year") or rel.year or 0)
                rel.formats = [f.get("name", "") for f in info.get("formats", [])] or rel.formats
                rel.added = str(item.get("date_added") or rel.added or "")[:10]
                rel.cover = str(info.get("cover_image") or rel.cover or "")
                rel.master_id = int(info.get("master_id") or rel.master_id or 0)
                if not rel.master_id and rel.released is None:
                    rel.released = rel.year           # no master: this pressing is the original
                out.append(rel)
            pages = data.get("pagination", {}).get("pages", 1)
            if page >= pages:
                break
            page += 1
        return out

    def _prefetch_tracklists(self) -> None:
        """Load every unloaded release, one API call each, rate limited."""
        while not self._stop.is_set():
            with self._lock:
                pending = [r for r in self._releases if not r.loaded]
            if not pending:
                return
            rel = pending[0]
            try:
                self._load_release(rel)
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning("Discogs: tracklist for %s failed: %s", rel.title, e)
                return                                 # network trouble: try again next loop
            if len(pending) == 1:
                logger.info("Discogs: all %d tracklists loaded", len(self._releases))

    def _load_release(self, rel: Release) -> None:
        data = self._get(f"/releases/{rel.id}")
        tracks = []
        for t in data.get("tracklist", []):
            kind = t.get("type_", "track")
            pos = t.get("position", "") or ""
            if kind == "index" and t.get("sub_tracks"):
                for st in t["sub_tracks"]:
                    if st.get("type_", "track") == "track":
                        tracks.append(_track_dict(st, pos))
                continue
            if kind != "track" or not pos:
                continue
            tracks.append(_track_dict(t, pos))
        images = data.get("images") or []
        primary = next((i.get("uri", "") for i in images if i.get("type") == "primary"),
                       images[0].get("uri", "") if images else "")
        with self._lock:
            self._track_keys.pop(rel.id, None)
            rel.tracks = tracks
            rel.loaded = True
            if not rel.year:
                rel.year = int(data.get("year") or 0)
            if not rel.cover and primary:
                rel.cover = primary
            if not rel.master_id:
                rel.master_id = int(data.get("master_id") or 0)
                if not rel.master_id and rel.released is None:
                    rel.released = rel.year
            self._save_cache()
        logger.debug("Discogs: loaded %s — %s (%d tracks)", rel.artist, rel.title, len(tracks))

    def _prefetch_artists(self) -> None:
        """What each artist on the shelf is also called, one call per artist, once: Discogs
        lists name variations (Кино: "Kino", "КИНО") and aliases (Ye: "Kanye West"). A
        fingerprint that names the artist the other way is then still this record's."""
        while not self._stop.is_set():
            with self._lock:
                pending = [aid for r in self._releases for aid in r.artist_ids
                           if aid and aid != VARIOUS_ID and aid not in self._aka]
            if not pending:
                return
            aid = pending[0]
            names: list[str] = []
            try:
                data = self._get(f"/artists/{aid}")
                names = [str(n) for n in data.get("namevariations") or []]
                names += [str(a.get("name", "")) for a in data.get("aliases") or []]
            except requests.HTTPError as e:
                if e.response is None or e.response.status_code != 404:
                    self.last_error = str(e)
                    logger.warning("Discogs: artist %s failed: %s", aid, e)
                    return
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning("Discogs: artist %s failed: %s", aid, e)
                return
            with self._lock:
                self._aka[aid] = [n for n in names if n]
                self._forget_keys()
                self._save_cache()
            if len(set(pending)) == 1:
                logger.info("Discogs: other names known for %d artists", len(self._aka))

    def _prefetch_masters(self) -> None:
        """The original release year lives on the master: one call per record whose
        master is known and has not been asked yet, rate limited like everything else.
        A record with no master id stays unanswered until a collection refresh says
        whether it has one (then it is its own original) - never guessed from the
        pressing year."""
        while not self._stop.is_set():
            with self._lock:
                pending = [r for r in self._releases
                           if r.loaded and r.master_id and r.released is None]
            if not pending:
                return
            rel = pending[0]
            try:
                year = int(self._get(f"/masters/{rel.master_id}").get("year") or 0)
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning("Discogs: master for %s failed: %s", rel.title, e)
                return
            with self._lock:
                rel.released = year or rel.year or 0
                self._save_cache()

    def _prefetch_prices(self) -> None:
        """Every record's marketplace price, asked once a day (two calls each), kept in the
        cache so the shelf can sort by price without a network call."""
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                pending = [r for r in self._releases if r.loaded and now - r.priced_at > PRICE_TTL]
            if not pending:
                return
            rel = pending[0]
            try:
                price, typical, for_sale = self._fetch_prices(rel.id)
            except requests.RequestException as e:
                self.last_error = str(e)
                logger.warning("Discogs: prices for %s failed: %s", rel.title, e)
                return
            with self._lock:
                rel.price_usd, rel.typical_usd, rel.for_sale = price, typical, for_sale
                rel.priced_at = now
                cached = self._details.get(rel.id)
                if cached is not None:
                    cached.update({"price_usd": price, "typical_usd": typical,
                                   "for_sale": for_sale})
                self._save_cache()

    def _fetch_prices(self, rid: int) -> tuple[float | None, float | None, int | None]:
        """The lowest copy for sale and Discogs' VG+ suggestion, in USD. A 404 on the stats
        (nothing ever sold) is not a failure; a network error is raised to the caller."""
        price = None
        for_sale = None
        try:
            stats = self._get(f"/marketplace/stats/{rid}", params={"curr_abbr": "USD"})
            lowest = stats.get("lowest_price") or {}
            if lowest.get("value") is not None:
                price = float(lowest["value"])
            for_sale = stats.get("num_for_sale")
        except requests.HTTPError as e:
            logger.debug("Discogs marketplace stats for %s: %s", rid, e)
        typical = None            # Discogs' suggested price for a VG+ copy (sales-history based)
        try:
            sugg = self._get(f"/marketplace/price_suggestions/{rid}")
            for cond in ("Very Good Plus (VG+)", "Near Mint (NM or M-)", "Very Good (VG)"):
                if isinstance(sugg.get(cond), dict) and sugg[cond].get("value") is not None:
                    typical = float(sugg[cond]["value"])
                    break
        except (requests.HTTPError, ValueError, TypeError) as e:
            logger.debug("Discogs price suggestions for %s unavailable: %s", rid, e)
        return price, typical, for_sale

    def release_details(self, rel: Release) -> dict:
        """Pressing facts for the display's info sheet, one API call per release,
        cached in memory: labels, country, year, formats, notes, credits."""
        with self._lock:
            cached = self._details.get(rel.id)
        if cached is not None:
            return cached
        data = self._get(f"/releases/{rel.id}")
        formats = []
        for f in data.get("formats", []) or []:
            bits = [f.get("name", "")] + list(f.get("descriptions", []) or [])
            qty = f.get("qty")
            prefix = f"{qty} × " if qty and str(qty) != "1" else ""
            formats.append(prefix + ", ".join(b for b in bits if b))
        credits = [{"name": a.get("name", ""), "role": a.get("role", ""),
                    "tracks": a.get("tracks", "")}
                   for a in data.get("extraartists", []) or []]
        by_track: dict[str, list] = {}
        for t in data.get("tracklist", []) or []:
            pos = t.get("position") or ""
            extra = [{"name": a.get("name", ""), "role": a.get("role", "")}
                     for a in t.get("extraartists", []) or []]
            if pos and extra:
                by_track[pos] = extra
        if rel.priced_at and time.time() - rel.priced_at < PRICE_TTL:
            price, typical, for_sale = rel.price_usd, rel.typical_usd, rel.for_sale
        else:
            try:
                price, typical, for_sale = self._fetch_prices(rel.id)
                with self._lock:
                    rel.price_usd, rel.typical_usd, rel.for_sale = price, typical, for_sale
                    rel.priced_at = time.time()
            except requests.RequestException as e:
                logger.debug("Discogs prices for %s failed: %s", rel.id, e)
                price, typical, for_sale = rel.price_usd, rel.typical_usd, rel.for_sale
        details = {
            "id": rel.id,
            "price_usd": price,
            "typical_usd": typical,
            "for_sale": for_sale,
            "labels": [{"name": lb.get("name", ""), "catno": lb.get("catno", "")}
                       for lb in data.get("labels", []) or []],
            "country": data.get("country", "") or "",
            "year": int(data.get("year") or rel.year or 0),
            "released": data.get("released", "") or "",
            "formats": formats,
            "notes": (data.get("notes") or "").strip(),
            "credits": credits,
            "credits_by_track": by_track,
            "added": rel.added,
            "url": data.get("uri", "") or "",
        }
        with self._lock:
            self._details[rel.id] = details
        return details

    def _get(self, path: str, params: dict | None = None) -> dict:
        wait = _MIN_GAP - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        resp = self._http.get(API + path, params=params, timeout=15)
        self._last_call = time.time()
        if resp.status_code == 429:
            time.sleep(5)
            resp = self._http.get(API + path, params=params, timeout=15)
            self._last_call = time.time()
        resp.raise_for_status()
        return resp.json()

    # ── disk cache ───────────────────────────────────────────────────────────

    def _cache_file(self) -> Path:
        return self.cache_dir / f"collection-{self.username}.json"

    def _load_cache(self) -> None:
        try:
            d = json.loads(self._cache_file().read_text())
            self._fetched_at = float(d.get("fetched_at", 0))
            self._releases = [Release.from_dict(r) for r in d.get("releases", [])]
            self._aka = {int(k): list(v) for k, v in (d.get("aka") or {}).items()}
            if any(not r.artist_ids for r in self._releases):
                self._fetched_at = 0.0      # a cache from before artist ids: ask the list again
        except (OSError, ValueError, KeyError):
            pass

    def _save_cache(self) -> None:
        try:
            tmp = self._cache_file().with_suffix(".tmp")
            tmp.write_text(json.dumps({
                "fetched_at": self._fetched_at,
                "releases": [r.to_dict() for r in self._releases],
                "aka": {str(k): v for k, v in self._aka.items()},
            }))
            tmp.replace(self._cache_file())
        except OSError as e:
            logger.debug("Discogs cache write failed: %s", e)


def _track_dict(t: dict, position: str) -> dict:
    artists = t.get("artists") or []
    return {
        "position": t.get("position", "") or position,
        "title": t.get("title", ""),
        "duration": parse_duration(t.get("duration", "")),
        "artist": ", ".join(a.get("name", "") for a in artists) if artists else "",
    }

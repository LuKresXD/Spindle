"""The touch display: Spindle serves its own screen as a local web page.

A kiosk browser on the Pi (Chromium under the cage Wayland compositor) shows
``http://127.0.0.1:8787/``. Nothing in here touches the engine: the tick
loop hands every ``ScreenState`` to ``StateHub.publish`` and the page pulls
the rest on demand.

  GET /            the page (web_static/index.html)
  GET /static/*    its files
  GET /events      Server-Sent Events: the latest state as JSON, pushed on change
  GET /api/state   the same, once
  GET /art/<key>   the cover bytes the state refers to
  GET /api/lyrics  time-synced lyrics from LRCLIB (artist, title, album, duration)
  GET /api/info    the info sheet: pressing (Discogs), credits, story (Wikipedia),
                   plays (Last.fm + the local history)
  GET /api/collection          the shelf: every record, newest first
  GET /api/collection/draw     one record drawn at random (?exclude=id,id): the row only, no network
  GET /api/collection/random   the same draw as a full card (pressing + plays; two network calls)
  GET /api/collection/<id>     one record as a card: pressing, plays, last spin
  GET /api/history/albums      the records played most recently (?limit=12)
  GET /art/collection/<id>.jpg a record's cover, cached on disk; <id>.s.jpg a 320 px copy for
                               tiles, <id>.blur.jpg a 32 px pre-blurred, darkened copy the page
                               stretches to the whole screen as the backdrop (no CSS blur)
  GET /art/<key>.blur.jpg      the same backdrop copy for the cover on screen

Everything that goes to the network is cached per track or per album and
runs on the request thread, never on the tick.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests

from .config import WebConfig
from .discogs import clean_artist
from .state import ScreenState
from .tracklist import side_of, titles_match

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "web_static"
ENDED_CARD_SECONDS = 75.0        # how long the "side finished" card stays after silence
SSE_KEEPALIVE_SECONDS = 15.0
LRCLIB = "https://lrclib.net/api"
WIKIPEDIA = "https://en.wikipedia.org"
USER_AGENT = "Spindle/0.3 (https://github.com/LuKresXD/spindle)"
HTTP_TIMEOUT = 8
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8", ".json": "application/json",
    ".ttf": "font/ttf", ".woff2": "font/woff2", ".png": "image/png", ".jpg": "image/jpeg",
    ".svg": "image/svg+xml", ".ico": "image/x-icon",
}


# ── state hub ────────────────────────────────────────────────────────────────

class StateHub:
    """The latest screen state as a JSON-ready dict, with a version number
    that SSE clients wait on, plus the cover bytes it refers to."""

    def __init__(self, clock: Callable[[], float] = time.time):
        self._cond = threading.Condition()
        self._version = 0
        self._payload: dict = {"phase": "idle"}
        self._art: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
        self._ended: dict | None = None
        self._clock = clock
        self.on_album: Callable[[str, str], None] | None = None   # set by the server
        self.on_track: Callable[[str, str, str, float | None], None] | None = None
        self._album_seen: tuple[str, str] | None = None
        self._track_seen: tuple[str, str] | None = None

    # publishing (tick thread) ------------------------------------------------

    def publish(self, st: ScreenState) -> None:
        payload = state_payload(st, self._clock())
        if st.artist and st.album and self.on_album is not None:
            album_key = (st.artist, st.album)
            if album_key != self._album_seen:
                self._album_seen = album_key
                with contextlib.suppress(Exception):
                    self.on_album(st.artist, st.album)
        if st.artist and st.title and self.on_track is not None:
            track_key = (st.artist, st.title)
            if track_key != self._track_seen:
                self._track_seen = track_key
                with contextlib.suppress(Exception):
                    self.on_track(st.artist, st.title, st.album or "", st.track_duration)
        if st.art:
            key = art_key(st.art)
            with self._cond:
                if key not in self._art:
                    self._art[key] = (st.art, st.art_source)
                    while len(self._art) > 6:
                        self._art.popitem(last=False)
            payload["art"] = key
        with self._cond:
            if self._ended is not None:
                expired = self._clock() - self._ended["at"] > ENDED_CARD_SECONDS
                if expired or st.phase.value != "idle":
                    self._ended = None
                else:
                    payload["ended"] = self._ended
            if payload != self._payload:
                self._payload = payload
                self._version += 1
                self._cond.notify_all()

    def side_ended(self, info: dict) -> None:
        with self._cond:
            self._ended = dict(info)
            self._payload = dict(self._payload, ended=self._ended)
            self._version += 1
            self._cond.notify_all()

    # reading (request threads) ----------------------------------------------

    def snapshot(self) -> tuple[int, dict]:
        with self._cond:
            return self._version, self._payload

    def wait(self, seen: int, timeout: float) -> tuple[int, dict] | None:
        """Block until the version moves past `seen`, or `timeout` passes (None)."""
        with self._cond:
            if self._version == seen:
                self._cond.wait(timeout)
            if self._version == seen:
                return None
            return self._version, self._payload

    def art(self, key: str) -> tuple[bytes, str] | None:
        with self._cond:
            return self._art.get(key)


def art_key(data: bytes) -> str:
    return hashlib.sha1(data[:4096] + len(data).to_bytes(4, "big")).hexdigest()[:12]


def state_payload(st: ScreenState, now: float) -> dict:
    """ScreenState -> plain dict (no bytes, enums as values, server time attached)."""
    d = asdict(st)
    d.pop("art", None)
    d["phase"] = st.phase.value
    d["art"] = None
    d["server_time"] = now
    return d


# ── lyrics ───────────────────────────────────────────────────────────────────

_LRC_TAG = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """'[mm:ss.xx] line' (several tags per line allowed) -> [(seconds, line)] sorted."""
    out: list[tuple[float, str]] = []
    for raw in text.splitlines():
        tags = _LRC_TAG.findall(raw)
        if not tags:
            continue
        line = _LRC_TAG.sub("", raw).strip()
        for m, s in tags:
            out.append((int(m) * 60 + float(s), line))
    out.sort(key=lambda x: x[0])
    return out


class Lyrics:
    """LRCLIB lookups, cached per (artist, title, duration)."""

    def __init__(self, http: requests.Session | None = None):
        self._http = http or requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, artist: str, title: str, album: str = "",
            duration: float | None = None) -> dict:
        key = f"{artist.lower()}|{title.lower()}|{int(duration or 0)}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            result = self._fetch(artist, title, album, duration)
        except (requests.RequestException, ValueError) as e:
            logger.debug("Lyrics lookup failed: %s", e)
            result = {"synced": [], "plain": None, "source": ""}
        with self._lock:
            self._cache[key] = result
        return result

    def _fetch(self, artist: str, title: str, album: str, duration: float | None) -> dict:
        params: dict[str, Any] = {"artist_name": artist, "track_name": title}
        if album:
            params["album_name"] = album
        if duration:
            params["duration"] = int(round(duration))
        hit: dict | None = None
        r = self._http.get(f"{LRCLIB}/get", params=params, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            cand = r.json()
            if cand.get("syncedLyrics"):
                hit = cand
        if hit is None:
            r = self._http.get(f"{LRCLIB}/search",
                               params={"artist_name": artist, "track_name": title},
                               timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            synced = [c for c in r.json() if c.get("syncedLyrics")]
            if synced:
                if duration:
                    synced.sort(key=lambda c: abs(float(c.get("duration") or 0) - duration))
                    if abs(float(synced[0].get("duration") or 0) - duration) > 12:
                        synced = []
                hit = synced[0] if synced else None
        if hit is None:
            return {"synced": [], "plain": None, "source": ""}
        return {
            "synced": [[t, line] for t, line in parse_lrc(hit.get("syncedLyrics") or "")],
            "plain": hit.get("plainLyrics") or None,
            "source": "lrclib",
        }


# ── info sheet ───────────────────────────────────────────────────────────────

class Info:
    """Pressing, credits, story and plays for an album, cached per album."""

    def __init__(self, collection: Any = None, scrobbler: Any = None, history: Any = None,
                 http: requests.Session | None = None):
        self.collection = collection
        self.scrobbler = scrobbler
        self.history = history
        self._http = http or requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self._cache: dict[str, dict] = {}
        self._warming: set[str] = set()
        self._lock = threading.Lock()

    def get(self, artist: str, album: str) -> dict:
        key = f"{artist.lower()}|{album.lower()}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        # the three sources are independent network calls: run them side by side
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="info") as pool:
            futs = {
                "pressing": pool.submit(self._pressing, artist, album),
                "story": pool.submit(self._story, artist, album),
                "plays": pool.submit(self._plays, artist, album),
            }
            result = {k: f.result() for k, f in futs.items()}
        with self._lock:
            self._cache[key] = result
        return result

    def prefetch(self, artist: str, album: str) -> None:
        """Warm the cache off-thread so the sheet opens at once (called when the
        album on screen changes)."""
        key = f"{artist.lower()}|{album.lower()}"
        with self._lock:
            if key in self._cache or key in self._warming:
                return
            self._warming.add(key)

        def run() -> None:
            try:
                self.get(artist, album)
            finally:
                with self._lock:
                    self._warming.discard(key)

        threading.Thread(target=run, name="info-prefetch", daemon=True).start()

    def _pressing(self, artist: str, album: str) -> dict | None:
        if self.collection is None:
            return None
        try:
            rel = self.collection.find_release(artist, album)
            if rel is None:
                return None
            return self.collection.release_details(rel)
        except Exception as e:  # network or shape trouble: the sheet shows less
            logger.debug("Pressing lookup failed: %s", e)
            return None

    def _story(self, artist: str, album: str) -> dict | None:
        try:
            q: dict[str, str | int] = {"action": "query", "list": "search", "format": "json",
                                       "srlimit": 3, "srsearch": f"{album} {artist} album"}
            r = self._http.get(f"{WIKIPEDIA}/w/api.php", params=q, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            hits = r.json().get("query", {}).get("search", [])
            if not hits:
                return None
            title = hits[0]["title"]
            r = self._http.get(f"{WIKIPEDIA}/api/rest_v1/page/summary/"
                               + urllib.parse.quote(title.replace(" ", "_")),
                               timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            page = r.json()
            extract = (page.get("extract") or "").strip()
            if not extract:
                return None
            text = extract if len(extract) <= 1400 else extract[:1397].rsplit(" ", 1)[0] + "…"
            return {"text": text, "title": page.get("title", title),
                    "url": page.get("content_urls", {}).get("desktop", {}).get("page", "")}
        except (requests.RequestException, ValueError, KeyError) as e:
            logger.debug("Story lookup failed: %s", e)
            return None

    def track_plays(self, artist: str, title: str) -> dict:
        """This user's play count and the world listener count for one track,
        cached per track. The title must be exactly as scrobbled (feat. and all)."""
        key = f"track|{artist.lower()}|{title.lower()}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        out: dict[str, Any] = {"plays": None, "listeners": None}
        net = getattr(self.scrobbler, "network", None) if self.scrobbler is not None else None
        if net is not None and artist and title:
            try:
                tr = net.get_track(artist, title)
                pc = tr.get_userplaycount()
                out["plays"] = int(pc) if pc is not None else None
                out["listeners"] = int(tr.get_listener_count())
            except Exception as e:
                logger.debug("Track plays lookup failed: %s", e)
        with self._lock:
            self._cache[key] = out
        return out

    def plays(self, artist: str, album: str) -> dict:
        """Luka's plays of an album (Last.fm) and the last time it was on (the history)."""
        return self._plays(artist, album)

    def _plays(self, artist: str, album: str) -> dict:
        out: dict[str, Any] = {"user_plays": None, "listeners": None, "last_played": None}
        if self.scrobbler is not None:
            try:
                out["user_plays"] = self.scrobbler.album_playcount(artist, album)
                net = getattr(self.scrobbler, "network", None)
                if net is not None:
                    out["listeners"] = int(net.get_album(artist, album).get_listener_count())
            except Exception as e:
                logger.debug("Plays lookup failed: %s", e)
        if self.history is not None:
            try:
                for entry in self.history.recent(count=200, artist=artist):
                    if (entry.album or "").lower() == album.lower():
                        out["last_played"] = entry.timestamp
                        break
            except Exception as e:
                logger.debug("History lookup failed: %s", e)
        return out


# ── Genius: the story behind the track, samples, producers ────────────────────

GENIUS = "https://genius.com/api"
GENIUS_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")
_URL_RE = re.compile(r"https?://\S+")
# Genius hosts fan translations ("… Türkçe Çeviriler", "… Traducciones") whose
# full_title still names the real artist — never let one win the song match.
_TRANSLATION_RE = re.compile(r"genius|[çc]eviri|traduc|translation|перевод", re.I)
# Genius ranks a song under its bare title; searching the full "(feat. X)" title
# surfaces only fan-translation pages. Strip the feature suffix for the search.
_FEAT_RE = re.compile(r"\s*[\(\[]\s*(feat|ft|featuring|with|w/)\b.*$", re.I)


def _dom_text(node: Any) -> str:
    """Flatten Genius's description DOM into text, paragraphs separated by blank lines."""
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""
    inner = "".join(_dom_text(c) for c in node.get("children", []) or [])
    tag = node.get("tag")
    if tag in ("p", "blockquote", "h1", "h2", "h3", "li"):
        return inner + "\n\n"
    if tag == "br":
        return "\n"
    if tag in ("img", "iframe", "script", "style"):
        return ""
    return inner


def _artist_in(name: str, haystack: str) -> bool:
    a = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    h = re.sub(r"[^a-z0-9]+", " ", haystack.lower())
    return bool(a) and a in h


class Genius:
    """Genius's own site JSON (no API key): search, then the song with its
    description and relationships. Cached per (artist, title); misses cached too."""

    def __init__(self, http: requests.Session | None = None):
        self._http = http or requests.Session()
        self._http.headers["User-Agent"] = GENIUS_UA
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, artist: str, title: str) -> dict:
        key = f"{artist.lower()}|{title.lower()}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        try:
            result = self._fetch(artist, title)
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            logger.debug("Genius lookup failed: %s", e)
            result = {}
        with self._lock:
            self._cache[key] = result
        return result

    def _fetch(self, artist: str, title: str) -> dict:
        search_title = _FEAT_RE.sub("", title).strip() or title
        r = self._http.get(f"{GENIUS}/search/multi", params={"q": f"{artist} {search_title}"},
                           timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        song_id = None
        for section in r.json().get("response", {}).get("sections", []):
            if section.get("type") != "song":
                continue
            for hit in section.get("hits", []):
                res = hit.get("result", {})
                primary = (res.get("primary_artist") or {}).get("name", "")
                if _TRANSLATION_RE.search(primary):     # a translation channel, not the artist
                    continue
                if _artist_in(artist, primary) or _artist_in(primary, artist):
                    song_id = res.get("id")
                    break
            break
        if not song_id:
            return {}
        r = self._http.get(f"{GENIUS}/songs/{song_id}", timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        song = r.json().get("response", {}).get("song", {})
        text = _dom_text((song.get("description") or {}).get("dom"))
        text = _URL_RE.sub("", text)
        paras = [pp.strip() for pp in text.split("\n\n") if pp.strip() and pp.strip() != "?"]
        story = "\n\n".join(paras[:3])
        if len(story) > 1200:
            story = story[:1197].rsplit(" ", 1)[0] + "…"
        def _songs(kind: str, n: int) -> list[dict]:
            for rel in song.get("song_relationships", []) or []:
                if rel.get("relationship_type") == kind:
                    out = []
                    for x in rel.get("songs", [])[:n]:
                        out.append({
                            "title": (x.get("title") or "").replace("\xa0", " "),
                            "artist": ((x.get("primary_artist") or {}).get("name") or "")
                            .replace("\xa0", " "),
                            "art": x.get("song_art_image_thumbnail_url") or "",
                        })
                    return out
            return []
        return {
            "id": song_id,
            "story": story,
            "samples": _songs("samples", 4),
            "sampled_in": _songs("sampled_in", 3),
            "producers": [a.get("name", "") for a in song.get("producer_artists", []) or []],
            "writers": [a.get("name", "") for a in song.get("writer_artists", []) or []][:6],
            "release": song.get("release_date_for_display") or "",
            "url": song.get("url", ""),
        }

    def prefetch(self, artist: str, title: str) -> None:
        key = f"{artist.lower()}|{title.lower()}"
        with self._lock:
            if key in self._cache:
                return
        threading.Thread(target=self.get, args=(artist, title), name="genius-prefetch",
                         daemon=True).start()



# ── the shelf: the collection for the idle screen ─────────────────────────────

COLLECTION_ART_DIR = Path.home() / ".cache" / "spindle" / "art" / "collection"
COVER_VARIANTS = {"s": 320, "blur": 32}   # a tile-sized copy, and the backdrop seed
BLUR_SATURATION, BLUR_BRIGHTNESS = 1.15, 0.62   # what the page used to do with a CSS filter


def derive_cover(data: bytes, variant: str) -> bytes:
    """A smaller copy of a cover. "s" is a 320 px JPEG for tiles. "blur" is a 32 px JPEG,
    softened, saturated and darkened: stretched to a 1920 px screen by the GPU it reads
    as a 64 px blur, for free, where a CSS filter cost the Pi a re-render every frame."""
    if variant not in COVER_VARIANTS:
        return data
    try:
        from PIL import Image, ImageEnhance, ImageFilter
    except ImportError:                       # no Pillow: the full cover will do
        return data
    import io
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        size = COVER_VARIANTS[variant]
        im.thumbnail((size, size), Image.Resampling.LANCZOS)
        if variant == "blur":
            im = im.filter(ImageFilter.GaussianBlur(1.2))
            im = ImageEnhance.Color(im).enhance(BLUR_SATURATION)
            im = ImageEnhance.Brightness(im).enhance(BLUR_BRIGHTNESS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=88 if variant == "s" else 80, optimize=True)
        return out.getvalue()
    except Exception as e:                    # a broken image: serve what we have
        logger.debug("Cover variant %s failed: %s", variant, e)
        return data
COVER_WARM_GAP = 0.6             # seconds between cover downloads when warming the cache
COVER_WARM_FIRST_DELAY = 5.0     # let the engine and the collection come up first
COVER_WARM_EVERY = 600.0         # how often the warm-up looks for missing covers


class Shelf:
    """The record collection as the idle screen sees it: the list, a random draw,
    one record's card, the covers (cached on disk) and the recently played records."""

    def __init__(self, collection: Any = None, history: Any = None, info: Info | None = None,
                 http: requests.Session | None = None, art_dir: Path | None = None):
        self.collection = collection
        self.history = history
        self.info = info
        self._http = http or requests.Session()
        self._http.headers["User-Agent"] = USER_AGENT
        self.art_dir = art_dir or COLLECTION_ART_DIR
        self._lock = threading.Lock()
        self._warming = False
        try:
            self.art_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("Collection art dir unavailable (%s)", e)

    # the list ----------------------------------------------------------------

    def releases(self) -> dict:
        if self.collection is None:
            return {"count": 0, "releases": []}
        rows = [self._row(r) for r in self.collection.all_releases()]
        return {"count": len(rows), "releases": rows}

    def _row(self, rel: Any) -> dict:
        typical, lowest = getattr(rel, "typical_usd", None), getattr(rel, "price_usd", None)
        price = typical if typical is not None else lowest
        kind = None if price is None else ("median" if typical is not None else "lowest")
        return {"id": rel.id, "artist": clean_artist(rel.artist), "title": rel.title,
                "year": rel.year or None, "released": rel.released or None,
                "formats": list(rel.formats or []), "added": rel.added,
                "cover": f"/art/collection/{rel.id}.jpg" if rel.cover else None,
                "price": price,
                "price_kind": kind,
                "for_sale": getattr(rel, "for_sale", None)}

    # one record ------------------------------------------------------------

    def draw(self, exclude: set[int] | None = None) -> dict | None:
        """The draw alone, from memory: the wheel can start on the tap while the card's
        network calls (pressing, plays) run behind it."""
        if self.collection is None:
            return None
        rel = self.collection.random_release(exclude)
        return self._row(rel) if rel is not None else None

    def random(self, exclude: set[int] | None = None) -> dict | None:
        if self.collection is None:
            return None
        rel = self.collection.random_release(exclude)
        return self.card(rel) if rel is not None else None

    def one(self, rid: int) -> dict | None:
        if self.collection is None:
            return None
        rel = self.collection.release_by_id(rid)
        return self.card(rel) if rel is not None else None

    def card(self, rel: Any) -> dict:
        """The row plus what the pick screen says about it: the pressing and the plays.
        Two network calls (Discogs, Last.fm), run side by side; either may come back empty."""
        card = self._row(rel)
        artist = card["artist"]
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="shelf") as pool:
            pressing = pool.submit(self._pressing, rel)
            plays = pool.submit(self._plays, artist, rel.title)
            card["pressing"] = pressing.result()
            card["plays"] = plays.result()
        return card

    def _pressing(self, rel: Any) -> dict | None:
        try:
            return self.collection.release_details(rel)
        except Exception as e:                        # the card shows less
            logger.debug("Pressing for the card failed: %s", e)
            return None

    def _plays(self, artist: str, album: str) -> dict | None:
        if self.info is None:
            return None
        try:
            return self.info.plays(artist, album)
        except Exception as e:
            logger.debug("Plays for the card failed: %s", e)
            return None

    # covers ----------------------------------------------------------------

    def cover(self, rid: int, variant: str = "") -> bytes | None:
        """The cover bytes for a record: the disk cache, else one download. A variant
        ("s", "blur") is derived from the full cover once and cached beside it."""
        blob = self.art_dir / (f"{rid}.{variant}.img" if variant else f"{rid}.img")
        with contextlib.suppress(OSError):
            if blob.is_file():
                return blob.read_bytes()
        if variant:
            full = self.cover(rid)
            if full is None:
                return None
            data = derive_cover(full, variant)
            with contextlib.suppress(OSError):
                tmp = blob.with_suffix(".tmp")
                tmp.write_bytes(data)
                tmp.replace(blob)
            return data
        if self.collection is None:
            return None
        rel = self.collection.release_by_id(rid)
        if rel is None or not rel.cover:
            return None
        try:
            r = self._http.get(rel.cover, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
        except requests.RequestException as e:
            logger.debug("Cover download failed for %s: %s", rid, e)
            return None
        data = r.content
        if not data or not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n"):
            return None
        with contextlib.suppress(OSError):
            tmp = blob.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(blob)
        return data

    def warm_covers(self) -> None:
        """Keep every cover on disk: a slow pass over the missing ones, off the request
        threads, repeated every few minutes so records added later (or a collection
        refresh that only just learnt the cover URLs) are picked up too. The shelf and
        the wheel then open with the pictures already there."""
        with self._lock:
            if self._warming:
                return
            self._warming = True

        def run() -> None:
            time.sleep(COVER_WARM_FIRST_DELAY)
            while True:
                try:
                    if self.collection is not None and getattr(self.collection, "ready", False):
                        for rel in self.collection.all_releases():
                            if not rel.cover:
                                continue
                            if not (self.art_dir / f"{rel.id}.img").is_file():
                                self.cover(rel.id)
                                time.sleep(COVER_WARM_GAP)
                            for variant in COVER_VARIANTS:
                                if not (self.art_dir / f"{rel.id}.{variant}.img").is_file():
                                    self.cover(rel.id, variant)
                except Exception as e:                # the loop must survive anything
                    logger.debug("Cover warm-up: %s", e)
                time.sleep(COVER_WARM_EVERY)

        threading.Thread(target=run, name="cover-warm", daemon=True).start()

    # recently played -------------------------------------------------------

    def recent_albums(self, limit: int = 12) -> list[dict]:
        """The records played most recently: the scrobble history walked newest first,
        one entry per album, each with the record on the shelf it matches (if any)."""
        if self.history is None:
            return []
        try:
            entries = self.history.recent(count=600)
        except Exception as e:
            logger.debug("History for the shelf failed: %s", e)
            return []
        out: list[dict] = []
        by_key: dict[str, dict] = {}
        for entry in entries:                          # newest first
            album = (entry.album or "").strip()
            if not album:
                continue
            key = f"{entry.artist.lower()}|{album.lower()}"
            if key in by_key:
                by_key[key]["titles"].add(entry.title.lower())
                continue
            if len(out) >= limit:
                continue
            rel = None
            if self.collection is not None:
                with contextlib.suppress(Exception):
                    rel = self.collection.find_release(entry.artist, album)
            item = {"key": key, "artist": clean_artist(entry.artist), "album": album,
                    "last_played": entry.timestamp, "titles": {entry.title.lower()}, "rel": rel}
            by_key[key] = item
            out.append(item)
        for o in out:
            rel = o.pop("rel")
            titles = o.pop("titles")
            o["tracks"] = len(titles)
            o["release"] = self._row(rel) if rel is not None else None
            o.update(self._played_shape(rel, titles))
            o.pop("key")
        return out

    @staticmethod
    def _played_shape(rel: Any, titles: set[str]) -> dict:
        """How much of the record the played titles cover: the whole record, whole sides,
        or just a count. Matching is the tracklist's own loose title match."""
        tracks = list(getattr(rel, "tracks", None) or []) if rel is not None else []
        if not tracks:
            return {"total": None, "whole": False, "sides": []}
        by_side: dict[str, list[bool]] = {}
        hit = 0
        for t in tracks:
            played = any(titles_match(t.get("title", ""), x) for x in titles)
            hit += played
            by_side.setdefault(side_of(t.get("position", "")) or "?", []).append(played)
        sides = [s for s, flags in by_side.items() if s != "?" and flags and all(flags)]
        return {"total": len(tracks), "whole": hit >= len(tracks), "sides": sorted(sides)}

# ── HTTP ─────────────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):
    server: WebServer  # type: ignore[assignment]
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet: one line per request is noise
        logger.debug("web: " + fmt, *args)

    # helpers ----------------------------------------------------------------

    def _send(self, status: HTTPStatus, body: bytes, ctype: str,
              cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _query(self) -> dict[str, str]:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    # routes -----------------------------------------------------------------

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                self._static("index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/api/state":
                self._json(self.server.hub.snapshot()[1])
            elif path == "/events":
                self._events()
            elif path.startswith("/art/collection/"):
                self._collection_art(path[len("/art/collection/"):])
            elif path.startswith("/art/"):
                self._art(path[len("/art/"):])
            elif path == "/api/collection":
                self._json(self.server.shelf.releases())
            elif path in ("/api/collection/random", "/api/collection/draw"):
                self._collection_random(full=path.endswith("random"))
            elif path.startswith("/api/collection/"):
                self._collection_one(path[len("/api/collection/"):])
            elif path == "/api/history/albums":
                limit = int(self._query().get("limit", "12") or 12)
                self._json(self.server.shelf.recent_albums(max(1, min(limit, 50))))
            elif path == "/api/lyrics":
                self._lyrics()
            elif path == "/api/info":
                self._info()
            elif path == "/api/track":
                self._track()
            elif path == "/healthz":
                self._json({"ok": True})
            else:
                self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # never kill the serving thread
            logger.warning("web: %s %s failed: %s", self.command, path, e)
            with contextlib.suppress(OSError):
                self._json({"error": str(e)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _static(self, name: str) -> None:
        target = (STATIC_DIR / name).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        ctype = CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        cache = "no-cache" if target.suffix in (".html", ".js", ".css") else "max-age=86400"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype, cache)

    def _art(self, key: str) -> None:
        parts = key.split(".")
        hit = self.server.hub.art(parts[0])
        if hit is None:
            self._json({"error": "no such art"}, HTTPStatus.NOT_FOUND)
            return
        data, _source = hit
        if len(parts) == 3 and parts[1] in COVER_VARIANTS:
            data = self.server.cover_variant(parts[0], parts[1], data)
        ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        self._send(HTTPStatus.OK, data, ctype, "max-age=31536000, immutable")

    def _collection_art(self, name: str) -> None:
        parts = name.split(".")                     # "123.jpg" | "123.s.jpg" | "123.blur.jpg"
        variant = parts[1] if len(parts) == 3 and parts[1] in COVER_VARIANTS else ""
        try:
            rid = int(parts[0])
        except ValueError:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        data = self.server.shelf.cover(rid, variant)
        if data is None:
            self._json({"error": "no cover"}, HTTPStatus.NOT_FOUND)
            return
        ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        self._send(HTTPStatus.OK, data, ctype, "max-age=604800")

    def _collection_random(self, full: bool = True) -> None:
        raw = self._query().get("exclude", "")
        exclude = {int(x) for x in raw.split(",") if x.strip().isdigit()}
        card = self.server.shelf.random(exclude) if full else self.server.shelf.draw(exclude)
        if card is None:
            self._json({"error": "the shelf is empty"}, HTTPStatus.NOT_FOUND)
            return
        self._json(card)

    def _collection_one(self, name: str) -> None:
        card = self.server.shelf.one(int(name)) if name.isdigit() else None
        if card is None:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        self._json(card)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        version, payload = self.server.hub.snapshot()
        self.wfile.write(sse(payload))
        self.wfile.flush()
        while not self.server.stopping:
            nxt = self.server.hub.wait(version, SSE_KEEPALIVE_SECONDS)
            if nxt is None:
                self.wfile.write(b": keepalive\n\n")
            else:
                version, payload = nxt
                self.wfile.write(sse(payload))
            self.wfile.flush()

    def _lyrics(self) -> None:
        q = self._query()
        artist, title = q.get("artist", ""), q.get("title", "")
        if not artist or not title:
            self._json({"error": "artist and title required"}, HTTPStatus.BAD_REQUEST)
            return
        dur = q.get("duration")
        self._json(self.server.lyrics.get(artist, title, q.get("album", ""),
                                          float(dur) if dur else None))

    def _track(self) -> None:
        q = self._query()
        artist, title = q.get("artist", ""), q.get("title", "")
        if not artist or not title:
            self._json({"error": "artist and title required"}, HTTPStatus.BAD_REQUEST)
            return
        data = dict(self.server.genius.get(artist, title))
        data["plays"] = self.server.info.track_plays(artist, title)
        self._json(data)

    def _info(self) -> None:
        q = self._query()
        artist, album = q.get("artist", ""), q.get("album", "")
        if not artist or not album:
            self._json({"error": "artist and album required"}, HTTPStatus.BAD_REQUEST)
            return
        self._json(self.server.info.get(artist, album))


def sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


class WebServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, cfg: WebConfig, collection: Any = None, scrobbler: Any = None,
                 history: Any = None, hub: StateHub | None = None):
        super().__init__((cfg.host, cfg.port), _Handler)
        self.cfg = cfg
        self.hub = hub or StateHub()
        self.lyrics = Lyrics()
        self.info = Info(collection, scrobbler, history)
        self.shelf = Shelf(collection, history, self.info)
        self.genius = Genius()
        self.hub.on_album = self.info.prefetch
        self.hub.on_track = self._prefetch_track
        self.stopping = False
        self._thread: threading.Thread | None = None
        self._variants: OrderedDict[str, bytes] = OrderedDict()   # (art key, variant) → bytes
        self._variants_lock = threading.Lock()

    def cover_variant(self, key: str, variant: str, data: bytes) -> bytes:
        """A derived copy of the cover on screen, kept for the last few records."""
        k = f"{key}.{variant}"
        with self._variants_lock:
            if k in self._variants:
                self._variants.move_to_end(k)
                return self._variants[k]
        out = derive_cover(data, variant)
        with self._variants_lock:
            self._variants[k] = out
            while len(self._variants) > 24:
                self._variants.popitem(last=False)
        return out

    def _prefetch_track(self, artist: str, title: str, album: str = "",
                        duration: float | None = None) -> None:
        """Warm everything the page asks for on a track change, so Lyrics and
        Info open instantly: the Genius page, this track's Last.fm plays and
        the synced lyrics (same duration rounding as the page's request)."""
        self.genius.prefetch(artist, title)

        def run() -> None:
            self.info.track_plays(artist, title)
            self.lyrics.get(artist, title, album, float(round(duration)) if duration else None)

        threading.Thread(target=run, name="track-prefetch", daemon=True).start()

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="web", daemon=True)
        self._thread.start()
        self.shelf.warm_covers()

    def stop(self) -> None:
        self.stopping = True
        self.shutdown()
        self.server_close()

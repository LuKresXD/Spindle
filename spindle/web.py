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
from .state import ScreenState

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
        self.on_track: Callable[[str, str], None] | None = None
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
                    self.on_track(st.artist, st.title)
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
        r = self._http.get(f"{GENIUS}/search/multi", params={"q": f"{artist} {title}"},
                           timeout=HTTP_TIMEOUT)
        r.raise_for_status()
        song_id = None
        for section in r.json().get("response", {}).get("sections", []):
            if section.get("type") != "song":
                continue
            for hit in section.get("hits", []):
                res = hit.get("result", {})
                primary = (res.get("primary_artist") or {}).get("name", "")
                if _artist_in(artist, primary) or _artist_in(primary, artist) \
                        or _artist_in(artist, res.get("full_title", "")):
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
        rels = {rel.get("relationship_type"):
                [x.get("full_title", "") for x in rel.get("songs", [])]
                for rel in song.get("song_relationships", []) or []}
        return {
            "id": song_id,
            "story": story,
            "samples": [t.replace("\xa0", " ") for t in rels.get("samples", [])[:4]],
            "sampled_in": [t.replace("\xa0", " ") for t in rels.get("sampled_in", [])[:3]],
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
            elif path.startswith("/art/"):
                self._art(path[len("/art/"):])
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
        hit = self.server.hub.art(key.split(".")[0])
        if hit is None:
            self._json({"error": "no such art"}, HTTPStatus.NOT_FOUND)
            return
        data, _source = hit
        ctype = "image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
        self._send(HTTPStatus.OK, data, ctype, "max-age=31536000, immutable")

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
        self._json(self.server.genius.get(artist, title))

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
        self.genius = Genius()
        self.hub.on_album = self.info.prefetch
        self.hub.on_track = self.genius.prefetch
        self.stopping = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="web", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stopping = True
        self.shutdown()
        self.server_close()

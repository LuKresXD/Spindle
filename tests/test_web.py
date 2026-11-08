import json
import threading
import time
import urllib.request

from spindle.config import WebConfig
from spindle.state import Phase, ScreenState, TrackRow
from spindle.web import Info, Lyrics, StateHub, WebServer, art_key, parse_lrc, sse, state_payload


def _state(phase=Phase.PLAYING, art=b"\xff\xd8\xff" + bytes(600)):
    rows = [TrackRow(title=f"Track {i + 1}", duration=100 + i, position=f"A{i + 1}",
                     scrobbled=i < 1) for i in range(3)]
    return ScreenState(phase=phase, artist="Madvillain", title="Track 2", album="Madvillainy",
                       year="2004", art=art, art_source="test", tracklist=rows, current_index=1,
                       track_elapsed=30, track_duration=101, side="A", position="A2", owned=True,
                       deck_speed=1.02, updated_at=1000.0)


def test_state_payload_is_json_safe_and_drops_bytes():
    d = state_payload(_state(), now=1234.5)
    assert d["phase"] == "playing" and d["art"] is None and d["server_time"] == 1234.5
    assert d["tracklist"][0] == {"title": "Track 1", "duration": 100, "position": "A1",
                                 "scrobbled": True}
    json.dumps(d)


def test_hub_versions_art_and_ended_card():
    clock = {"t": 1000.0}
    hub = StateHub(clock=lambda: clock["t"])
    v0, _ = hub.snapshot()
    st = _state()
    hub.publish(st)
    v1, payload = hub.snapshot()
    assert v1 == v0 + 1 and payload["art"] == art_key(st.art)
    assert hub.art(payload["art"])[0] == st.art
    hub.publish(st)                                               # same state: no new version
    assert hub.snapshot()[0] == v1
    assert hub.wait(v1, timeout=0.01) is None
    hub.side_ended({"artist": "Madvillain", "album": "Madvillainy", "side": "A", "scrobbled": 3,
                    "next_side": "B", "next_title": "Track 4", "at": clock["t"]})
    v2, payload = hub.snapshot()
    assert v2 == v1 + 1 and payload["ended"]["next_side"] == "B"
    hub.publish(_state(phase=Phase.IDLE, art=None))             # idle keeps the card ...
    assert hub.snapshot()[1]["ended"]["scrobbled"] == 3
    clock["t"] += 200                                             # ... until it expires
    hub.publish(_state(phase=Phase.IDLE, art=None))
    assert "ended" not in hub.snapshot()[1]


def test_parse_lrc_handles_multiple_tags_and_sorts():
    text = ("[00:12.50]Now that that don't kill me\n[00:15.00][01:20.00]Can only make me stronger"
            "\nno tag\n[00:01]late?")
    out = parse_lrc(text)
    assert out[0] == (1.0, "late?")
    assert out[1] == (12.5, "Now that that don't kill me")
    assert out[2] == (15.0, "Can only make me stronger") and out[3][0] == 80.0


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data = status, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Http:
    """Scripted responses keyed by URL suffix."""

    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp(params) if callable(resp) else resp
        return _Resp(404, {})


def test_lyrics_prefers_exact_then_search_by_duration():
    http = _Http({
        "/get": _Resp(200, {"syncedLyrics": None, "plainLyrics": "plain only"}),
        "/search": _Resp(200, [
            {"duration": 250, "syncedLyrics": "[00:01.00]far"},
            {"duration": 203, "syncedLyrics": "[00:02.00]close", "plainLyrics": "close plain"},
        ]),
    })
    ly = Lyrics(http=http)
    out = ly.get("Kanye West", "Good Life", "Graduation", 207)
    assert out["synced"] == [[2.0, "close"]] and out["plain"] == "close plain"
    assert ly.get("Kanye West", "Good Life", "Graduation", 207) is out    # cached
    assert len(http.calls) == 2


class _Collection:
    def find_release(self, artist, album):
        return "rel"

    def release_details(self, rel):
        return {"labels": [{"name": "Not On Label", "catno": ""}], "country": "Europe",
                "year": 2019,
                "formats": ["2 × Vinyl, LP, Album"], "credits": [], "credits_by_track": {},
                "added": "2026-11-06", "notes": "", "url": ""}


class _Scrobbler:
    network = None

    def album_playcount(self, artist, album):
        return 158


def test_info_assembles_pressing_story_and_plays():
    http = _Http({
        "/w/api.php": _Resp(200, {"query": {"search": [{"title": "Graduation (album)"}]}}),
        "/page/summary/Graduation_%28album%29": _Resp(200, {
            "title": "Graduation (album)",
            "extract": ("Graduation is the third studio album by Kanye West. It was released "
                        "in 2007. It sold a lot. Fourth sentence."),
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Graduation_(album)"}}}),
    })
    info = Info(collection=_Collection(), scrobbler=_Scrobbler(), history=None, http=http)
    out = info.get("Kanye West", "Graduation")
    assert out["pressing"]["country"] == "Europe" and out["pressing"]["added"] == "2026-11-06"
    assert out["story"]["text"].startswith("Graduation is the third")
    assert out["story"]["text"].endswith("Fourth sentence.")           # the whole summary
    assert out["plays"]["user_plays"] == 158


def test_server_serves_page_state_art_and_events():
    srv = WebServer(WebConfig(host="127.0.0.1", port=0))
    srv.start()
    port = srv.server_address[1]
    try:
        st = _state()
        srv.hub.publish(st)
        base = f"http://127.0.0.1:{port}"
        html = urllib.request.urlopen(f"{base}/").read().decode()
        assert "<main id=\"home\"" in html
        state = json.loads(urllib.request.urlopen(f"{base}/api/state").read())
        assert state["title"] == "Track 2" and state["art"] == art_key(st.art)
        art = urllib.request.urlopen(f"{base}/art/{state['art']}.jpg")
        assert art.headers["Content-Type"] == "image/jpeg" and art.read() == st.art
        js = urllib.request.urlopen(f"{base}/static/app.js")
        assert js.headers["Content-Type"].startswith("application/javascript")
        with urllib.request.urlopen(f"{base}/events") as es:
            first = es.readline()
            assert first.startswith(b"data: ")
            assert json.loads(first[6:])["phase"] == "playing"
            assert es.readline() == b"\n"
            later = _state(phase=Phase.LISTENING, art=None)
            threading.Timer(0.2, lambda: srv.hub.publish(later)).start()
            nxt = es.readline()
            assert json.loads(nxt[6:])["phase"] == "listening"
        try:
            urllib.request.urlopen(f"{base}/static/../web.py")
        except urllib.error.HTTPError as e:
            assert e.code == 404
        else:
            raise AssertionError("path escape served")
    finally:
        srv.stop()


def test_sse_frame():
    assert sse({"a": 1}) == b'data: {"a": 1}\n\n'
    time.sleep(0)


def test_genius_parses_story_samples_and_producers():
    from spindle.web import Genius
    http = _Http({
        "/search/multi": _Resp(200, {"response": {"sections": [
            {"type": "top_hit", "hits": []},
            {"type": "song", "hits": [{"result": {
                "id": 525, "full_title": "Stronger by Kanye West",
                "primary_artist": {"name": "Kanye West"}}}]}]}}),
        "/songs/525": _Resp(200, {"response": {"song": {
            "id": 525, "url": "https://genius.com/Kanye-west-stronger-lyrics",
            "release_date_for_display": "July 31, 2007",
            "description": {"dom": {"tag": "root", "children": [
                {"tag": "p", "children": ["The biggest hit from ",
                                          {"tag": "em", "children": ["Graduation"]},
                                          " samples Daft Punk. https://youtu.be/x"]},
                {"tag": "p", "children": ["Second paragraph."]}]}},
            "producer_artists": [{"name": "Kanye West"}, {"name": "MIKE DEAN"}],
            "writer_artists": [{"name": "Kanye West"}],
            "song_relationships": [
                {"relationship_type": "samples",
                 "songs": [{"full_title": "Harder Better Faster Stronger by\u00a0Daft\u00a0Punk",
                            "title": "Harder Better Faster Stronger",
                            "primary_artist": {"name": "Daft\u00a0Punk"},
                            "song_art_image_thumbnail_url": "https://img/hbfs.jpg"}]},
                {"relationship_type": "sampled_in", "songs": []}]}}}),
    })
    g = Genius(http=http)
    out = g.get("Kanye West", "Stronger")
    assert out["story"] == "The biggest hit from Graduation samples Daft Punk.\n\nSecond paragraph."
    assert out["samples"] == [{"title": "Harder Better Faster Stronger", "artist": "Daft Punk",
                               "art": "https://img/hbfs.jpg"}]
    assert out["producers"] == ["Kanye West", "MIKE DEAN"] and out["release"] == "July 31, 2007"
    assert g.get("Kanye West", "Stronger") is out


# ── the shelf ─────────────────────────────────────────────────────────────────

class _Rel:
    def __init__(self, rid, artist, title, added, cover="https://img/x.jpg", released=None,
                 year=2019, formats=("Vinyl",)):
        self.id, self.artist, self.title, self.added = rid, artist, title, added
        self.cover, self.released, self.year, self.formats = cover, released, year, list(formats)


class _Shelf:
    ready = True

    def __init__(self):
        self.rels = [_Rel(1, "Massive Attack", "Mezzanine", "2026-02-26", released=1998),
                     _Rel(2, "Kanye West (2)", "Graduation", "2026-11-06", released=2007),
                     _Rel(3, "Кино", "45", "2026-10-14", cover="")]

    def all_releases(self):
        return sorted(self.rels, key=lambda r: r.added, reverse=True)

    def release_by_id(self, rid):
        return next((r for r in self.rels if r.id == rid), None)

    def random_release(self, exclude=None):
        return next(r for r in self.rels if r.id not in (exclude or set()))

    def release_details(self, rel):
        return {"formats": ["2 × Vinyl, LP, Album"], "year": rel.year, "country": "Europe"}

    def find_release(self, artist, album):
        return next((r for r in self.rels if r.title.lower() == album.lower()), None)


class _Entry:
    def __init__(self, artist, album, ts):
        self.artist, self.album, self.timestamp = artist, album, ts


class _History:
    def recent(self, count=10, artist=None):
        return [_Entry("Kanye West", "Graduation", 300), _Entry("Kanye West", "Graduation", 240),
                _Entry("Pendulum", "Hold Your Colour", 100), _Entry("X", "", 50)]


def test_shelf_lists_draws_and_serves_cards(tmp_path):
    from spindle.web import Shelf
    http = _Http({"/x.jpg": _Resp(200, None)})
    http.routes["/x.jpg"].content = b"\xff\xd8\xff" + bytes(40)
    info = Info(collection=_Collection(), scrobbler=_Scrobbler(), history=None, http=_Http({}))
    shelf = Shelf(_Shelf(), _History(), info, http=http, art_dir=tmp_path)
    listing = shelf.releases()
    assert listing["count"] == 3 and [r["id"] for r in listing["releases"]] == [2, 3, 1]
    row = listing["releases"][0]
    assert row["artist"] == "Kanye West" and row["released"] == 2007   # "(2)" stripped, master year
    assert row["cover"] == "/art/collection/2.jpg" and listing["releases"][1]["cover"] is None
    card = shelf.random({1})
    assert card["id"] == 2 and card["pressing"]["country"] == "Europe"
    assert card["plays"]["user_plays"] == 158
    assert shelf.one(1)["title"] == "Mezzanine" and shelf.one(9) is None
    # covers: downloaded once, then the disk copy
    assert shelf.cover(1) == http.routes["/x.jpg"].content and (tmp_path / "1.img").is_file()
    assert shelf.cover(1) is not None and len(http.calls) == 1
    assert shelf.cover(3) is None                                       # no cover listed
    recent = shelf.recent_albums(limit=5)
    assert [r["album"] for r in recent] == ["Graduation", "Hold Your Colour"]
    assert recent[0]["tracks"] == 2 and recent[0]["release"]["id"] == 2
    assert recent[1]["release"] is None and "key" not in recent[0]


def test_server_routes_for_the_shelf(tmp_path):
    srv = WebServer(WebConfig(host="127.0.0.1", port=0), collection=_Shelf(), history=_History())
    srv.shelf.art_dir = tmp_path
    (tmp_path / "2.img").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(20))
    srv.start()
    port = srv.server_address[1]
    try:
        base = f"http://127.0.0.1:{port}"
        d = json.loads(urllib.request.urlopen(f"{base}/api/collection").read())
        assert d["count"] == 3 and d["releases"][0]["title"] == "Graduation"
        card = json.loads(urllib.request.urlopen(f"{base}/api/collection/random?exclude=2,1")
                          .read())
        assert card["id"] == 3 and card["pressing"]["year"] == 2019
        one = json.loads(urllib.request.urlopen(f"{base}/api/collection/1").read())
        assert one["title"] == "Mezzanine"
        art = urllib.request.urlopen(f"{base}/art/collection/2.jpg")
        assert art.headers["Content-Type"] == "image/png" and art.read()[:4] == b"\x89PNG"
        hist = json.loads(urllib.request.urlopen(f"{base}/api/history/albums?limit=1").read())
        assert len(hist) == 1 and hist[0]["album"] == "Graduation"
        for bad in ("/api/collection/77", "/art/collection/nope.jpg"):
            try:
                urllib.request.urlopen(base + bad)
            except urllib.error.HTTPError as e:
                assert e.code == 404
            else:
                raise AssertionError(bad)
    finally:
        srv.stop()

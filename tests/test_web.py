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
                                 "scrobbled": True, "base": "", "featured": [], "version": ""}
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
    def __init__(self, artist, album, ts, title="Track"):
        self.artist, self.album, self.timestamp, self.title = artist, album, ts, title


class _History:
    def recent(self, count=10, artist=None):
        return [_Entry("Kanye West", "Graduation", 300, "Champion"),
                _Entry("Kanye West", "Graduation", 240, "Good Morning"),
                _Entry("Pendulum", "Hold Your Colour", 100, "Slam"), _Entry("X", "", 50)]


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
        draw = json.loads(urllib.request.urlopen(f"{base}/api/collection/draw?exclude=2,1").read())
        assert draw["id"] == 3 and "pressing" not in draw and draw["cover"] is None
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


def test_cover_variants_are_smaller_and_served(tmp_path):
    import io

    from PIL import Image

    from spindle.web import Shelf, derive_cover
    buf = io.BytesIO()
    Image.new("RGB", (600, 600), (200, 40, 40)).save(buf, "JPEG")
    full = buf.getvalue()
    small, blur = derive_cover(full, "s"), derive_cover(full, "blur")
    assert Image.open(io.BytesIO(small)).size == (320, 320)
    assert Image.open(io.BytesIO(blur)).size == (32, 32) and len(blur) < len(small) < len(full)
    assert derive_cover(full, "nope") == full
    shelf = Shelf(_Shelf(), None, None, http=_Http({}), art_dir=tmp_path)
    (tmp_path / "1.img").write_bytes(full)
    assert shelf.cover(1, "blur") == blur and (tmp_path / "1.blur.img").is_file()
    assert shelf.cover(1, "s") == small
    srv = WebServer(WebConfig(host="127.0.0.1", port=0), collection=_Shelf())
    srv.shelf.art_dir = tmp_path
    srv.start()
    try:
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        r = urllib.request.urlopen(f"{base}/art/collection/1.blur.jpg")
        assert r.headers["Content-Type"] == "image/jpeg"
        assert Image.open(io.BytesIO(r.read())).size == (32, 32)
        st = _state(art=full)
        srv.hub.publish(st)
        key = json.loads(urllib.request.urlopen(f"{base}/api/state").read())["art"]
        b = urllib.request.urlopen(f"{base}/art/{key}.blur.jpg").read()
        assert Image.open(io.BytesIO(b)).size == (32, 32)
        assert urllib.request.urlopen(f"{base}/art/{key}.jpg").read() == full
    finally:
        srv.stop()


def test_shelf_rows_carry_prices_and_recent_says_how_much_was_played(tmp_path):
    from spindle.web import Shelf
    coll = _Shelf()
    coll.rels[0].typical_usd, coll.rels[0].price_usd, coll.rels[0].for_sale = 61.0, 42.5, 7
    coll.rels[1].price_usd = 30.0
    coll.rels[1].tracks = [{"position": "A1", "title": "Good Morning"},
                           {"position": "A2", "title": "Champion"},
                           {"position": "B1", "title": "I Wonder"}]

    class _Hist:
        def recent(self, count=10, artist=None):
            return [_Entry2("Kanye West", "Graduation", "Champion", 300),
                    _Entry2("Kanye West", "Graduation", "Good Morning", 240),
                    _Entry2("Pendulum", "Hold Your Colour", "Slam", 100)]
    shelf = Shelf(coll, _Hist(), None, http=_Http({}), art_dir=tmp_path)
    rows = {r["id"]: r for r in shelf.releases()["releases"]}
    assert rows[1]["price"] == 61.0 and rows[1]["price_kind"] == "median"
    assert rows[1]["for_sale"] == 7
    assert rows[2]["price"] == 30.0 and rows[2]["price_kind"] == "lowest"
    assert rows[3]["price"] is None and rows[3]["price_kind"] is None
    recent = shelf.recent_albums()
    grad = recent[0]
    assert grad["tracks"] == 2 and grad["total"] == 3
    assert grad["whole"] is False and grad["sides"] == ["A"]
    assert recent[1]["total"] is None and recent[1]["sides"] == []


class _Entry2:
    def __init__(self, artist, album, title, ts):
        self.artist, self.album, self.title, self.timestamp = artist, album, title, ts


def test_hub_queues_remote_commands_and_numbers_them():
    hub = StateHub(clock=lambda: 1000.0)
    v0, _ = hub.snapshot()
    assert hub.ui_seq() == 0 and hub.ui_since(0) == []
    assert hub.ui("view/lyrics") == 1 and hub.ui("page/off") == 2
    assert hub.ui_since(0) == [(1, "view/lyrics"), (2, "page/off")]
    assert hub.ui_since(1) == [(2, "page/off")]
    v1, _ = hub.snapshot()
    assert v1 > v0                                   # a command wakes the event streams


def test_remote_endpoint_needs_the_key_and_rides_the_event_stream():
    srv = WebServer(WebConfig(host="127.0.0.1", port=0, remote_key="s3cret"))
    srv.start()
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        srv.hub.publish(_state())
        for url in (f"{base}/api/ui/view/lyrics", f"{base}/api/ui/view/lyrics?k=wrong",
                    f"{base}/api/debug/pulse"):
            try:
                urllib.request.urlopen(url)
            except urllib.error.HTTPError as e:
                assert e.code == 403
            else:
                raise AssertionError("no key must be refused: " + url)
        try:
            urllib.request.urlopen(f"{base}/api/ui/dance?k=s3cret")
        except urllib.error.HTTPError as e:
            assert e.code == 400 and "commands" in e.read().decode()
        with urllib.request.urlopen(f"{base}/events") as es:
            assert es.readline().startswith(b"data: ") and es.readline() == b"\n"
            req = urllib.request.Request(f"{base}/api/ui/view/lyrics?k=s3cret", method="POST")
            threading.Timer(0.2, lambda: urllib.request.urlopen(req).read()).start()
            assert es.readline() == b"event: ui\n"
            frame = json.loads(es.readline()[6:])
            assert frame["command"] == "view/lyrics" and frame["seq"] == 1
        req2 = urllib.request.Request(f"{base}/api/ui/page/off",
                                      headers={"X-Spindle-Key": "s3cret"})
        r = json.loads(urllib.request.urlopen(req2).read())
        assert r["ok"] and r["command"] == "page/off" and r["phase"] == "playing"
        assert r["now"] == "Madvillain – Track 2"
        # a stream opened now does not replay the two commands already sent
        with urllib.request.urlopen(f"{base}/events") as es2:
            es2.readline()
            es2.readline()
            threading.Timer(0.2, lambda: srv.hub.ui("back")).start()
            assert es2.readline() == b"event: ui\n"
            assert json.loads(es2.readline()[6:])["command"] == "back"
    finally:
        srv.stop()


def test_remote_is_off_without_a_key():
    srv = WebServer(WebConfig(host="127.0.0.1", port=0))
    srv.start()
    port = srv.server_address[1]
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ui/back?k=anything")
        except urllib.error.HTTPError as e:
            assert e.code == 403 and "not configured" in e.read().decode()
        else:
            raise AssertionError("must be off")
    finally:
        srv.stop()


# ── 2026-11-09 review ────────────────────────────────────────────────────────────────────────────

def test_the_story_is_only_taken_from_a_page_about_this_album_by_this_artist():
    """The first search hit used to be shown as it came: "Graduation (Deluxe Edition)" got the
    article about another Kanye West album."""
    http = _Http({
        "/w/api.php": _Resp(200, {"query": {"search": [
            {"title": "Bully (album)"}, {"title": "Graduation (band)"},
            {"title": "Graduation (album)"}]}}),
        "/page/summary/Graduation_%28band%29": _Resp(200, {
            "title": "Graduation (band)", "description": "Danish rock band",
            "extract": "Graduation is a rock band from Aarhus."}),
        "/page/summary/Graduation_%28album%29": _Resp(200, {
            "title": "Graduation (album)", "description": "2007 studio album by Kanye West",
            "extract": "Graduation is the third studio album by the American rapper."}),
    })
    info = Info(http=http)
    out = info._story("Kanye West", "Graduation (Deluxe Edition)")
    assert out["title"] == "Graduation (album)"
    asked = [u for u, _ in http.calls if "/page/summary/" in u]
    assert not any("Bully" in u for u in asked)                    # wrong title: not even fetched
    assert info._story("Kanye West", "Graduation (Deluxe Edition)") is out      # kept


def test_no_story_is_better_than_another_albums_story():
    http = _Http({
        "/w/api.php": _Resp(200, {"query": {"search": [{"title": "Bully (album)"}]}}),
    })
    assert Info(http=http)._story("Kanye West", "Graduation") is None


def test_a_failed_lookup_is_asked_again_and_play_counts_expire(monkeypatch):
    from spindle import web as web_mod
    now = {"t": 1000.0}
    monkeypatch.setattr(web_mod.time, "time", lambda: now["t"])

    class Flaky:
        headers: dict = {}
        fail = True

        def get(self, url, params=None, timeout=None):
            if self.fail:
                import requests
                raise requests.ConnectionError("down")
            if url.endswith("/get"):
                return _Resp(200, {"syncedLyrics": "[00:01.00]line"})
            return _Resp(200, [])

    http = Flaky()
    ly = Lyrics(http=http)
    assert ly.get("A", "B", "", 200)["synced"] == []               # the network was down
    http.fail = False
    assert ly.get("A", "B", "", 200)["synced"] == []               # ...not asked for 2 minutes
    now["t"] += web_mod.FAILED_TTL + 1
    assert ly.get("A", "B", "", 200)["synced"] == [[1.0, "line"]]  # then it is

    class Counting:
        network = None
        n = 157

        def album_playcount(self, artist, album):
            self.n += 1
            return self.n

    nothing = _Http({"/w/api.php": _Resp(200, {"query": {"search": []}})})
    info = Info(scrobbler=Counting(), http=nothing)
    assert info.get("Kanye West", "Graduation")["plays"]["user_plays"] == 158
    assert info.get("Kanye West", "Graduation")["plays"]["user_plays"] == 158   # within ten minutes
    now["t"] += web_mod.COUNTS_TTL + 1
    assert info.get("Kanye West", "Graduation")["plays"]["user_plays"] == 159   # the count moved


def test_recently_played_describes_the_last_sitting_only(tmp_path):
    """Side A a month ago plus side B today read as "Whole record"."""
    from spindle.web import Shelf

    class Rel:
        id, artist, title, year, released = 1, "X", "Rec", 2000, 2000
        formats, added, cover = ["Vinyl"], "", ""
        tracks = [{"position": "A1", "title": "a1"}, {"position": "A2", "title": "a2"},
                  {"position": "B1", "title": "b1"}, {"position": "B2", "title": "b2"}]
        typical_usd = price_usd = for_sale = None

    class Col:
        def find_release(self, artist, album):
            return Rel()

    class E:
        def __init__(self, title, ts):
            self.artist, self.album, self.title, self.timestamp = "X", "Rec", title, ts

    class Hist:
        def recent(self, count=10, artist=None):
            now = 2_000_000_000
            return [E("b2", now), E("b1", now - 200),                       # today: side B
                    E("a2", now - 30 * 86400), E("a1", now - 30 * 86400 - 200)]   # last month: A

    out = Shelf(collection=Col(), history=Hist(), art_dir=tmp_path).recent_albums()
    assert out[0]["whole"] is False and out[0]["sides"] == ["B"] and out[0]["tracks"] == 2


def test_a_day_a_whole_shelf_was_entered_on_is_marked_as_an_import(tmp_path):
    """The page once knew one such day by heart (2025-04-17, the author's own); now any shelf's
    import day is found from the data, and a day with a few new records keeps its date."""
    from spindle.web import BULK_DAY, Shelf
    stock = _Shelf()
    stock.rels = ([_Rel(100 + i, "Artist", f"Album {i}", "2025-04-17") for i in range(BULK_DAY)]
                  + [_Rel(1, "Massive Attack", "Mezzanine", "2026-02-26"),
                     _Rel(2, "Kanye West", "Graduation", "2026-02-26"),
                     _Rel(3, "Кино", "45", "")])
    info = Info(collection=_Collection(), scrobbler=_Scrobbler(), history=None, http=_Http({}))
    shelf = Shelf(stock, _History(), info, http=_Http({}), art_dir=tmp_path)
    rows = {r["id"]: r for r in shelf.releases()["releases"]}
    assert all(rows[100 + i]["added_bulk"] for i in range(BULK_DAY))
    assert not rows[1]["added_bulk"] and not rows[2]["added_bulk"] and not rows[3]["added_bulk"]

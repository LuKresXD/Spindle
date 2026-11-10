"""The second catalogue: Deezer's answers in the resolver's shapes, and the routing."""
import time

from spindle.deezer import Catalogue, DeezerClient
from spindle.spotify import SpotifyCandidate

ALBUM = {
    "id": 707965, "title": "My Beautiful Dark Twisted Fantasy", "record_type": "album",
    "nb_tracks": 3, "release_date": "2010-11-22", "artist": {"name": "Kanye West"},
    "tracks": {"data": [
        {"title": "Blame Game", "duration": 470, "artist": {"name": "Kanye West"}},
        {"title": "Lost In The World", "duration": 257, "artist": {"name": "Kanye West"}},
        {"title": "Who Will Survive In America", "duration": 98, "artist": {"name": "Kanye West"}},
    ]},
}
SEARCH = {"data": [
    {"title": "Lost In The World", "duration": 257, "artist": {"name": "Kanye West"},
     "album": {"id": 707965, "title": "My Beautiful Dark Twisted Fantasy", "cover_xl": "http://c/x"}},
    {"title": "Lost In the World (Kanye West Cover)", "duration": 200,
     "artist": {"name": "The District"}, "album": {"id": 1, "title": "Live Sessions"}},
]}


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(str(self.status_code))

    def json(self):
        return self._data


class Http:
    def __init__(self, routes):
        self.routes, self.calls, self.headers = routes, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append(url)
        for frag, data in self.routes.items():
            if frag in url:
                return data if isinstance(data, Resp) else Resp(data)
        return Resp({}, 404)


def test_search_keeps_only_the_right_song_and_reads_the_album_for_its_type():
    http = Http({"/search": SEARCH, "/album/707965": ALBUM})
    dz = DeezerClient(session=http)
    cands = dz.search_tracks("Kanye West", "Lost In The World (feat. Bon Iver)")
    assert [c.name for c in cands] == ["Lost In The World"]          # the cover is filtered out
    c = cands[0]
    assert c.album_id == "dz:707965" and c.album_type == "album" and c.total_tracks == 3
    assert c.duration == 257 and c.provider == "deezer" and c.to_track().source == "deezer_lookup"
    n = len(http.calls)
    assert dz.search_tracks("Kanye West", "Lost In The World (feat. Bon Iver)") == cands
    assert len(http.calls) == n                                      # cached


def test_album_tracklist_in_play_order_with_lengths():
    dz = DeezerClient(session=Http({"/album/707965": ALBUM}))
    tl = dz.get_album_tracklist("dz:707965")
    assert tl.album_id == "dz:707965" and tl.artist == "Kanye West" and tl.year == "2010"
    assert [(t.title, t.duration) for t in tl.tracks][1] == ("Lost In The World", 257)
    assert tl.source == "deezer" and not tl.owned


def test_a_quota_error_inside_a_200_backs_off_and_is_not_cached():
    quota = {"error": {"type": "Exception", "message": "Quota limit exceeded", "code": 4}}
    http = Http({"/search": quota})
    dz = DeezerClient(session=http)
    assert dz.search_tracks("A", "B") == [] and not dz.available
    assert dz.search_tracks("A", "B") == [] and len(http.calls) == 1    # backing off, not cached


class FakeSpotify:
    def __init__(self, cands, available=True):
        self.cands, self.available, self.asked = cands, available, 0

    def search_tracks(self, artist, title):
        self.asked += 1
        return self.cands

    def get_album_tracklist(self, album_id):
        return f"spotify:{album_id}"


def test_the_catalogue_asks_spotify_first_and_deezer_when_it_cannot_answer():
    dz = DeezerClient(session=Http({"/search": SEARCH, "/album/707965": ALBUM}))
    own = [SpotifyCandidate(name="Lost In The World", artists=["Kanye West"], album_id="sp1")]
    assert Catalogue(FakeSpotify(own), dz).search_tracks("Kanye West", "Lost In The World") == own
    # rate limited for a day: not even asked, Deezer answers
    limited = FakeSpotify(own, available=False)
    got = Catalogue(limited, dz).search_tracks("Kanye West", "Lost In The World")
    assert limited.asked == 0 and got[0].provider == "deezer"
    # available but empty-handed: Deezer too
    empty = Catalogue(FakeSpotify([]), dz).search_tracks("Kanye West", "Lost In The World")
    assert empty[0].provider == "deezer"
    # album ids route by prefix
    cat = Catalogue(FakeSpotify(own), dz)
    assert cat.get_album_tracklist("abc") == "spotify:abc"
    assert cat.get_album_tracklist("dz:707965").album_name == "My Beautiful Dark Twisted Fantasy"
    assert Catalogue(None, None).search_tracks("a", "b") == []


def test_spotify_reports_unavailable_while_its_retry_after_runs():
    from spindle.config import SpotifyConfig
    from spindle.spotify import SpotifyClient
    sp = SpotifyClient(SpotifyConfig(client_id="i", client_secret="s"))
    assert sp.available
    sp._backoff_until = time.time() + 80000
    assert not sp.available

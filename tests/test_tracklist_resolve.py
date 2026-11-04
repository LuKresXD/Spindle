from conftest import make_album

from spindle.discogs import Release, clean_artist, parse_duration, same_album
from spindle.fingerprint import TrackInfo
from spindle.resolve import ResolveContext, Resolver
from spindle.spotify import SpotifyCandidate, artist_matches
from spindle.tracklist import AlbumTracklist, covers, merge, norm_title, side_of, titles_match


def test_title_matching_is_case_and_punctuation_tolerant():
    assert titles_match("The Black Seminole.", "the BLACK seminole.")
    assert titles_match("Ive Officially Lost Vision!!!!", "IVE OFFICIALLY LOST VISION!!!!")
    assert titles_match(":(Failure(:", ":(failure(:")
    assert titles_match("Pretty", "pRETTy")
    assert titles_match("Let's Start Here", "Lets Start Here.")
    assert titles_match("Aye (feat. Travis Scott)", "Aye")
    assert titles_match("Heroes (2017 Remaster)", "Heroes")
    assert not titles_match("Track 1", "Track 2")
    assert not titles_match("Accordion", "Meat Grinder")
    assert norm_title("Running Out Of Time") == "running out of time"


def test_sides_and_side_end():
    al = make_album([100, 100, 100, 100], positions=["A1", "A2", "B1", "B2"])
    assert al.side_at(1) == "A" and al.side_at(2) == "B"
    assert al.is_side_end(1) and not al.is_side_end(0)
    assert al.is_side_end(3)                       # last track of the record
    assert side_of("C3") == "C" and side_of("1-4") == "" and side_of("") == ""
    assert side_of("2A") == "A" and side_of("12B") == "B" and side_of("B") == "B"
    assert side_of("A.3") == "A" and side_of("7") == "" and side_of("CD1-3") == ""
    numbered = make_album([100, 100, 100], positions=["1A", "2A", "1B"])    # Madvillainy style
    assert numbered.side_at(1) == "A" and numbered.is_side_end(1) and numbered.has_sides
    plain = make_album([100, 100])
    assert not plain.has_sides and not plain.is_side_end(0)


def test_merge_keeps_pressing_order_and_spotify_durations():
    streaming = make_album([200, 150, 180], album_id="sp", titles=["the ride-", "pRETTy", "Bonus"])
    pressing = AlbumTracklist(
        album_id="dg:1", album_name="Let's Start Here.", artist="Lil Yachty",
        tracks=[TrackInfo(title="Pretty", artist="Lil Yachty", source="discogs"),
                TrackInfo(title="The Ride-", artist="Lil Yachty", source="discogs"),
                TrackInfo(title="Vinyl Only", artist="Lil Yachty", duration=95, source="discogs")],
        positions=["A1", "A2", "B1"], source="discogs", discogs_release_id=1, owned=True,
    )
    assert covers(streaming, pressing) == 2 / 3
    m = merge(streaming, pressing)
    assert [t.title for t in m.tracks] == ["pRETTy", "the ride-", "Vinyl Only"]
    assert [t.duration for t in m.tracks] == [150, 200, 95]
    assert m.positions == ["A1", "A2", "B1"] and m.owned and m.source == "merged"
    assert m.album_id == "sp" and m.discogs_release_id == 1
    assert m.find_track_index("Pretty") == 0       # fuzzy lookup still finds Spotify's spelling


def test_discogs_helpers():
    assert clean_artist("Travis Scott (2)") == "Travis Scott"
    assert clean_artist("Various") == "Various Artists"
    assert parse_duration("3:45") == 225 and parse_duration("1:02:03") == 3723
    assert parse_duration("") is None and parse_duration("abc") is None
    assert same_album("Whole Lotta Red ", "Whole Lotta Red")
    assert same_album("2 Alivë (Geëk Pack) ", "2 Alivë")
    assert not same_album("Madvillainy Instrumentals", "Madvillainy")
    rel = Release.from_dict({"id": 1, "artist": "X", "title": "Y", "positions": [["A1", "Song"]],
                             "loaded": True})
    assert rel.tracks == [{"position": "A1", "title": "Song", "duration": None, "artist": ""}]
    assert rel.vinyl


def test_artist_matching():
    assert artist_matches(["Lil Yachty", "Diana Gordon"], "Lil Yachty")
    assert artist_matches(["Travis Scott"], "Travi$ Scott")
    assert artist_matches(["Kanye West"], "Kanye West & Ty Dolla $ign")
    assert artist_matches(["Madvillain"], "Madvillain")
    assert not artist_matches(["Lil Uzi Vert"], "Lil Yachty")


# ── resolver with fakes ──────────────────────────────────────────────────────

def cand(name, artist, album_id, album, kind, rank, total=10):
    return SpotifyCandidate(name=name, artists=[artist], album_id=album_id, album_name=album,
                            album_type=kind, total_tracks=total, release_date="2023",
                            duration=200, art_url=f"https://img/{album_id}", rank=rank)


class FakeSpotify:
    def __init__(self, cands, albums):
        self.cands = cands
        self.albums = albums          # album_id -> AlbumTracklist

    def search_tracks(self, artist, title):
        return list(self.cands)

    def get_album_tracklist(self, album_id):
        return self.albums.get(album_id)


class FakeCollection:
    def __init__(self, releases):
        self.releases = releases      # list[Release]

    def releases_for_artist(self, artist):
        return [r for r in self.releases if r.artist.lower().startswith(artist.lower()[:5])]

    def find_release(self, artist, album):
        for r in self.releases_for_artist(artist):
            if same_album(r.title, album):
                return r
        return None

    def releases_with_track(self, artist, title):
        return [r for r in self.releases_for_artist(artist)
                if r.loaded and any(titles_match(t["title"], title) for t in r.tracks)]

    def tracklist(self, rel):
        from spindle.discogs import DiscogsCollection
        return DiscogsCollection.tracklist(self, rel)  # type: ignore[arg-type]


def yachty_release(loaded=True):
    titles = ["The Black Seminole.", "The Ride-", "Running Out Of Time", "Pretty"]
    return Release(25926235, "Lil Yachty", "Let’s Start Here.", 2023, ["Vinyl"],
                   [{"position": f"A{i + 1}", "title": t, "duration": None, "artist": ""}
                    for i, t in enumerate(titles)], loaded)


def test_resolver_prefers_the_owned_album_over_singles_and_compilations():
    cands = [
        cand("the BLACK seminole.", "Lil Yachty", "comp", "chill sleeping rap", "compilation",
             0, 42),
        cand("the BLACK seminole.", "Lil Yachty", "single", "Strike (Holster)", "single", 1, 3),
        cand("the BLACK seminole.", "Lil Yachty", "album", "Let’s Start Here.", "album", 2, 14),
    ]
    albums = {
        "album": make_album([418, 250, 269, 162], artist="Lil Yachty", album="Let’s Start Here.",
                            album_id="album",
                            titles=["the BLACK seminole.", "the ride-", "running out of time",
                                    "pRETTy"]),
        "single": make_album([418], artist="Lil Yachty", album="Strike (Holster)",
                             album_id="single", titles=["the BLACK seminole."]),
    }
    r = Resolver(FakeSpotify(cands, albums), FakeCollection([yachty_release()]))
    out = r.resolve(TrackInfo(title="the BLACK seminole.", artist="Lil Yachty", source="shazam"))
    assert out.album_id == "album" and out.owned
    assert out.tracklist.source == "merged" and out.tracklist.positions == ["A1", "A2", "A3", "A4"]
    assert [t.duration for t in out.tracklist.tracks] == [418, 250, 269, 162]
    assert out.track.title == "the BLACK seminole." and out.track.source == "spotify_lookup"
    assert out.art_url == "https://img/album"


def test_resolver_without_collection_prefers_album_type_then_spotify_order():
    cands = [
        cand("Song", "Artist", "single", "Song - Single", "single", 0, 1),
        cand("Song", "Artist", "albumB", "Second Album", "album", 1, 12),
        cand("Song", "Artist", "albumA", "First Album", "album", 2, 12),
    ]
    albums = {k: make_album([200], artist="Artist", album=k, album_id=k, titles=["Song"])
              for k in ("single", "albumA", "albumB")}
    r = Resolver(FakeSpotify(cands, albums), None)
    out = r.resolve(TrackInfo(title="Song", artist="Artist"))
    assert out.album_id == "albumB"                # album beats single; Spotify order wins ties
    assert not out.owned and out.tracklist.source == "spotify"


def test_resolver_sticks_to_the_locked_album_when_nothing_is_owned():
    cands = [
        cand("Song", "Artist", "albumB", "Second Album", "album", 0, 12),
        cand("Song", "Artist", "albumA", "First Album", "album", 1, 12),
    ]
    albums = {k: make_album([200], artist="Artist", album=k, album_id=k, titles=["Song"])
              for k in ("albumA", "albumB")}
    r = Resolver(FakeSpotify(cands, albums), None)
    out = r.resolve(TrackInfo(title="Song", artist="Artist"),
                    ResolveContext(locked_album_id="albumA", locked_artist="Artist"))
    assert out.album_id == "albumA"


def test_resolver_falls_back_to_discogs_only_when_spotify_has_nothing():
    rel = yachty_release()
    r = Resolver(FakeSpotify([], {}), FakeCollection([rel]))
    out = r.resolve(TrackInfo(title="running out of time", artist="Lil Yachty", source="shazam"))
    assert out.album_id == "dg:25926235" and out.owned and out.tracklist.source == "discogs"
    assert out.track.title == "Running Out Of Time" and out.track.album == "Let’s Start Here."
    assert out.tracklist.positions == ["A1", "A2", "A3", "A4"]
    assert out.tracklist.year == "2023"


def test_resolver_rejects_a_wrong_title_or_artist():
    cands = [cand("Completely Different", "Artist", "a", "Album", "album", 0)]
    out = Resolver(FakeSpotify(cands, {}), None).resolve(TrackInfo(title="Song", artist="Artist"))
    assert not out.has_album and out.track.title == "Song"
    cands = [cand("Song", "Somebody Else", "a", "Album", "album", 0)]
    out = Resolver(FakeSpotify(cands, {}), None).resolve(TrackInfo(title="Song", artist="Artist"))
    assert not out.has_album

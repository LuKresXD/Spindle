from conftest import make_album

from spindle.discogs import Release, clean_artist, parse_duration, same_album
from spindle.fingerprint import TrackInfo
from spindle.resolve import ResolveContext, Resolver
from spindle.spotify import EDITION_RE, SpotifyCandidate, artist_matches
from spindle.tracklist import (
    AlbumTracklist,
    box_part,
    covers,
    merge,
    norm_title,
    side_of,
    titles_match,
)


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


def test_a_title_that_is_on_the_record_twice_is_the_copy_on_the_needles_side():
    """A 2LP whose side D closes on a remix of a side B track: heard on side D, the title
    anchored to B2 and the screen jumped two sides back."""
    titles = ["Good Morning", "Champion", "I Wonder", "Can't Tell Me Nothing",
              "Homecoming", "Big Brother", "Stronger (A-Trak Remix)",
              "Can't Tell Me Nothing (Featuring Young Jeezy)"]
    al = make_album([200] * 8, titles=titles,
                    positions=["A1", "A2", "B1", "B2", "C1", "C2", "D1", "D2"])
    assert al.find_track_index("Can't Tell Me Nothing") == 3            # no needle: best spelling
    assert al.find_track_index("Can't Tell Me Nothing", near=3) == 3
    assert al.find_track_index("Can't Tell Me Nothing", near=6) == 7    # on side D: the remix
    assert al.find_track_index("Can't Tell Me Nothing", near=7) == 7
    assert al.find_track_index("Can't Tell Me Nothing", near=4) == 3    # side C has neither
    assert al.find_track_index("Champion", near=7) == 1                 # one copy: that one
    assert al.find_track_index("Nope", near=2) is None
    plain = make_album([100] * 4, titles=["Intro", "Song", "Intro", "Outro"])
    assert plain.find_track_index("Intro", near=3) == 2                 # no sides: the nearer


def test_discogs_helpers():
    assert clean_artist("Travis Scott (2)") == "Travis Scott"
    assert clean_artist("Future (4), Metro Boomin") == "Future, Metro Boomin"
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
    def __init__(self, cands, albums, by_title=None):
        self.cands = cands
        self.albums = albums          # album_id -> AlbumTracklist
        self.by_title = by_title      # title -> candidates; None = `cands` whatever is asked
        self.asked = []

    def search_tracks(self, artist, title):
        self.asked.append(title)
        if self.by_title is not None:
            return list(self.by_title.get(title, []))
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
    assert out.track.title == "the BLACK seminole." and out.track.source == "spotify_album"
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


PROMISE = ["Is It a Crime", "The Sweetest Taboo", "War of the Hearts", "You're Not the Man",
           "Jezebel", "Mr Wrong", "Punch Drunk", "Never as Good as the First Time", "Fear",
           "Tar Baby", "Maureen"]


def sade_box():
    """Six records in one release, sides A to L; here the first three albums."""
    diamond = ["Smooth Operator", "Your Love Is King", "Hang On To Your Love",
               "Frankie's First Affair", "When Am I Going To Make A Living", "Cherry Pie",
               "Sally", "I Will Be Your Friend", "Why Can't We Live Together"]
    stronger = ["Love Is Stronger Than Pride", "Paradise", "Nothing Can Come Between Us",
                "Haunt Me", "Turn My Back On You", "Keep Looking", "Clean Heart", "Give It Up",
                "I Never Thought I'd See The Day", "Siempre Hay Esperanza"]
    sides = [("A", 4), ("B", 5), ("C", 5), ("D", 6), ("E", 5), ("F", 5)]
    pos = [f"{side}{i}" for side, n in sides for i in range(1, n + 1)]
    titles = diamond + [t.title() for t in PROMISE] + stronger
    tracks = [{"position": p, "title": t, "duration": None, "artist": ""}
              for p, t in zip(pos, titles, strict=True)]
    return Release(16026908, "Sade", "This Far", 2020, ["Box Set", "Vinyl", "Vinyl", "Vinyl"],
                   tracks, True)


def test_box_part_cuts_the_album_sides_out_of_a_box_set():
    pressing = FakeCollection([sade_box()]).tracklist(sade_box())
    promise = make_album([200] * 11, artist="Sade", album="Promise", album_id="promise",
                         titles=PROMISE)
    assert covers(promise, pressing) < 0.5                  # the whole box never qualifies
    part = box_part(pressing, promise)
    assert part is not None
    assert [side_of(p) for p in part.positions] == ["C"] * 5 + ["D"] * 6
    assert part.discogs_release_id == 16026908 and covers(promise, part) == 1.0
    # scattered hits (a compilation box) give nothing
    scattered = make_album([200, 200], artist="Sade", album="Best", album_id="best",
                           titles=["Smooth Operator", "Maureen"])
    assert box_part(pressing, scattered) is None
    # a single record is never cut, a double album with the album on its first record is
    one_lp = make_album([100, 100], positions=["A1", "B1"])
    assert box_part(one_lp, make_album([100], titles=["Track 1"])) is None
    two_lp = make_album([100] * 4, positions=["A1", "B1", "C1", "D1"])
    half = box_part(two_lp, make_album([100, 100], titles=["Track 1", "Track 2"]))
    assert half is not None and half.positions == ["A1", "B1"]
    assert box_part(make_album([100, 100]), promise) is None   # no sides at all


def test_resolver_merges_a_box_set_album_with_its_own_sides():
    cands = [cand("Is It a Crime", "Sade", "promise", "Promise", "album", 0, 1),
             cand("Is It a Crime", "Sade", "best", "The Best of Sade", "compilation", 1, 2)]
    durations = [384, 277, 400, 300, 325, 250, 330, 320, 280, 240, 260]
    albums = {"promise": make_album(durations, artist="Sade", album="Promise",
                                    album_id="promise", titles=PROMISE)}
    r = Resolver(FakeSpotify(cands, albums), FakeCollection([sade_box()]))
    out = r.resolve(TrackInfo(title="Is It a Crime", artist="Sade", source="shazam"))
    assert out.album_id == "promise" and out.owned
    assert out.tracklist.source == "merged" and out.tracklist.album_name == "Promise"
    assert out.tracklist.positions == [f"C{i}" for i in range(1, 6)] + \
        [f"D{i}" for i in range(1, 7)]
    assert out.tracklist.discogs_release_id == 16026908
    assert out.tracklist.is_side_end(4) and not out.tracklist.is_side_end(3)
    assert [t.duration for t in out.tracklist.tracks] == durations
    assert out.reason == "spotify+discogs (100% of sides C-D of the box)"


def test_edition_marker_is_a_qualifier_not_a_word_in_the_title():
    editions = ["Whole Lotta Red (Deluxe)", "Graduation (Deluxe Edition)",
                "Heroes (2017 Remaster)", "Love Deluxe - 2011 Remaster",
                "Thriller 25 Super Deluxe Edition", "Low [Remastered]", "Promise: Expanded Edition",
                "Madvillainy (Bonus Tracks)", "Ok Computer Remastered"]
    plain = ["Love Deluxe", "Diamond Life", "Deluxe", "The Bonus Round", "Anniversary",
             "Stronger Than Pride", "Special Edition Of You", "1999", "2 Alivë (Geëk Pack)"]
    assert [bool(EDITION_RE.search(x)) for x in editions] == [True] * len(editions)
    assert [bool(EDITION_RE.search(x)) for x in plain] == [False] * len(plain)


# ── the record first: editions chosen by how well they agree with the pressing ───────────

def _release(rid, artist, title, titles, sides=("A", "B"), durations=None):
    """A loaded vinyl release whose tracks are split evenly over `sides`."""
    per = -(-len(titles) // len(sides))
    tracks = [{"position": f"{sides[i // per]}{i % per + 1}", "title": t,
               "duration": durations[i] if durations else None, "artist": ""}
              for i, t in enumerate(titles)]
    return Release(rid, artist, title, 2020, ["Vinyl"], tracks, True)


BAD = ["Bad", "The Way You Make Me Feel", "Speed Demon", "Liberian Girl", "Just Good Friends",
       "Another Part of Me", "Man in the Mirror", "I Just Can't Stop Loving You",
       "Dirty Diana", "Smooth Criminal"]


def test_the_edition_is_the_album_that_contains_the_record_not_the_one_listed_first():
    """Deezer lists "Smooth Criminal" on a soundtrack first; the old pick went by the list's
    order and the album's name, and the side ran on a list with 4 of the record's 10 tracks."""
    cands = [cand("Smooth Criminal", "Michael Jackson", "thisisit", "This Is It", "album", 0),
             cand("Smooth Criminal - 2012 Remaster", "Michael Jackson", "bad", "Bad (Remastered)",
                  "album", 1)]
    albums = {
        "thisisit": make_album([250] * 8, artist="Michael Jackson", album="This Is It",
                               album_id="thisisit",
                               titles=["Wanna Be Startin' Somethin'", "Jam", "Human Nature",
                                       "Smooth Criminal", "The Way You Make Me Feel",
                                       "Man in the Mirror", "Thriller", "Bad"]),
        "bad": make_album(list(range(200, 210)), artist="Michael Jackson",
                          album="Bad (Remastered)", album_id="bad",
                          titles=[f"{t} - 2012 Remaster" for t in BAD]),
    }
    r = Resolver(FakeSpotify(cands, albums), FakeCollection([_release(7, "Michael Jackson",
                                                                      "Bad", BAD)]))
    out = r.resolve(TrackInfo(title="Smooth Criminal", artist="Michael Jackson"))
    assert out.album_id == "bad" and out.owned and out.tracklist.source == "merged"
    assert out.reason == "spotify+discogs (100% of pressing)"
    assert out.track.title == "Smooth Criminal - 2012 Remaster" and out.track.duration == 209
    assert out.tracklist.position_at(9) == "B5"


def test_an_anthology_that_contains_the_album_and_thirty_more_is_not_its_edition():
    """Кино's "Группа крови" was dressed as "Легенда": every track of the album is on the
    40-track anthology, which is all a one-way coverage test can see."""
    album = ["Группа крови", "Закрой за мной дверь", "Война", "Спокойная ночь", "Мама",
             "Бошетунмай", "В наших глазах", "Попробуй спеть", "Прохожий", "Дальше", "Легенда"]
    more = [f"Песня {i}" for i in range(30)]
    cands = [cand("Группа крови", "Кино", "legenda", "Легенда", "album", 0),
             cand("Группа крови", "Кино", "gk", "Группа крови", "album", 1)]
    albums = {
        "legenda": make_album([240] * 41, artist="Кино", album="Легенда", album_id="legenda",
                              titles=album + more),
        "gk": make_album([285] * 10, artist="Кино", album="Группа крови", album_id="gk",
                         titles=album[:10]),          # the catalogue's album lacks one track
    }
    r = Resolver(FakeSpotify(cands, albums),
                 FakeCollection([_release(8, "Кино", "Группа Крови", album)]))
    out = r.resolve(TrackInfo(title="Группа крови", artist="Кино"))
    assert out.album_id == "gk" and out.tracklist.album_name == "Группа крови"
    assert out.reason == "spotify+discogs (91% of pressing)"
    assert len(out.tracklist.tracks) == 11 and out.tracklist.tracks[10].title == "Легенда"


BLOND = ["Nikes", "Ivy", "Pink + White", "Solo", "Nights", "White Ferrari",
         "Chanel (Bonus Track)", "Biking (Bonus Track)", "Godspeed", "Futura Free"]


def _blond(by_title):
    blonde = make_album([300, 250, 184, 257, 307, 248, 177, 564], artist="Frank Ocean",
                        album="Blonde", album_id="blonde",
                        titles=["Nikes", "Ivy", "Pink + White", "Solo", "Nights",
                                "White Ferrari", "Godspeed", "Futura Free"])
    single = make_album([210], artist="Frank Ocean", album="Chanel", album_id="chanel",
                        titles=["Chanel"])
    rel = _release(9, "Frank Ocean", "Blond", BLOND, sides=("A", "B", "C", "D"))
    cat = FakeSpotify([], {"blonde": blonde, "chanel": single}, by_title)
    return Resolver(cat, FakeCollection([rel])), cat


def test_a_bonus_track_that_only_came_out_as_a_single_is_still_the_record():
    """Blond with bonus tracks: a side opened on "Chanel", the only catalogue album with it
    is the single, the lock went to the single and "Biking" was never scrobbled."""
    chanel = [cand("Chanel", "Frank Ocean", "chanel", "Chanel", "single", 0)]
    nikes = [cand("Nikes", "Frank Ocean", "blonde", "Blonde", "album", 0)]
    r, cat = _blond({"Chanel": chanel, "Nikes": nikes, "Ivy": []})
    # another track of the record was heard before: its edition is the record's
    first = r.resolve(TrackInfo(title="Nikes", artist="Frank Ocean"))
    assert first.album_id == "blonde" and first.reason == "spotify+discogs (80% of pressing)"
    out = r.resolve(TrackInfo(title="Chanel", artist="Frank Ocean"))
    assert out.album_id == "blonde" and out.owned and out.tracklist is first.tracklist
    assert out.tracklist.position_at(6) == "C1" and out.tracklist.is_side_end(5)
    # ... and the single gave the bonus track its name and its length
    assert out.track.title == "Chanel" and out.track.duration == 200
    assert out.tracklist.tracks[6].title == "Chanel"
    assert out.tracklist.tracks[7].title == "Biking (Bonus Track)"       # not heard yet


def test_with_nothing_heard_before_the_catalogue_is_asked_about_the_tracks_next_to_it():
    chanel = [cand("Chanel", "Frank Ocean", "chanel", "Chanel", "single", 0)]
    ferrari = [cand("White Ferrari", "Frank Ocean", "blonde", "Blonde", "album", 0)]
    r, cat = _blond({"Chanel": chanel, "White Ferrari": ferrari})
    out = r.resolve(TrackInfo(title="Chanel", artist="Frank Ocean"))
    assert out.album_id == "blonde" and out.reason == "spotify+discogs (80% of pressing)"
    assert out.art_url == "https://img/blonde"                # the album's cover, not the single's
    asked = len(cat.asked)
    r.resolve(TrackInfo(title="Chanel", artist="Frank Ocean"))
    assert len(cat.asked) <= asked + 3                        # the edition is not asked for again


def test_a_pressing_no_edition_contains_is_its_own_album_and_learns_from_the_catalogue():
    """A pressing that differs wholesale from the streaming album of the same name (15% in
    common): the old pick locked the streaming list and the record's own tracks were lost."""
    mine = ["Preacher Man", "Beauty and the Beast", "Last Breath", "Losing Your Mind",
            "This One Here", "Circles"]
    streaming = make_album([200] * 7, artist="Kanye West", album="BULLY", album_id="bully",
                           titles=["Preacher Man", "Other 1", "Other 2", "Other 3", "Other 4",
                                   "Other 5", "Other 6"])
    by_title = {"Preacher Man": [cand("PREACHER MAN", "Kanye West", "bully", "BULLY", "album", 0)],
                "Beauty and the Beast": [cand("BEAUTY AND THE BEAST", "Kanye West", "single2",
                                              "BEAUTY AND THE BEAST", "single", 0)]}
    cat = FakeSpotify([], {"bully": streaming}, by_title)
    rel = _release(10, "Kanye West", "Bully", mine)
    r = Resolver(cat, FakeCollection([rel]))
    out = r.resolve(TrackInfo(title="Preacher Man", artist="Kanye West"))
    assert out.album_id == "dg:10" and out.owned and out.reason == "discogs"
    assert out.tracklist.source == "discogs" and len(out.tracklist.tracks) == 6
    assert out.track.title == "PREACHER MAN" and out.track.duration == 200    # the catalogue's
    assert out.track.album == "Bully"                                         # the record's
    # the next track's length was asked for ahead of the clock
    assert out.tracklist.tracks[1].duration == 200
    assert out.tracklist.tracks[1].title == "Beauty and the Beast"   # named when it is heard
    again = r.resolve(TrackInfo(title="Beauty and the Beast", artist="Kanye West"))
    assert again.tracklist is out.tracklist                    # one list: the session sees it
    assert again.track.title == "BEAUTY AND THE BEAST"
    for _ in range(3):              # two lengths a hit: the rest of the list is asked about
        r.resolve(TrackInfo(title="Beauty and the Beast", artist="Kanye West"))
    asked = len(cat.asked)
    for _ in range(5):
        r.resolve(TrackInfo(title="Beauty and the Beast", artist="Kanye West"))
    # from then on a hit costs its own lookup: the edition and the lengths that were not
    # found are not asked for again
    assert cat.asked[asked:] == ["Beauty and the Beast"] * 5


def _two_records_one_song():
    noch = _release(11, "Кино", "Ночь", ["Видели ночь", "Фильмы", "Последний герой", "Игра"])
    kamchatka = _release(5, "Кино", "Начальник Камчатки",
                         ["Последний герой", "Каждую ночь", "Транквилизатор", "Троллейбус"])
    albums = {
        "noch": make_album([200] * 4, artist="Кино", album="Ночь", album_id="noch",
                           titles=["Видели ночь", "Фильмы", "Последний герой", "Игра"]),
        "kam": make_album([210] * 4, artist="Кино", album="Начальник Камчатки", album_id="kam",
                          titles=["Последний герой", "Каждую ночь", "Транквилизатор",
                                  "Троллейбус"]),
    }
    cands = [cand("Последний герой", "Кино", "noch", "Ночь", "album", 0),
             cand("Последний герой", "Кино", "kam", "Начальник Камчатки", "album", 1)]
    return Resolver(FakeSpotify(cands, albums), FakeCollection([kamchatka, noch]))


def test_a_song_on_two_owned_records_is_the_one_being_played():
    hit = TrackInfo(title="Последний герой", artist="Кино")
    # nothing known: the catalogue's first album for the song
    assert _two_records_one_song().resolve(hit).tracklist.discogs_release_id == 11
    # the session is on the other record: that one
    out = _two_records_one_song().resolve(hit, ResolveContext(locked_release_id=5))
    assert out.tracklist.discogs_release_id == 5 and out.tracklist.position_at(0) == "A1"
    # the fingerprint names its album: that one
    named = TrackInfo(title="Последний герой", artist="Кино", album="Начальник Камчатки")
    assert _two_records_one_song().resolve(named).tracklist.discogs_release_id == 5


def test_a_box_side_whose_own_album_the_search_missed_is_found_by_its_neighbours():
    """Deezer answers "Is It a Crime" with best-ofs only; the track after it on the side
    brings the album, and the side gets its own sides of the box as before."""
    best_of = make_album([240] * 4, artist="Sade", album="The Best of Sade", album_id="best",
                         titles=["Smooth Operator", "Is It a Crime", "Paradise", "Maureen"])
    promise = make_album([250] * 11, artist="Sade", album="Promise", album_id="promise",
                         titles=PROMISE)
    by_title = {"Is It a Crime": [cand("Is It a Crime", "Sade", "best", "The Best of Sade",
                                       "album", 0)],
                "The Sweetest Taboo": [cand("The Sweetest Taboo", "Sade", "promise", "Promise",
                                            "album", 0)]}
    r = Resolver(FakeSpotify([], {"best": best_of, "promise": promise}, by_title),
                 FakeCollection([sade_box()]))
    out = r.resolve(TrackInfo(title="Is It a Crime", artist="Sade"))
    assert out.album_id == "promise" and out.tracklist.album_name == "Promise"
    assert out.reason == "spotify+discogs (100% of sides C-D of the box)"
    assert out.tracklist.positions[0] == "C1" and out.track.duration == 250
    # the answer is remembered for the side, and stays good on the next hits (the pressing is
    # built anew for every hit: a remembered part must not be compared by object)
    again = r.resolve(TrackInfo(title="Is It a Crime", artist="Sade"))
    taboo = r.resolve(TrackInfo(title="The Sweetest Taboo", artist="Sade"))
    assert again.tracklist is out.tracklist and taboo.tracklist is out.tracklist
    assert taboo.tracklist.position_at(1) == "C2"

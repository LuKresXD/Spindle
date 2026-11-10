"""The collection as the shelf sees it: covers, masters, original years, the random draw."""
import threading

from spindle.discogs import DiscogsCollection, Release


def _collection(releases, routes):
    c = DiscogsCollection.__new__(DiscogsCollection)
    c._lock = threading.RLock()
    c._stop = threading.Event()
    c._releases = releases
    c._aka = {}
    c._keys = {}
    c._track_keys = {}
    c._names = {}
    c.last_error = None
    c._save_cache = lambda: None
    c.calls = []

    def get(path, params=None):
        c.calls.append(path)
        return routes[path]
    c._get = get
    return c


def test_release_roundtrip_keeps_cover_master_and_unknown_year():
    r = Release(1, "Massive Attack", "Mezzanine", 2023, ["Vinyl"], [], True, "2026-02-26",
                "https://img/mezz.jpg", 10, None)
    back = Release.from_dict(r.to_dict())
    assert back.cover == r.cover and back.master_id == 10 and back.released is None
    old = Release.from_dict({"id": 2, "artist": "a", "title": "t"})     # a pre-shelf cache entry
    assert old.cover == "" and old.master_id == 0 and old.released is None


def test_masters_pass_asks_only_known_masters_and_never_guesses():
    with_master = Release(1, "Massive Attack", "Mezzanine", 2023, ["Vinyl"], [], True,
                          master_id=10)
    unknown = Release(2, "Кино", "45", 2016, ["Vinyl"], [], True)        # no master id yet
    c = _collection([with_master, unknown], {"/masters/10": {"year": 1998}})
    c._prefetch_masters()
    assert with_master.released == 1998
    assert unknown.released is None and c.calls == ["/masters/10"]      # left for the refresh
    c._prefetch_masters()
    assert c.calls == ["/masters/10"]                                    # asked once


def _item(rid, title, year, master, artist, cover, added):
    return {"basic_information": {"id": rid, "title": title, "year": year, "master_id": master,
                                  "artists": [{"name": artist}], "formats": [{"name": "Vinyl"}],
                                  "cover_image": cover}, "date_added": added}


def test_collection_fetch_dedupes_and_marks_originals():
    noch = _item(5, "Ночь", 2017, 77, "Кино", "https://img/noch.jpg", "2026-10-14T01:02:03-07:00")
    boot = _item(6, "Bootleg", 2019, None, "Kanye West", "", "2026-11-06T00:00:00-07:00")
    page = {"pagination": {"pages": 1}, "releases": [noch, noch, boot]}
    c = _collection([], {"/users/LuKres/collection/folders/0/releases": page})
    c.username = "LuKres"
    out = c._fetch_collection()
    assert [r.id for r in out] == [5, 6]                     # the second copy is one record
    assert out[0].cover == "https://img/noch.jpg" and out[0].master_id == 77
    assert out[0].released is None                           # a master to ask later
    assert out[1].master_id == 0 and out[1].released == 2019  # no master: its own original


def test_shelf_queries():
    a = Release(1, "A", "x", formats=["Vinyl"], added="2026-01-01")
    b = Release(2, "B", "y", formats=["Vinyl"], added="2026-02-01")
    cd = Release(3, "C", "z", formats=["CD"], added="2026-03-01")
    c = _collection([a, b, cd], {})
    assert [r.id for r in c.all_releases()] == [3, 2, 1]
    assert c.release_by_id(2) is b and c.release_by_id(9) is None
    assert c.random_release({1}) is b                        # vinyl only, never the excluded one
    assert c.random_release({1, 2}) in (a, b)                 # nothing left: anything on vinyl


def test_prices_are_asked_once_a_day_and_kept():
    rel = Release(1, "Massive Attack", "Mezzanine", 2023, ["Vinyl"], [], True)
    routes = {"/marketplace/stats/1": {"lowest_price": {"value": 42.5}, "num_for_sale": 7},
              "/marketplace/price_suggestions/1": {"Very Good Plus (VG+)": {"value": 61.0}}}
    c = _collection([rel], routes)
    c._details = {}
    c._prefetch_prices()
    assert (rel.price_usd, rel.typical_usd, rel.for_sale) == (42.5, 61.0, 7) and rel.priced_at > 0
    assert c.calls == ["/marketplace/stats/1", "/marketplace/price_suggestions/1"]
    c._prefetch_prices()
    assert len(c.calls) == 2                                           # fresh: not asked again
    back = Release.from_dict(rel.to_dict())
    assert back.typical_usd == 61.0 and back.for_sale == 7 and back.priced_at == rel.priced_at


# ── whose record it is: separate names, Discogs' other names, a track's own artist ───────

def _tracks(*rows):
    return [{"position": p, "title": t, "duration": None, "artist": a} for p, t, a in rows]


def test_a_record_answers_to_what_discogs_says_its_artist_is_also_called():
    """Bully is filed under "Ye (2)" and Shazam says "Kanye West"; the old substring test
    matched them by accident ("ye" is inside "kanye west") and matched Yeat as well."""
    bully = Release(1, "Ye (2)", "Bully", 2026, ["Vinyl"], _tracks(("A1", "Preacher Man", "")),
                    True, artists=["Ye (2)"], artist_ids=[11470286])
    yeat = Release(2, "Yeat", "2 Alivë", 2022, ["Vinyl"], _tracks(("A1", "Poppin", "")), True,
                   artists=["Yeat"], artist_ids=[7])
    kino = Release(3, "Кино", "Ночь", 2017, ["Vinyl"], _tracks(("A1", "Видели ночь", "")), True,
                   artists=["Кино"], artist_ids=[345278])
    c = _collection([bully, yeat, kino], {
        "/artists/11470286": {"namevariations": [], "aliases": [{"name": "Kanye West"},
                                                                 {"name": "Yeezy"}]},
        "/artists/7": {"namevariations": ["YEAT"], "aliases": []},
        "/artists/345278": {"namevariations": ["Kino", "КИНО"], "aliases": []},
    })
    assert c.releases_for_artist("Kanye West") == []            # nothing known yet
    c._prefetch_artists()
    assert c.calls == ["/artists/11470286", "/artists/7", "/artists/345278"]
    assert c.releases_for_artist("Kanye West") == [bully]
    assert c.releases_for_artist("Ye") == [bully]               # and not Yeat
    assert c.releases_for_artist("Yeat") == [yeat]
    assert c.releases_for_artist("Kino") == [kino]
    assert c.releases_with_track("Kanye West", "PREACHER MAN") == [bully]
    assert c.names_for("Ye") == ["Ye (2)", "Kanye West", "Yeezy"]
    assert c.names_for("Sade") == []
    c._prefetch_artists()
    assert len(c.calls) == 3                                    # each artist is asked once


def test_a_compilation_track_is_owned_by_its_own_artist():
    """A Various Artists soundtrack was never recognised: no hit is by "Various"."""
    va = Release(4, "Various", "Stranger Things: The WSQK Collection", 2025, ["Vinyl"],
                 _tracks(("A1", "I Think We're Alone Now", "Tiffany"),
                         ("A2", "Should I Stay Or Should I Go", "The Clash"),
                         ("B1", "Upside Down", "Diana Ross")), True,
                 artists=["Various"], artist_ids=[194])
    crew = Release(5, "Jackboys", "Jackboys", 2020, ["Vinyl"],
                   _tracks(("A1", "Highest In The Room (Remix)", "Travis Scott (2)"),
                           ("A2", "Gang Gang", "Jackboys")), True,
                   artists=["Jackboys"], artist_ids=[7566128])
    c = _collection([va, crew], {"/artists/7566128": {}})
    c._prefetch_artists()
    assert c.calls == ["/artists/7566128"]                      # "Various" is nobody to ask about
    assert c.releases_with_track("Tiffany", "I Think We're Alone Now") == [va]
    assert c.releases_with_track("The Clash", "Should I Stay or Should I Go") == [va]
    assert c.releases_with_track("Tiffany", "Upside Down") == []     # hers is another track
    assert c.releases_with_track("Travis Scott", "HIGHEST IN THE ROOM (REMIX)") == [crew]
    assert c.releases_with_track("Jackboys", "Gang Gang") == [crew]
    tl = c.tracklist(va)
    assert tl.artist == "Various Artists" and tl.tracks[1].artist == "The Clash"


def test_two_copies_of_an_album_the_one_that_knows_its_sides_first():
    numbered = Release(10, "Playboi Carti", "Whole Lotta Red", 2021, ["Vinyl"],
                       _tracks(("1", "Rockstar Made", ""), ("2", "Go2DaMoon", "")), True)
    sided = Release(20, "Playboi Carti", "Whole Lotta Red", 2022, ["Vinyl"],
                    _tracks(("A1", "Rockstar Made", ""), ("A2", "Go2DaMoon", "")), True)
    c = _collection([numbered, sided], {})
    assert c.releases_with_track("Playboi Carti", "Rockstar Made") == [sided, numbered]
    assert c.find_release("Playboi Carti", "Whole Lotta Red") is sided


def test_the_cache_keeps_names_and_asks_again_when_it_predates_them(tmp_path):
    rel = Release(1, "Future (4), Metro Boomin", "We Don't Trust You", 2024, ["Vinyl"], [], True,
                  artists=["Future (4)", "Metro Boomin"], artist_ids=[221673, 3244227])
    back = Release.from_dict(rel.to_dict())
    assert back.artists == ["Future (4)", "Metro Boomin"] and back.artist_ids == [221673, 3244227]
    import json
    import time
    cache = tmp_path / "collection-u.json"
    cache.write_text(json.dumps({"fetched_at": time.time(), "releases": [rel.to_dict()],
                                 "aka": {"221673": ["Future Hndrxx"]}}))
    c = DiscogsCollection("t", "u", cache_dir=tmp_path)
    assert c._aka == {221673: ["Future Hndrxx"]} and c._fetched_at > 0
    assert c.releases_for_artist("Future Hndrxx") == c.all_releases()
    old = rel.to_dict()
    del old["artists"], old["artist_ids"]                       # written before 0.7
    cache.write_text(json.dumps({"fetched_at": time.time(), "releases": [old]}))
    c = DiscogsCollection("t", "u", cache_dir=tmp_path)
    assert c._fetched_at == 0.0                                 # the list is asked again
    assert c.releases_for_artist("Metro Boomin") == c.all_releases()   # the credit still splits

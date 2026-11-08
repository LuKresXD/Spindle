"""The collection as the shelf sees it: covers, masters, original years, the random draw."""
import threading

from spindle.discogs import DiscogsCollection, Release


def _collection(releases, routes):
    c = DiscogsCollection.__new__(DiscogsCollection)
    c._lock = threading.RLock()
    c._stop = threading.Event()
    c._releases = releases
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

import time

from spindle.art import ArtProvider, _norm, _similar
from spindle.display import FbInfo, Renderer, make_layout, pack_rows
from spindle.state import Phase, ScreenState, TrackRow


def test_art_matching_is_edition_tolerant():
    assert _norm("Madvillainy (Deluxe Edition)") == "madvillainy"
    assert _similar("Lil Yachty", "lil yachty")
    assert _similar("Let's Start Here.", "Lets Start Here")
    assert not _similar("Whole Lotta Red", "Whole Lotta Clawz")
    assert not _similar("Madvillainy Instrumentals", "Madvillainy")
    assert _similar("Whole Lotta Red (Explicit)", "Whole Lotta Red")


def test_art_cache_roundtrip(tmp_path):
    p = ArtProvider(cache_dir=tmp_path, itunes=False, deezer=False)
    from spindle.art import Art
    p._to_disk(p._key("a", "b"), Art(b"img" * 500, "test", "u"))
    got = p._from_disk(p._key("A ", "b"))
    assert got and got.data == b"img" * 500 and got.source == "test"


def test_layout_scales():
    L = make_layout(1920, 1080)
    assert L.pad == 60 and L.art == 960 and L.text_x == 1100 and L.text_w == 760
    s = make_layout(480, 320)
    assert s.art <= 264 and s.text_w > 100


def test_pack_rows_formats():
    from PIL import Image
    img = Image.new("RGB", (2, 1))
    img.putpixel((0, 0), (255, 0, 0))
    img.putpixel((1, 0), (255, 255, 255))
    xrgb = pack_rows(img, FbInfo("/dev/fb0", "vc4drmfb", 2, 1, 32, 8))
    assert list(xrgb) == [0, 0, 255, 0, 255, 255, 255, 0]
    assert list(pack_rows(img, FbInfo("/dev/fb0", "vc4drmfb", 2, 1, 16, 4))) == [0, 248, 255, 255]
    assert list(pack_rows(img, FbInfo("/dev/fb0", "fb_ili9486", 2, 1, 16, 4))) == [255, 7, 0, 0]
    padded = pack_rows(img, FbInfo("/dev/fb0", "vc4drmfb", 2, 1, 32, 16))
    assert len(padded) == 16 and padded[8:] == bytes(8)


def _state(n_tracks=22, idx=5):
    rows = [TrackRow(title=f"Track {i + 1}", duration=100 + i, position=f"A{i + 1}",
                     scrobbled=i < idx) for i in range(n_tracks)]
    return ScreenState(phase=Phase.PLAYING, artist="A",
                       title="A long title that will need wrapping twice over",
                       album="Album", year="2004", tracklist=rows, current_index=idx,
                       track_elapsed=30, track_duration=100, side="A", position="A6",
                       scrobbled=True, album_playcount=3, updated_at=time.time())


def test_renderer_frames_and_bands():
    r = Renderer(1920, 1080)
    st = _state()
    fr = r.playing(st)
    assert fr.image.size == (1920, 1080) and fr.band
    y0, band = r.playing_band(fr, st)
    assert 0 <= y0 < 1080 and band.width == 1920 and 0 < band.height <= 140
    lf = r.listening()
    y0, band = r.listening_band(lf, -30)
    assert band.width == 1920
    assert r.idle().image.getpixel((5, 5)) == (0, 0, 0)
    # small legacy panel still renders
    r2 = Renderer(480, 320, True)
    assert r2.playing(_state(8, 2)).image.size == (480, 320)


def test_state_identity_and_elapsed():
    st = _state()
    a = st.identity()
    st.track_elapsed = 50                 # dynamic field: identity unchanged
    assert st.identity() == a
    st.scrobbled = False
    assert st.identity() != a
    st.updated_at = time.time() - 10
    assert 39 < st.elapsed_now() < 61

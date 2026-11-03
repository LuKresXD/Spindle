from conftest import FakeSpotify

from spindle.session import CONFIRM_THRESHOLD, ScrobbleSession, SessionMode, normalize_title


def confirmed(session, spotify, title, music_start):
    """Identify the same track CONFIRM_THRESHOLD times (the confirmation gate)."""
    out = []
    for _ in range(CONFIRM_THRESHOLD):
        out = session.on_identified(spotify.lookup("Madvillain", title), music_start)
    return out


def test_normalize_title_strips_editions():
    assert normalize_title("Heroes (Remastered 2017)") == "heroes"
    assert normalize_title('Thriller 7" – Special Edit') == "thriller"
    assert normalize_title("The Girl Is Mine (2008 with will.i.am)") == "the girl is mine"


def test_played_enough_follows_lastfm_rule(clock, album):
    s = ScrobbleSession(FakeSpotify(album), min_play_seconds=30, clock=clock)
    long_track = album.tracks[5]            # 234 s
    assert not s.played_enough(long_track, 31)     # 30 s of a 4-min song: no
    assert s.played_enough(long_track, 117)        # half of it: yes
    short = album.tracks[1]                 # 72 s
    assert not s.played_enough(short, 29)
    assert s.played_enough(short, 36)
    long_long = type(long_track)(title="x", artist="a", duration=1200)
    assert s.played_enough(long_long, 240)         # 4 minutes caps the half rule


def test_lock_then_timing_advance_scrobbles_in_order(clock, album):
    sp = FakeSpotify(album)
    s = ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(10)
    assert confirmed(s, sp, "Track 1", music_start) == []
    assert s.is_locked() and s.mode == SessionMode.ALBUM
    assert s.get_current_track().title == "Track 1"

    clock.advance(115 + 5)                         # past duration + buffer
    done = s.check_advance()
    assert [t.title for t, _ in done] == ["Track 1"]
    assert s.get_current_track().title == "Track 2"
    assert s.album_state.scrobbled == {0}
    assert done[0][1] == music_start               # scrobble timestamp = track start


def test_retroactive_backfill_from_elapsed_time(clock, album):
    sp = FakeSpotify(album)
    s = ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(115 + 72 + 40)                   # two tracks played, 40 s into track 3
    out = confirmed(s, sp, "Track 3", music_start)
    assert [t.title for t, _ in out] == ["Track 1", "Track 2"]
    assert s.album_state.current_index == 2
    # anchor track start refined to music_start + prior durations
    assert abs(s.album_state.current_track_start - (music_start + 187)) < 1e-6


def test_silence_scrobbles_current_only_if_played_enough(clock, album):
    sp = FakeSpotify(album)
    s = ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, sp, "Track 6", music_start)       # the 234 s track
    clock.advance(40)                              # 44 s: over 30 s, under half
    assert s.on_silence() == []
    assert s.mode is None                          # reset

    s = ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, sp, "Track 6", music_start)
    clock.advance(130)
    out = s.on_silence()
    assert [t.title for t, _ in out] == ["Track 6"]


def test_session_dedup_survives_edition_switch(clock, album):
    sp = FakeSpotify(album)
    s = ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(10)
    confirmed(s, sp, "Track 1", music_start)
    clock.advance(125)
    s.check_advance()                              # Track 1 scrobbled by timing
    assert s._try_scrobble(0, music_start) == []   # never twice

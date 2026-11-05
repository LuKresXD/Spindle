from conftest import make_album, resolved_for

from spindle.fingerprint import TrackInfo
from spindle.resolve import Resolved
from spindle.session import CONFIRM_THRESHOLD, ScrobbleSession, SessionMode, normalize_title


def confirmed(session, tracklist, title, music_start, at=None):
    """Identify the same track CONFIRM_THRESHOLD times (the confirmation gate)."""
    out = []
    for _ in range(CONFIRM_THRESHOLD):
        out = session.on_identified(resolved_for(tracklist, title), music_start, at=at)
    return out


def test_normalize_title_strips_editions():
    assert normalize_title("Heroes (Remastered 2017)") == "heroes"
    assert normalize_title('Thriller 7" – Special Edit') == "thriller"
    assert normalize_title("The Girl Is Mine (2008 with will.i.am)") == "the girl is mine"


def test_played_enough_follows_lastfm_rule(clock, album):
    s = ScrobbleSession(min_play_seconds=30, clock=clock)
    long_track = album.tracks[5]            # 234 s
    assert not s.played_enough(long_track, 31)     # 30 s of a 4-min song: no
    assert s.played_enough(long_track, 117)        # half of it: yes
    short = album.tracks[1]                 # 72 s
    assert not s.played_enough(short, 29)
    assert s.played_enough(short, 36)
    long_long = TrackInfo(title="x", artist="a", duration=1200)
    assert s.played_enough(long_long, 240)         # 4 minutes caps the half rule


def test_lock_then_timing_advance_scrobbles_in_order(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(10)
    assert confirmed(s, album, "Track 1", music_start) == []
    assert s.is_locked() and s.mode == SessionMode.ALBUM
    assert s.get_current_track().title == "Track 1"
    assert s.locked_album_id == "alb1" and s.locked_artist == "Madvillain"

    clock.advance(115 + 5)                         # past duration + buffer
    done = s.check_advance()
    assert [t.title for t, _ in done] == ["Track 1"]
    assert s.get_current_track().title == "Track 2"
    assert s.album_state.scrobbled == {0}
    assert done[0][1] == music_start               # scrobble timestamp = track start


def test_anchor_time_is_when_the_audio_ended_not_when_applied(clock, album):
    """A slow identification must not shift the track start by its latency."""
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(115 + 72 + 40)
    heard_at = clock.t
    clock.advance(18)                              # identification took 18 s
    out = confirmed(s, album, "Track 3", music_start, at=heard_at)
    assert [t.title for t, _ in out] == ["Track 1", "Track 2"]
    assert abs(s.album_state.current_track_start - (music_start + 187)) < 1e-6


def test_retroactive_backfill_from_elapsed_time(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(115 + 72 + 40)                   # two tracks played, 40 s into track 3
    out = confirmed(s, album, "Track 3", music_start)
    assert [t.title for t, _ in out] == ["Track 1", "Track 2"]
    assert s.album_state.current_index == 2
    assert abs(s.album_state.current_track_start - (music_start + 187)) < 1e-6


def test_backfill_never_crosses_a_side_boundary(clock):
    # A1 A2 | B1 B2: dropping the needle on B2 after 3 minutes of B1 must not
    # "backfill" side A, which was a different session.
    al = make_album([100, 100, 180, 100], positions=["A1", "A2", "B1", "B2"])
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(180 + 30)
    out = confirmed(s, al, "Track 4", music_start)
    assert [t.title for t, _ in out] == ["Track 3"]


def test_timing_stops_at_the_end_of_a_side(clock):
    al = make_album([100, 100, 100, 100], positions=["A1", "A2", "B1", "B2"])
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, al, "Track 2", music_start)        # A2, the last track of side A
    clock.advance(100 + 20)
    done = s.check_advance()
    assert [t.title for t, _ in done] == ["Track 2"]  # it played in full: scrobbled
    assert s.get_current_track().title == "Track 2"   # ...but no jump to B1
    assert s.album_state.side_done and s.is_locked()
    clock.advance(600)
    assert s.check_advance() == []                    # stays put in the run-out
    # the flip: silence ends the session, nothing double-scrobbled
    assert s.on_silence() == []
    # a fresh anchor on B1 re-opens timing normally
    music_start = clock.t
    clock.advance(4)
    confirmed(s, al, "Track 3", music_start)
    assert not s.album_state.side_done
    clock.advance(100 + 20)
    assert [t.title for t, _ in s.check_advance()] == ["Track 3"]
    assert s.get_current_track().title == "Track 4"


def test_silence_scrobbles_current_only_if_played_enough(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 6", music_start)       # the 234 s track
    clock.advance(40)                                 # 44 s: over 30 s, under half
    assert s.on_silence() == []
    assert s.mode is None                             # reset

    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 6", music_start)
    clock.advance(130)
    out = s.on_silence()
    assert [t.title for t, _ in out] == ["Track 6"]


def test_session_dedup_survives_edition_switch(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(10)
    confirmed(s, album, "Track 1", music_start)
    clock.advance(125)
    s.check_advance()                              # Track 1 scrobbled by timing
    assert s._try_scrobble(0, music_start) == []   # never twice


def test_same_artist_off_album_id_does_not_flip_to_compilation(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    other = make_album([200], artist="Madvillain", album="Other Record", album_id="alb9",
                       titles=["Stray Song"])
    for _ in range(4):                             # a same-artist catalogue confusion, repeatedly
        s.on_identified(resolved_for(other, "Stray Song"), music_start)
    assert s.mode == SessionMode.ALBUM and s.is_locked()
    assert s.get_current_track().title == "Track 1"


def test_different_artists_switch_to_compilation_with_timing(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    a = make_album([200], artist="Someone Else", album="Mix A", album_id="x1", titles=["Alpha"])
    b = make_album([200], artist="Third Act", album="Mix B", album_id="x2", titles=["Beta"])
    clock.advance(70)                              # Track 1 (115 s) played 74 s: over half
    for _ in range(CONFIRM_THRESHOLD):
        out = s.on_identified(resolved_for(a, "Alpha"), music_start)
    assert out == [] and s.mode == SessionMode.ALBUM  # one foreign artist: tolerated
    clock.advance(10)
    for _ in range(CONFIRM_THRESHOLD):
        out = s.on_identified(resolved_for(b, "Beta"), music_start)
    assert s.mode == SessionMode.COMPILATION
    # the album track that had played enough was finalised on the switch
    assert [t.title for t, _ in out] == ["Track 1"]
    # compilation tracks scrobble when they END (next track / silence), with their start time
    assert s.get_current_track().title == "Beta"
    beta_start = s.current_track_start()
    clock.advance(130)
    c = make_album([200], artist="Fourth", album="Mix C", album_id="x3", titles=["Gamma"])
    for _ in range(CONFIRM_THRESHOLD):
        out = s.on_identified(resolved_for(c, "Gamma"), music_start)
    assert [(t.title, ts) for t, ts in out] == [("Beta", beta_start)]
    assert s.comp_scrobbled_count == 1
    clock.advance(20)                              # Gamma: 20 s, too short
    assert s.on_silence() == []


def test_single_upgrades_to_the_album(clock):
    """Locked on a 1-track single, the album containing it (and the next track) takes over."""
    single = make_album([115], artist="Madvillain", album="Track 1 - Single", album_id="sg",
                        titles=["Track 1"])
    full = make_album([115, 72, 109], artist="Madvillain", album="Madvillainy", album_id="alb1")
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, single, "Track 1", music_start)
    assert s.locked_album_id == "sg"
    clock.advance(118)                             # Track 1 is over; the single is exhausted
    s.check_advance()
    assert not s.is_locked() and s.album_state.scrobbled == {0}
    confirmed(s, full, "Track 2", music_start)
    assert s.is_locked() and s.locked_album_id == "alb1"
    assert s.get_current_track().title == "Track 2"
    assert s.album_state.scrobbled == {0}          # Track 1's scrobble carried over by title


def test_streaming_edition_upgrades_to_owned_pressing(clock):
    streaming = make_album([115, 72, 109], album_id="sp1")
    owned = make_album([115, 72, 109], album_id="sp1", positions=["A1", "A2", "B1"], owned=True,
                       source="merged")
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, streaming, "Track 1", music_start)
    start = s.album_state.current_track_start
    # the collection finished loading; the next ID resolves to the merged pressing
    # (same Spotify album id, now with positions): swap the tracklist, keep the clock
    s.on_identified(resolved_for(owned, "Track 1"), music_start)
    assert s.album_state.tracklist is owned and s.album_state.tracklist.owned
    assert s.album_state.current_track_start == start
    assert s.album_state.starts == {0: start}      # per-track starts carried over by title
    assert s.get_current_track().title == "Track 1"
    # never the other way round: a streaming edition does not replace the owned pressing
    s.on_identified(resolved_for(streaming, "Track 1"), music_start)
    assert s.album_state.tracklist is owned


def test_no_album_resolution_leaves_session_untouched(clock):
    s = ScrobbleSession(clock=clock)
    r = Resolved(track=TrackInfo(title="Unknown Song", artist="Nobody"))
    for _ in range(3):
        assert s.on_identified(r, clock.t) == []
    assert s.mode is None


def test_forward_fill_is_bounded_by_the_clock(clock, album):
    """A record that does not follow the tracklist (a bootleg cut, a skipped
    track): gap tracks the clock had no room for never played."""
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    clock.advance(115 + 10 - 4)                    # Track 1 (115 s) done, 10 s into the next one
    out = confirmed(s, album, "Track 4", music_start)   # the fingerprint says Track 4
    assert [(t.title, ts) for t, ts in out] == [("Track 1", music_start)]
    assert s.album_state.current_index == 3        # Tracks 2 and 3: no time for them, not filled
    assert s.album_state.current_track_start == clock.t
    assert s.album_state.scrobbled == {0}


def test_forward_fill_scrobbles_a_cut_short_track_only_if_it_played_enough(clock, album):
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    clock.advance(70 - 4)                          # Track 1 played 70 of 115 s: over half
    out = confirmed(s, album, "Track 3", music_start)
    assert [t.title for t, _ in out] == ["Track 1"]  # it counts; Track 2 never played
    assert s.album_state.current_index == 2

    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    clock.advance(40 - 4)                          # 40 s of 115: under half
    assert confirmed(s, album, "Track 3", music_start) == []
    assert s.album_state.current_index == 2 and s.album_state.scrobbled == set()


def test_lagging_id_of_the_track_that_just_ended_keeps_its_start(clock, album):
    """The capture window trails the audio: Track 1 is heard again right after
    the clock moved on. Nothing re-scrobbles and no timestamp moves."""
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    clock.advance(115 + 5 - 4)
    assert [t.title for t, _ in s.check_advance()] == ["Track 1"]
    assert s.get_current_track().title == "Track 2"
    clock.advance(3)
    assert confirmed(s, album, "Track 1", music_start) == []     # the lagging ID
    assert s.get_current_track().title == "Track 1"
    assert s.album_state.current_track_start == music_start      # start kept, not "now"
    assert s.check_advance() == []                               # re-advances, no second scrobble
    assert s.get_current_track().title == "Track 2"
    t2_start = s.album_state.current_track_start
    assert t2_start == clock.t
    clock.advance(72 + 6)
    out = confirmed(s, album, "Track 3", music_start)
    assert [(t.title, ts) for t, ts in out] == [("Track 2", t2_start)]  # in order, in the past
    assert out[0][1] < clock.t


def test_restarting_the_side_scrobbles_it_again(clock, album):
    """Three tracks play, the needle goes back to the first: a second play."""
    s = ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
    music_start = clock.t
    clock.advance(4)
    confirmed(s, album, "Track 1", music_start)
    for dur in (115, 72, 109):
        clock.advance(dur + 10)
        s.check_advance()
    assert s.album_state.scrobbled == {0, 1, 2}
    assert s.get_current_track().title == "Track 4"
    clock.advance(20)                              # 20 s into Track 4, then the needle goes back
    replay_start = clock.t
    out = s.on_identified(resolved_for(album, "Track 1"), music_start)  # confirmed before: acts
    assert out == []                               # Track 4 (20 s) did not play enough
    assert s.get_current_track().title == "Track 1"
    assert s.album_state.scrobbled == set()
    assert s.album_state.current_track_start == replay_start
    clock.advance(115 + 10)
    out = s.check_advance()
    assert [(t.title, ts) for t, ts in out] == [("Track 1", replay_start)]
    assert s.get_current_track().title == "Track 2"

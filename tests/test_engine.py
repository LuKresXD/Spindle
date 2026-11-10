from conftest import FakeClock, FakeResolver, make_album

from spindle.art import Art
from spindle.config import SpindleConfig
from spindle.engine import Extras, IdentifyWorker, Player
from spindle.fingerprint import TrackInfo
from spindle.session import ScrobbleSession
from spindle.state import Phase


class FakeCapture:
    """Scripted levels; `window_wav` hands back a dummy path."""

    def __init__(self, tmp_path):
        self.level = -60.0
        self.motion_value = 0.4          # moves like music unless a test says otherwise
        self.window_seconds = 6
        self.tmp = tmp_path
        self.running = True
        self.windows = 0

    def level_db(self, seconds=2):
        return self.level

    def motion(self, seconds=None):
        return self.motion_value

    runout_value = (0.0, 0.0)            # (pulse, duty): no lead-out thumps unless a test says so

    def runout(self, seconds=None):
        return self.runout_value

    def buffered_seconds(self):
        return 10.0

    def window_wav(self, seconds=None, min_seconds=2.0):
        self.windows += 1
        p = self.tmp / f"w{self.windows}.wav"
        p.write_bytes(b"RIFF")
        return p


class Recorder:
    """Collects what the engine would send out."""

    def __init__(self):
        self.scrobbles = []
        self.now_playing = []

    # scrobbler
    def scrobble(self, t, timestamp=None):
        self.scrobbles.append((t.title, timestamp))
        return True

    def update_now_playing(self, t):
        self.now_playing.append(t.title)

    def album_playcount(self, artist, album):
        return 7

    def queue_size(self):
        return 0


class FakeHistory:
    def __init__(self):
        self.rows = []
        self.recent = set()                  # titles titles_since() reports

    def log(self, t, ts, source="live"):
        self.rows.append((t.title, source))

    def titles_since(self, artist, since):
        return set(self.recent)


class FakeArt:
    def get(self, artist, album, spotify_url=""):
        return Art(b"\x89PNG-not-really", "test", "u")


def make_player(tmp_path, clock, identify, with_session=True, albums=None):
    cfg = SpindleConfig()
    cfg.silence.threshold_db = -45
    cfg.silence.min_silence_seconds = 8
    albums = albums if albums is not None else [make_album([115, 72, 109], owned=True)]
    resolver = FakeResolver(*albums) if with_session else None
    session = (ScrobbleSession(min_play_seconds=30, chunk_duration=2, clock=clock)
               if with_session else None)
    rec = Recorder()
    cap = FakeCapture(tmp_path)
    extras = Extras()
    extras.album_playcount = rec.album_playcount
    worker = IdentifyWorker(identify, resolver, FakeArt(), threaded=False)
    player = Player(cfg, cap, worker, session=session, scrobbler=rec,
                    history=FakeHistory(), extras=extras, clock=clock, step=2)
    return player, cap, rec


def settle(player):
    """Let the one-worker playcount executor finish."""
    player._bg.shutdown(wait=True)
    from concurrent.futures import ThreadPoolExecutor
    player._bg = ThreadPoolExecutor(max_workers=1)


def test_idle_until_music_then_listening_then_playing(tmp_path):
    clock = FakeClock()

    def identify(path):
        return TrackInfo(title="Track 2", artist="Madvillain", album="Madvillainy", duration=72)

    player, cap, rec = make_player(tmp_path, clock, identify)
    st = player.tick()
    assert st.phase == Phase.IDLE
    cap.level = -20
    assert player.tick().phase == Phase.IDLE        # one loud tick is not a session yet
    clock.advance(2)
    st = player.tick()
    # first ID shows the track right away (not confirmed yet: no tracklist)
    assert st.phase == Phase.PLAYING and st.title == "Track 2" and not st.tracklist
    clock.advance(2)
    st = player.tick()
    settle(player)
    st = player.tick()
    assert st.phase == Phase.PLAYING
    assert st.title == "Track 2" and st.album == "Madvillainy" and st.year == "2004"
    assert [r.title for r in st.tracklist] == ["Track 1", "Track 2", "Track 3"]
    assert st.current_index == 1 and st.track_duration == 72
    assert st.album_playcount == 7
    assert st.art == b"\x89PNG-not-really"
    assert rec.now_playing[-1] == "Track 2"


def test_runout_noise_near_threshold_never_starts_a_session(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}

    def identify(path):
        calls["n"] += 1
        return None

    player, cap, rec = make_player(tmp_path, clock, identify)
    for level in (-44.6, -42.0, -46, -43.5, -41, -44, -40.5, -42):
        cap.level = level
        st = player.tick()
        clock.advance(2)
        assert st.phase == Phase.IDLE
    assert calls["n"] == 0                          # no fingerprinting of noise
    cap.level = -30                                 # real music: two ticks to start
    assert player.tick().phase == Phase.IDLE
    clock.advance(2)
    assert player.tick().phase == Phase.LISTENING
    assert player.music_since == clock.t - 4        # counted from the first loud tick


def test_quiet_pause_does_not_end_session(tmp_path):
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 1", artist="Madvillain", duration=115)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(3):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING
    cap.level = -70                                # a 6 s quiet intro
    for _ in range(3):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING               # still the same session
    assert player.session.is_locked()
    for _ in range(2):                             # now 10 s quiet: over
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE
    assert not player.session.is_locked()


def test_timing_advance_scrobbles_and_identify_slows_when_locked(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}
    def identify(path):
        calls["n"] += 1
        if clock.t - 1_700_000_000.0 < 60:
            return TrackInfo(title="Track 1", artist="Madvillain", duration=115)
        return None
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    ticks = 0
    while clock.t - 1_700_000_000.0 < 140:
        player.tick()
        clock.advance(2)
        ticks += 1
    assert rec.scrobbles and rec.scrobbles[0][0] == "Track 1"
    assert player.state.current_index == 1        # advanced to Track 2 by timing
    assert calls["n"] < ticks / 2                  # locked: far fewer fingerprints than ticks


def test_simple_mode_without_any_tracklist_source(tmp_path):
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Song", artist="Someone", duration=100)
    player, cap, rec = make_player(tmp_path, clock, identify, with_session=False)
    cap.level = -20
    for _ in range(30):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "Song" and not st.tracklist
    assert rec.scrobbles[0][0] == "Song"
    assert st.track_elapsed is not None and st.track_elapsed > 30


def test_unknown_track_then_silence_is_not_scrobbled(tmp_path):
    """Spotify and Discogs know nothing about this record: it is held, then shown in
    simple mode, and the needle lift sends nothing to Last.fm (see the test above)."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="White Label", artist="Nobody", duration=200)
    player, cap, rec = make_player(tmp_path, clock, identify)   # session present, no album
    cap.level = -20
    for _ in range(80):                            # 160 s: past the hold
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING and not player.session.is_locked()
    cap.level = -80
    for _ in range(6):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.IDLE
    assert rec.scrobbles == []                     # unknown to every catalogue: never sent


def test_capture_down_holds_the_session(tmp_path):
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 1", artist="Madvillain", duration=115)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(3):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING
    cap.running = False
    cap.level = -120                               # the buffer was cleared: digital silence
    for _ in range(10):                            # 20 s of "silence" while arecord is dead
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and player.session.is_locked()
    cap.running = True
    cap.level = -20
    assert player.tick().phase == Phase.PLAYING


def test_identification_applies_at_window_time_and_worker_runs_one_job(tmp_path):
    clock = FakeClock()
    seen = []
    def identify(path):
        seen.append(path)
        return TrackInfo(title="Track 1", artist="Madvillain", duration=115)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(8):                             # lock, then one slow-cadence re-identify
        player.tick()
        clock.advance(2)
    # every submitted WAV was deleted by the worker
    assert seen and all(not p.exists() for p in seen)
    assert 2 < len(seen) < 6                       # locked after 2 IDs: 8 s cadence from then on
    ctx = player.worker.resolver.calls[-1][1]
    assert ctx.locked_album_id == "alb1" and ctx.locked_artist == "Madvillain"


def test_hot_input_warns_once(tmp_path, caplog):
    clock = FakeClock()
    def identify(path):
        return None
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -2
    with caplog.at_level("WARNING"):
        for _ in range(4):
            player.tick()
            clock.advance(2)
    assert sum("hot" in r.message for r in caplog.records) == 1


def test_steady_loud_input_stays_idle_and_looks_up_slowly(tmp_path, clock):
    """An empty turntable: loud enough, but the spectrum stands still. No
    session, screen black, a lookup after 20 s then backing off; a drop
    in level ends the run."""
    player, cap, rec = make_player(tmp_path, clock, identify=lambda p: None)
    cap.level = -38.0
    cap.motion_value = 0.05
    t0 = clock.t
    for _ in range(3):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE and st.steady_noise and st.motion == 0.05
    assert player.music_since is None
    assert player.noise_since == t0 - 2                      # the loud run began a step earlier
    assert cap.windows == 0 and rec.now_playing == []
    while clock.t < t0 + 60:
        player.tick()
        clock.advance(2)
    assert cap.windows == 2                                   # lookups at 20 s, then 40 s later
    while clock.t < t0 + 140:
        player.tick()
        clock.advance(2)
    assert cap.windows == 3                                   # the next one 80 s after that
    cap.level = -60.0
    st = player.tick()
    assert not st.steady_noise and player.noise_since is None


def test_steady_input_that_identifies_becomes_a_session(tmp_path, clock):
    track = TrackInfo(title="Drone", artist="Someone", duration=600)
    player, cap, rec = make_player(tmp_path, clock, identify=lambda p: track, with_session=False)
    cap.level = -30.0
    cap.motion_value = 0.05
    t0 = clock.t
    for _ in range(12):                                      # past the first lookup at +20 s
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "Drone"
    assert player.music_since == t0 - 2                      # the session dates from the loud start
    assert not st.steady_noise


def test_music_gate_fails_open_without_a_motion_reading(tmp_path, clock):
    player, cap, rec = make_player(tmp_path, clock, identify=lambda p: None)
    cap.level = -30.0
    cap.motion_value = None
    for _ in range(3):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.LISTENING and not st.steady_noise


def test_ticks_from_recent_history_survive_the_flip(tmp_path, clock):
    """Side A scrobbled Track 1 earlier tonight; on side B's session the row still shows done."""
    player, cap, rec = make_player(tmp_path, clock, identify=lambda p: TrackInfo(
        title="Track 2", artist="Madvillain", album="Madvillainy"))
    player.history.recent = {"Track 1"}
    cap.level = -30.0
    for _ in range(4):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.current_index == 1
    assert st.tracklist[0].scrobbled and not st.tracklist[2].scrobbled
    assert player.session.album_state.scrobbled == set()       # the session itself is clean


def test_disagreeing_id_skips_the_locked_interval(tmp_path, clock):
    """Locked on Track 1 by timing; a hit for Track 3 is not confirmed yet, so the
    next window goes out on the very next tick instead of 8 s later."""
    hits = {"n": 0}

    def identify(p):
        hits["n"] += 1
        return TrackInfo(title="Track 1" if hits["n"] <= 2 else "Track 3", artist="Madvillain",
                         album="Madvillainy")

    player, cap, rec = make_player(tmp_path, clock, identify=identify)
    cap.level = -30.0
    for _ in range(4):                                           # lock on Track 1 (2 IDs)
        player.tick()
        clock.advance(2)
    assert player.session.get_current_track().title == "Track 1"
    before = cap.windows
    for _ in range(5):                                           # 10 s: 1 locked-interval window
        player.tick()
        clock.advance(2)
    assert cap.windows - before >= 2                             # ... plus the hurried confirmer
    assert player.session.get_current_track().title == "Track 3"


def test_hit_nobody_knows_is_held_off_the_screen_and_lastfm(tmp_path):
    """Shazam's catalogue carries user uploads that fingerprint like the record on the
    platter. A hit that neither Spotify nor the collection knows stays off the screen and
    Last.fm until it has been the only answer for a while (a 40 s side of it is nothing),
    and the engine stops asking every two seconds while it waits."""
    clock = FakeClock()
    calls = {"n": 0}
    def identify(path):
        calls["n"] += 1
        return TrackInfo(title="Zion Williamson/ Air Max Mix", artist="Stanvibes")
    player, cap, rec = make_player(tmp_path, clock, identify)     # session, catalogues know nothing
    cap.level = -20
    for _ in range(20):                                           # 40 s of the same nobody
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.LISTENING and not st.title
    assert not rec.now_playing and not rec.scrobbles
    assert calls["n"] <= 12                                  # every 4 s while held, not each tick
    cap.level = -70                                               # needle lifted
    for _ in range(8):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE and not rec.scrobbles and not rec.now_playing


def test_hit_nobody_knows_that_persists_shows_but_is_never_scrobbled(tmp_path):
    """The same unknown answer for long enough reaches the screen (counted from the first
    hearing), but a track that neither Spotify nor the collection knows never goes to
    Last.fm: his records are all in Discogs, so such a hit is a stranger's upload that
    happens to match the side (one matched three minutes of it, 2026-11-08)."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="White Label", artist="Nobody")
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(72):                                           # 144 s: still held
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.LISTENING and not st.title
    for _ in range(8):                                            # 160 s: shown
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "White Label"
    assert st.track_elapsed is not None and st.track_elapsed > 150   # from the first hearing
    assert not rec.now_playing                                    # and not announced to Last.fm
    cap.level = -70
    for _ in range(8):
        player.tick()
        clock.advance(2)
    assert rec.scrobbles == []


def test_known_album_is_not_held_and_clears_a_hold(tmp_path):
    clock = FakeClock()
    first = {"on": True}
    def identify(path):
        if first["on"]:
            return TrackInfo(title="Air Max Mix", artist="Stanvibes")
        return TrackInfo(title="Track 2", artist="Madvillain", album="Madvillainy", duration=72)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(5):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.LISTENING                       # the nobody is held
    first["on"] = False
    for _ in range(8):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "Track 2" and st.album == "Madvillainy"
    assert player.session.is_locked() and player._held is None


def test_lead_out_thumps_end_the_side_and_never_start_one(tmp_path):
    """The needle reaches the lead-out: loud thumps once a revolution, never silence. The
    side ends on them instead of waiting for the needle to be lifted, and the thumps that
    follow do not start a session."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 3", artist="Madvillain", album="Madvillainy", duration=109)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(6):                                                 # two IDs confirm the lock
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING and player.session.is_locked()
    assert player.session.get_current_track().title == "Track 3"       # the side's last track
    ended = []
    player.side_listeners.append(lambda summary: ended.append(summary))
    clock.advance(109 - 40 - 12)                                 # into the last 40 s of Track 3
    player.tick()
    cap.runout_value = (0.8, 0.05)                                     # thump, quiet, thump...
    for _ in range(2):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING                               # two ticks: not yet
    player.tick()
    clock.advance(2)
    assert player.phase == Phase.IDLE and not player.session.is_locked()
    player.worker.identify = lambda path: None                         # thumps identify as nothing
    for _ in range(6):                                                 # the thumps go on
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE and st.steady_noise                  # no new session
    cap.level = -70                                                    # needle lifted
    for _ in range(5):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE and not st.steady_noise


def test_lead_out_thumps_mid_side_do_not_end_it(tmp_path):
    """The clock is on the first track of the side: thumps there are a scratch or a loud
    passage, not the lead-out; the side keeps going. (The last track's own early minutes
    are covered the same way: only its final stretch may end the side.)"""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 1", artist="Madvillain", album="Madvillainy", duration=115)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(6):
        player.tick()
        clock.advance(2)
    assert player.session.is_locked() and player.session.get_current_track().title == "Track 1"
    cap.runout_value = (0.9, 0.05)
    for _ in range(5):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING and player.session.is_locked()


def test_once_the_clock_ran_the_side_out_the_first_quiet_or_thump_tick_ends_it(tmp_path):
    """The last track's duration has elapsed: the side is over as soon as the input goes
    quiet or thumps, not eight seconds later."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 3", artist="Madvillain", album="Madvillainy", duration=109)
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(6):
        player.tick()
        clock.advance(2)
    assert player.session.is_locked() and player.session.get_current_track().title == "Track 3"
    clock.advance(109 + 15)                                            # the clock runs Track 3 out
    player.tick()
    al = player.session.album_state
    assert (al.side_done or al.exhausted) and player.phase == Phase.PLAYING
    cap.level = -70                                                    # quiet: the end, now
    player.tick()
    assert player.phase == Phase.IDLE and [s[0] for s in rec.scrobbles] == ["Track 3"]
    # and the same with the lead-out thumps instead of quiet
    clock2 = FakeClock()
    player2, cap2, rec2 = make_player(tmp_path, clock2, identify)
    cap2.level = -20
    for _ in range(6):
        player2.tick()
        clock2.advance(2)
    clock2.advance(109 + 15)
    player2.tick()
    al2 = player2.session.album_state
    assert al2.side_done or al2.exhausted
    cap2.runout_value = (0.9, 0.05)
    player2.tick()
    assert player2.phase == Phase.IDLE and [s[0] for s in rec2.scrobbles] == ["Track 3"]


def test_a_first_hit_not_in_the_collection_waits_before_it_shows(tmp_path):
    """Shazam's answer at a needle drop can be a stranger's upload that Spotify knows and
    that keeps coming for half a minute: a hit that is not one of his records reaches the
    screen only after 45 s of it. The session still locks on it underneath."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Track 2", artist="Madvillain", album="Madvillainy", duration=72)
    streaming_only = make_album([115, 72, 109], owned=False)
    player, cap, rec = make_player(tmp_path, clock, identify, albums=[streaming_only])
    cap.level = -20
    for _ in range(22):                                      # 44 s
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.LISTENING and not st.title and not rec.now_playing
    assert player.session.is_locked()                        # heard, locked, not shown
    for _ in range(3):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "Track 2"


def test_a_stranger_displaced_by_the_lock_is_not_a_play(tmp_path):
    """The needle drop reads as a stranger's Spotify track for a while, then the album locks:
    the stranger is a confusion, not a play, and never reaches Last.fm."""
    clock = FakeClock()
    calls = {"n": 0}
    def identify(path):
        calls["n"] += 1
        if calls["n"] <= 4:                                 # the first half minute: the stranger
            return TrackInfo(title="Ken Car", artist="TreyWayG", duration=70)
        return TrackInfo(title="Track 1", artist="Madvillain", album="Madvillainy", duration=115)
    stranger = make_album([70], artist="TreyWayG", album="Ken Car", album_id="stranger",
                          titles=["Ken Car"], owned=False)
    his = make_album([115, 72, 109], owned=True)
    player, cap, rec = make_player(tmp_path, clock, identify, albums=[stranger, his])
    cap.level = -20
    for _ in range(20):
        player.tick()
        clock.advance(2)
    assert player.session.is_locked() and player.state.title == "Track 1"   # his record took over
    assert "Ken Car" not in rec.now_playing and rec.now_playing[0] == "Track 1"   # it never showed
    assert "Ken Car" not in [s[0] for s in rec.scrobbles]
    cap.level = -70
    for _ in range(6):
        player.tick()
        clock.advance(2)
    assert "Ken Car" not in [s[0] for s in rec.scrobbles]


# ── 2026-11-09 review: the regressions it found, each reproduced before it was fixed ─────────────

SIDES = ["A1", "A2", "A3", "B1", "B2", "B3"]


def run(player, clock, seconds, step=2):
    st = None
    for _ in range(int(seconds / step)):
        clock.advance(step)
        st = player.tick()
    return st


def test_an_identification_finishing_after_the_side_ended_is_not_applied_to_the_next(tmp_path):
    """A job in flight when the side ends finishes later. Applied to the next needle drop it
    announced side A's last track at the start of side B and scrobbled it a second time."""
    from conftest import resolved_for

    from spindle.engine import IdentifyResult
    clock = FakeClock()
    album = make_album([115, 72, 109, 68, 150, 234], positions=SIDES, owned=True)
    heard = {"t": "Track 3"}
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo(heard["t"], "Madvillain"),
                                   albums=[album])
    cap.level = -20
    run(player, clock, 130)                                   # Track 3, to the end of side A
    cap.level = -60
    run(player, clock, 2)                                     # the first quiet tick ends the side
    assert player.phase == Phase.IDLE and [t for t, _ in rec.scrobbles] == ["Track 3"]
    late = IdentifyResult(at=clock.t - 3, raw=TrackInfo("Track 3", "Madvillain"),
                          resolved=resolved_for(album, "Track 3"))
    player.worker._results.put(late)
    run(player, clock, 400)                                   # a slow flip
    heard["t"] = "Track 4"
    cap.level = -20
    st = run(player, clock, 20)                               # side B
    assert [t for t, _ in rec.scrobbles] == ["Track 3"]       # no second scrobble
    assert "Track 3" not in rec.now_playing[1:] or rec.now_playing[-1] == "Track 4"
    assert st.title == "Track 4"


def test_run_out_crackle_after_a_side_is_no_new_session_and_the_end_card_stays(tmp_path):
    """The side ends on its first quiet tick with the needle still in the run-out; the crackle
    there clears the start gate and "moves" more than music. It started a phantom session
    seconds later and the end-of-side card went with it (four times on 2026-11-08)."""
    from spindle.web import StateHub
    clock = FakeClock()
    album = make_album([115, 72, 109, 68, 150, 234], positions=SIDES, owned=True)
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo("Track 3", "Madvillain"),
                                   albums=[album])
    hub = StateHub(clock=clock)
    player.side_listeners.append(hub.side_ended)
    cap.level = -20
    for _ in range(65):
        clock.advance(2)
        hub.publish(player.tick())
    cap.level = -60
    clock.advance(2)
    hub.publish(player.tick())
    assert "ended" in hub.snapshot()[1]
    player.worker.identify = lambda p: None
    cap.level, cap.motion_value = -33, 0.98                   # crackle: loud enough, lively enough
    for _ in range(15):                                       # half a minute of it
        clock.advance(2)
        st = player.tick()
        hub.publish(st)
        assert st.phase == Phase.IDLE and "ended" in hub.snapshot()[1]
    # the needle comes up (the floor, not just under the gate), then side B goes on
    cap.level, cap.motion_value = -66, 0.4
    run(player, clock, 6)
    player.worker.identify = lambda p: TrackInfo("Track 4", "Madvillain")
    cap.level = -20
    st = run(player, clock, 8)
    assert st.phase == Phase.PLAYING and st.title == "Track 4"


def test_music_that_goes_on_after_a_side_end_is_found_without_the_needle_coming_up(tmp_path):
    """The clock ended the side early (a hidden track, a slow deck): the sound that follows is
    identified within seconds even though the needle never left the groove."""
    clock = FakeClock()
    album = make_album([115, 72, 109, 68, 150, 234], positions=SIDES, owned=True)
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo("Track 3", "Madvillain"),
                                   albums=[album])
    cap.level = -20
    run(player, clock, 130)
    cap.level = -60
    run(player, clock, 2)
    assert player.phase == Phase.IDLE
    cap.level = -20                                           # it was only a pause
    st = run(player, clock, 16)
    assert st.phase == Phase.PLAYING and st.title == "Track 3"
    assert [t for t, _ in rec.scrobbles] == ["Track 3"]       # and not counted twice


def test_a_sparse_intro_reading_as_a_lead_out_does_not_end_an_unidentified_session(tmp_path):
    """Runaway's opening notes come about a revolution apart: three such ticks ended the
    session twice, 36 s into the side, while nothing had been identified yet."""
    clock = FakeClock()
    player, cap, rec = make_player(tmp_path, clock, lambda p: None)
    cap.level = -24
    run(player, clock, 4)
    assert player.phase == Phase.LISTENING
    cap.runout_value = (0.7, 0.06)
    st = run(player, clock, 30)
    assert st.phase == Phase.LISTENING and not st.steady_noise


def test_a_wrong_lock_on_one_of_his_records_yields_to_the_one_that_keeps_being_heard(tmp_path):
    """Locked on a record he owns by a confusion at the needle drop, every hit from the record
    really playing (also his) was ignored for the whole side and the clock scrobbled the wrong
    record's tracks."""
    clock = FakeClock()
    wrong = make_album([200, 200, 200], artist="Kino", album="Nachalnik Kamchatki", album_id="w",
                       positions=["A1", "A2", "A3"], owned=True,
                       titles=["Posledniy Geroy", "Kazhduyu Noch", "Tranquilizer"])
    real = make_album([200, 200, 200], artist="Kino", album="Noch", album_id="r",
                      positions=["B1", "B2", "B3"], owned=True,
                      titles=["Posledniy Geroy", "Zhizn V Steklah", "Mama Anarhiya"])
    heard = {"t": "Posledniy Geroy"}
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo(heard["t"], "Kino"),
                                   albums=[wrong, real])
    cap.level = -20
    run(player, clock, 196)                                   # the song both records carry
    assert player.session.album_state.tracklist.album_name == "Nachalnik Kamchatki"
    heard["t"] = "Zhizn V Steklah"                            # only on the record that is playing
    run(player, clock, 60)
    assert player.session.album_state.tracklist.album_name == "Noch"
    run(player, clock, 400)
    titles = [t for t, _ in rec.scrobbles]
    assert "Kazhduyu Noch" not in titles and "Tranquilizer" not in titles
    assert titles[:2] == ["Posledniy Geroy", "Zhizn V Steklah"]


def test_the_end_card_counts_what_was_sent_not_the_sessions_bookkeeping(tmp_path):
    """A backwards anchor takes tracks out of the session's scrobbled set so they can count
    again; the end-of-side card then said "1 track scrobbled" after three had gone out."""
    clock = FakeClock()
    album = make_album([60, 60, 60], positions=["A1", "A2", "A3"], owned=True)
    heard = {"t": "Track 1"}
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo(heard["t"], "Madvillain"),
                                   albums=[album])
    ended = []
    player.side_listeners.append(ended.append)
    cap.level = -20
    run(player, clock, 58)
    heard["t"] = "Track 2"
    run(player, clock, 60)
    heard["t"] = "Track 3"
    run(player, clock, 70)
    assert [t for t, _ in rec.scrobbles] == ["Track 1", "Track 2", "Track 3"]
    player.session.album_state.scrobbled.discard(1)           # what a replay branch does
    player.session.album_state.scrobbled.discard(2)
    cap.level = -70
    run(player, clock, 12)
    assert ended and ended[-1]["scrobbled"] == 3


def test_the_side_ends_when_the_level_falls_far_below_its_music_not_only_on_silence(tmp_path):
    """A run-out groove at -44 dB is a dB over the silence threshold: the screen sat on "0 s left"
    for 12 s until the thump detector agreed (2026-11-09). The music had plainly stopped."""
    clock = FakeClock()
    album = make_album([115, 72, 109, 68, 150, 234], positions=SIDES, owned=True)
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo("Track 3", "Madvillain"),
                                   albums=[album])
    ended = []
    player.side_listeners.append(ended.append)
    cap.level = -18
    st = run(player, clock, 104)                              # into the last seconds of side A
    assert st.phase == Phase.PLAYING and not ended
    player.worker.identify = lambda p: None
    cap.level = -44                                           # the groove: not "quiet", not music
    st = run(player, clock, 2)
    assert st.phase == Phase.IDLE and ended                   # over on the first such tick
    assert [t for t, _ in rec.scrobbles] == ["Track 3"]


def test_a_quiet_passage_in_the_middle_of_the_last_track_does_not_end_the_side(tmp_path):
    clock = FakeClock()
    album = make_album([115, 72, 109, 68, 150, 234], positions=SIDES, owned=True)
    player, cap, rec = make_player(tmp_path, clock, lambda p: TrackInfo("Track 3", "Madvillain"),
                                   albums=[album])
    cap.level = -18
    run(player, clock, 50)
    cap.level = -40                                           # a breakdown a minute before the end
    st = run(player, clock, 6)
    assert st.phase == Phase.PLAYING

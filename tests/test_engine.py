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
    albums = albums if albums is not None else [make_album([115, 72, 109])]
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


def test_unknown_track_then_silence_still_scrobbles_it(tmp_path):
    """Spotify and Discogs know nothing about this record: simple mode, and the
    needle lift finalises the track (this used to be lost when a session existed)."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="White Label", artist="Nobody", duration=200)
    player, cap, rec = make_player(tmp_path, clock, identify)   # session present, no album
    cap.level = -20
    for _ in range(60):                            # 120 s: over half of 200 s
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.PLAYING and not player.session.is_locked()
    cap.level = -80
    for _ in range(6):
        player.tick()
        clock.advance(2)
    assert player.phase == Phase.IDLE
    assert [t for t, _ in rec.scrobbles] == ["White Label"]


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
    assert calls["n"] <= 6                                        # ~every 8 s, not every tick
    cap.level = -70                                               # needle lifted
    for _ in range(8):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.IDLE and not rec.scrobbles and not rec.now_playing


def test_hit_nobody_knows_that_persists_is_a_real_record(tmp_path):
    """The same unknown answer for long enough is an obscure record: it shows, and it
    scrobbles from when it was first heard, not from when the hold ended."""
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="White Label", artist="Nobody")
    player, cap, rec = make_player(tmp_path, clock, identify)
    cap.level = -20
    for _ in range(50):                                           # 100 s
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "White Label" and rec.now_playing
    assert st.track_elapsed is not None and st.track_elapsed > 90   # counted from the first hearing
    for _ in range(15):                                           # 130 s in all
        player.tick()
        clock.advance(2)
    cap.level = -70
    for _ in range(8):
        player.tick()
        clock.advance(2)
    assert [s[0] for s in rec.scrobbles] == ["White Label"]      # unknown length: two minutes count
    assert rec.scrobbles[0][1] is not None and rec.scrobbles[0][1] <= 1_700_000_000.0 + 6


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

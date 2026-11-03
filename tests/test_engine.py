from conftest import FakeClock, FakeSpotify, make_album

from spindle.art import Art
from spindle.config import SpindleConfig
from spindle.engine import Extras, Player
from spindle.fingerprint import TrackInfo
from spindle.session import ScrobbleSession
from spindle.state import Phase


class FakeCapture:
    """Scripted levels; `window_wav` hands back a dummy path."""

    def __init__(self, tmp_path):
        self.level = -60.0
        self.window_seconds = 6
        self.tmp = tmp_path

    def level_db(self, seconds=2):
        return self.level

    def buffered_seconds(self):
        return 10.0

    def window_wav(self, seconds=None, min_seconds=2.0):
        p = self.tmp / "w.wav"
        p.write_bytes(b"RIFF")
        return p


class Recorder:
    """Collects what the engine would send out."""

    def __init__(self):
        self.scrobbles = []
        self.now_playing = []
        self.events = []

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

    # notifier
    def __getattr__(self, name):
        def ev(*a, **k):
            self.events.append((name, a))
        return ev


class FakeHistory:
    def __init__(self):
        self.rows = []

    def log(self, t, ts, source="live"):
        self.rows.append((t.title, source))


class FakeArt:
    def get(self, artist, album, spotify_url=""):
        return Art(b"\x89PNG-not-really", "test", "u")


def make_player(tmp_path, clock, identify, with_session=True):
    cfg = SpindleConfig()
    cfg.silence.threshold_db = -45
    cfg.silence.min_silence_seconds = 8
    album = make_album([115, 72, 109])
    sp = FakeSpotify(album)
    session = (ScrobbleSession(sp, min_play_seconds=30, chunk_duration=2, clock=clock)
               if with_session else None)
    rec = Recorder()
    cap = FakeCapture(tmp_path)
    extras = Extras()
    extras.album_playcount = rec.album_playcount
    player = Player(cfg, cap, identify, session=session, spotify=sp if with_session else None,
                    scrobbler=rec, notifier=rec, history=FakeHistory(), art=FakeArt(),
                    extras=extras, clock=clock, step=2)
    return player, cap, rec


def test_idle_until_music_then_listening_then_playing(tmp_path):
    clock = FakeClock()
    hits = {"n": 0}

    def identify(path):
        hits["n"] += 1
        return TrackInfo(title="Track 2", artist="Madvillain", album="Madvillainy", duration=72)

    player, cap, rec = make_player(tmp_path, clock, identify)
    st = player.tick()
    assert st.phase == Phase.IDLE
    cap.level = -20
    assert player.tick().phase == Phase.IDLE        # one loud tick is not a session yet
    clock.advance(2)
    st = player.tick()
    # first ID shows the track right away (no tracklist yet: not confirmed)
    assert st.phase == Phase.PLAYING and st.title == "Track 2" and not st.tracklist
    clock.advance(2)
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
    assert ("side_finished", ) not in rec.events  # nothing scrobbled (too short)


def test_timing_advance_scrobbles_and_identify_slows_when_locked(tmp_path):
    clock = FakeClock()
    calls = {"n": 0}
    def identify(path):
        calls["n"] += 1
        # fingerprints agree with the record for the first minute, then nothing matches
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


def test_simple_mode_without_spotify(tmp_path):
    clock = FakeClock()
    def identify(path):
        return TrackInfo(title="Song", artist="Someone", duration=100)
    player, cap, rec = make_player(tmp_path, clock, identify, with_session=False)
    cap.level = -20
    for _ in range(30):
        st = player.tick()
        clock.advance(2)
    assert st.phase == Phase.PLAYING and st.title == "Song" and not st.tracklist
    assert rec.scrobbles == [("Song", 1_700_000_000.0 - 0)] or rec.scrobbles[0][0] == "Song"

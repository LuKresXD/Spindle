import logging

import pylast

from spindle.config import LastFMConfig, ScrobbleConfig, _parse_config
from spindle.fingerprint import TrackInfo
from spindle.scrobbler import Scrobbler, _classify


class FlakyNetwork:
    """Scripted Last.fm: each call pops the next behaviour."""

    def __init__(self, script):
        self.script = list(script)
        self.sent = []

    def scrobble(self, **kw):
        action = self.script.pop(0) if self.script else "ok"
        if action == "ok":
            self.sent.append(kw["title"])
        elif action == "down":
            raise pylast.NetworkError(self, "connection refused")
        elif action == "reject":
            raise pylast.WSError(self, "6", "Invalid parameters")
        elif action == "busy":
            raise pylast.WSError(self, "16", "temporarily unavailable")

    def update_now_playing(self, **kw):
        self.sent.append("np:" + kw["title"])


def make(tmp_path, script):
    s = Scrobbler(LastFMConfig(api_key="k", api_secret="s", username="u"),
                  ScrobbleConfig(dedup_window=300), queue_path=tmp_path / "q.json",
                  threaded=False, on_error=lambda t, d: errors.append(f"{t}: {d}"))
    s.network = FlakyNetwork(script)
    return s


errors: list = []


def test_classification():
    net = FlakyNetwork([])
    assert _classify(pylast.NetworkError(net, "x")) == "retry"
    assert _classify(pylast.WSError(net, "16", "x")) == "retry"
    assert _classify(pylast.WSError(net, "29", "x")) == "retry"
    assert _classify(pylast.WSError(net, "6", "x")) == "reject"
    assert _classify(pylast.WSError(net, "4", "auth")) == "auth"      # the credentials, not a play
    assert _classify(pylast.WSError(net, "9", "bad session")) == "auth"
    assert _classify(TimeoutError()) == "retry"


def test_network_failure_queues_and_rejection_drops(tmp_path):
    errors.clear()
    s = make(tmp_path, ["down", "reject", "ok"])
    t = TrackInfo(title="A", artist="X", source="spotify_album")
    assert s.scrobble(t, 100)                      # accepted -> network down -> queued
    assert s.queue_size() == 1
    assert s.scrobble(TrackInfo(title="B", artist="X", source="spotify_album"), 200)
    assert s.queue_size() == 1 and s.failed == 1 and errors and "rejected" in errors[0].lower()
    assert s.scrobble(TrackInfo(title="C", artist="X", source="spotify_album"), 300)
    # a success proves the network is back: the queue is flushed right behind it
    assert s.network.sent == ["C", "A"] and s.queue_size() == 0


def test_flush_stops_at_first_network_error_keeps_order(tmp_path):
    s = make(tmp_path, [])
    for i, title in enumerate(["A", "B", "C"]):
        s._queue.add("X", title, "", 0, 100 + i)
    s.network.script = ["ok", "down"]
    s.flush_queue()
    assert s.network.sent == ["A"]
    assert [e["title"] for e in s._queue._queue] == ["B", "C"]   # B put back first, order kept
    s.network.script = ["reject", "ok"]
    s.flush_queue()
    assert s.network.sent == ["A", "C"] and s.queue_size() == 0 and s.failed == 1


def test_the_same_play_is_sent_once_and_now_playing_cools_down(tmp_path):
    s = make(tmp_path, [])
    t = TrackInfo(title="A", artist="X", source="spotify_album")
    assert s.scrobble(t, 1) and not s.scrobble(t, 2)
    s.update_now_playing(t)
    s.update_now_playing(t)
    assert s.network.sent == ["A", "np:A"]


def test_a_replay_is_another_play_but_a_start_inside_the_last_play_is_not(tmp_path):
    """The needle went back: the same track, a full track later, is a second scrobble (the old
    300 s rule dropped it). The same track starting again inside its own length is one play
    reported twice (Last.fm ignores those as well)."""
    s = make(tmp_path, [])
    t = TrackInfo(title="Stronger", artist="Kanye West", duration=200, source="spotify_album")
    assert s.scrobble(t, 1_700_000_000)
    assert s.scrobble(t, 1_700_000_210)                 # replayed from the top
    assert not s.scrobble(t, 1_700_000_300)             # 90 s into that play: the same play
    assert s.network.sent == ["Stronger", "Stronger"]


def test_a_flush_keeps_on_disk_what_it_has_not_sent(tmp_path):
    """The queue used to be written empty before its entries were sent."""
    import json
    s = make(tmp_path, ["down", "down", "ok", "down"])
    for i, name in enumerate(["A", "B"]):
        s.scrobble(TrackInfo(title=name, artist="X", source="spotify_album"), 100 + i * 1000)
    assert s.queue_size() == 2
    s._do_flush()                                       # A goes through, B meets the network down
    on_disk = json.loads((tmp_path / "q.json").read_text())
    assert [e["title"] for e in on_disk] == ["B"] and s.network.sent == ["A"]


def test_an_auth_refusal_keeps_the_play(tmp_path):
    errors.clear()
    s = make(tmp_path, [])
    s.network.script = None

    def refuse(**kw):
        raise pylast.WSError(s.network, "9", "Invalid session key")
    s.network.scrobble = refuse
    s.scrobble(TrackInfo(title="A", artist="X", source="spotify_album"), 1)
    assert s.queue_size() == 1 and s.failed == 0
    assert errors and "credentials" in errors[0]


def test_config_ignores_unknown_keys_and_reads_session_key(caplog):
    raw = {
        "lastfm": {"api_key": "k", "api_secret": "s", "username": "u", "session_key": "sk",
                   "typo_key": 1},
        "silence": {"threshold_db": -40, "min_silence_seconds": None},
        "bogus_section": {"a": 1},
        "fingerprint": {"corrections": "corrections.yaml"},
    }
    with caplog.at_level(logging.WARNING):
        cfg = _parse_config(raw)
    assert cfg.lastfm.session_key == "sk" and cfg.lastfm.api_key == "k"
    assert cfg.silence.threshold_db == -40 and cfg.silence.min_silence_seconds == 8.0
    assert cfg.fingerprint.corrections == "corrections.yaml"
    msgs = " ".join(r.message for r in caplog.records)
    assert "typo_key" in msgs and "bogus_section" in msgs


def test_a_scrobble_last_fm_ignores_is_reported_not_logged_as_sent(tmp_path, caplog):
    from spindle import scrobbler as mod
    errors.clear()
    s = make(tmp_path, [])

    def ignore(**kw):
        raise mod.Ignored("1")
    s.network.scrobble = ignore
    with caplog.at_level(logging.INFO):
        s.scrobble(TrackInfo(title="1Train", artist="ASAP Rocky", source="discogs"), 1)
    assert s.sent == 0 and s.failed == 1 and s.queue_size() == 0
    assert errors and "did not count" in errors[0] and "artist ignored" in errors[0]
    assert not any("Scrobbled:" in r.message for r in caplog.records)


def test_the_raw_scrobble_reads_the_verdict(monkeypatch):
    from xml.dom import minidom

    from spindle import scrobbler as mod

    class Req:
        answer = ""

        def __init__(self, network, method, params):
            self.params = params

        def sign_it(self):
            pass

        def execute(self, cacheable):
            return minidom.parseString(Req.answer)

    monkeypatch.setattr(mod.pylast, "_Request", Req)
    wrap = ('<lfm status="ok"><scrobbles accepted="{a}" ignored="{i}">'
            '<scrobble>{m}</scrobble></scrobbles></lfm>')
    ok = wrap.format(a=1, i=0, m='<ignoredMessage code="0"/>')
    no = wrap.format(a=0, i=1, m='<ignoredMessage code="1">x</ignoredMessage>')
    Req.answer = ok
    mod._raw_scrobble(None, "A", "B", 1, "", 0)
    Req.answer = no
    try:
        mod._raw_scrobble(None, "A", "B", 1, "", 0)
        raise AssertionError("an ignored play must raise")
    except mod.Ignored as e:
        assert e.code == "1"

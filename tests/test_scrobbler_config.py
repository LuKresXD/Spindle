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
                  threaded=False, on_error=lambda m: errors.append(m))
    s.network = FlakyNetwork(script)
    return s


errors: list = []


def test_classification():
    net = FlakyNetwork([])
    assert _classify(pylast.NetworkError(net, "x")) == "retry"
    assert _classify(pylast.WSError(net, "16", "x")) == "retry"
    assert _classify(pylast.WSError(net, "29", "x")) == "retry"
    assert _classify(pylast.WSError(net, "6", "x")) == "reject"
    assert _classify(pylast.WSError(net, "4", "auth")) == "reject"
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


def test_dedup_window_and_now_playing_cooldown(tmp_path):
    s = make(tmp_path, [])
    t = TrackInfo(title="A", artist="X", source="spotify_album")
    assert s.scrobble(t, 1) and not s.scrobble(t, 2)
    s.update_now_playing(t)
    s.update_now_playing(t)
    assert s.network.sent == ["A", "np:A"]


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

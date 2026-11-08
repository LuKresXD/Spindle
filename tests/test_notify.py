"""The Telegram log channel: start cards, error cards, the warning digest, the run record."""
import logging

from spindle.config import TelegramConfig
from spindle.notify import DIGEST_GAP, DIGEST_WINDOW, Notifier, RunRecord


def make(clock):
    sent = []
    n = Notifier(TelegramConfig(bot_token="t", chat_id="c"), clock=lambda: clock["t"],
                 send=lambda text, silent: sent.append((text, silent)))
    return n, sent


def _join(sent):
    import time
    for _ in range(50):                       # the sends run on threads
        if sent:
            return
        time.sleep(0.01)


def test_start_cards_say_how_spindle_came_back():
    clock = {"t": 1_700_000_000.0}
    n, sent = make(clock)
    n.started({"reason": "restarted", "stopped_for": 6, "version": "0.4.0", "display": True,
               "records": 87, "gain_db": 14})
    _join(sent)
    text, silent = sent[0]
    assert text.startswith("🟢 <b>Spindle is back</b>") and "stopped for 6 s" in text
    assert "v0.4.0" in text and "Display on · 87 records on the shelf · capture 14 dB" in text
    assert silent is True                      # logs do not buzz by default
    sent.clear()
    n.started({"reason": "crashed"})
    _join(sent)
    assert "after a crash" in sent[0][0]
    sent.clear()
    n.started({"reason": "booted", "dry_run": True})
    _join(sent)
    assert sent[0][0].startswith("🧪")


def test_error_card_escapes_and_clips():
    n, sent = make({"t": 1_700_000_000.0})
    n.error("Last.fm rejected a scrobble", "Kanye West - Stronger · <bad> " + "x" * 500)
    _join(sent)
    text = sent[0][0]
    assert text.startswith("🔴 <b>Last.fm rejected a scrobble</b>")
    assert "&lt;bad&gt;" in text and text.count("x") < 420 and "…" in text
    n.cfg.errors = False
    sent.clear()
    n.error("quiet", "")
    assert not sent


def test_warning_digest_batches_counts_and_rate_limits():
    clock = {"t": 1_700_000_000.0}
    n, sent = make(clock)
    n.warning("discogs: master for Graduation failed: timeout")
    n.warning("discogs: master for Graduation failed: timeout")
    clock["t"] += 30
    n.warning("web: cover download failed for 242785")
    assert not sent                            # nothing until the window closes
    n._timer.cancel()
    n.flush_warnings()
    _join(sent)
    text, silent = sent[0]
    assert text.startswith("⚠️ <b>3 warnings</b>") and "×2" in text and silent is True
    assert text.count("\n• ") == 2
    # the next digest waits for the gap, not just the window
    sent.clear()
    clock["t"] += 10
    n.warning("late one")
    assert n._timer is not None and n._timer.interval >= DIGEST_GAP - 10 - DIGEST_WINDOW
    n._timer.cancel()
    n.flush_warnings()
    _join(sent)
    assert sent[0][0].startswith("⚠️ <b>1 warning</b>")


def test_logging_handler_feeds_only_spindle_warnings():
    n, sent = make({"t": 1_700_000_000.0})
    h = n.logging_handler()
    log = logging.getLogger("spindle.discogs")
    log.addHandler(h)
    try:
        log.warning("Discogs: master for %s failed: %s", "45", "boom")
        log.info("quiet")
        logging.getLogger("urllib3.x").addHandler(h)
        logging.getLogger("urllib3.x").warning("not ours")
    finally:
        log.removeHandler(h)
    assert n._pending and n._pending[0][1] == "discogs: Discogs: master for 45 failed: boom"
    assert len(n._pending) == 1
    n._timer.cancel()


def test_run_record_tells_boot_restart_and_crash(tmp_path):
    clock = {"t": 1_700_000_000.0}
    up = {"s": 5000.0}
    rr = RunRecord(tmp_path / "run.json", clock=lambda: clock["t"], uptime=lambda: up["s"])
    assert rr.begin()["reason"] == "started"              # nothing known
    rr.end()
    clock["t"] += 12
    facts = rr.begin()
    assert facts["reason"] == "restarted" and facts["stopped_for"] == 12
    clock["t"] += 100                                       # no end(): the run died
    assert rr.begin()["reason"] == "crashed"
    up["s"] = 40.0
    assert rr.begin()["reason"] == "booted"                # right after a boot, whatever before

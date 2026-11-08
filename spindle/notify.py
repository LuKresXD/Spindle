"""Telegram as Spindle's log channel.

Nothing conversational lives here any more: no commands, no now-playing cards, no
side summaries (the display and Last.fm have those). The bot posts only what a
log would, each as one short card in the same shape:

    🟢 <b>Spindle is back</b>
    <i>restarted · stopped for 6 s · 12:40</i>
    Display on · 87 records on the shelf · capture 14 dB

  started    once per start: up after a boot, back after a stop, back after a crash
  error      at once: something was dropped or failed for good (a rejected scrobble)
  warning    as a digest: WARNING log lines, batched for a minute, at most one card
             every ten minutes, repeats counted
  recovered  the offline queue delivered

Everything goes out on a background thread; the engine never waits on Telegram.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import requests

from .config import TelegramConfig

logger = logging.getLogger(__name__)

DIGEST_WINDOW = 60.0        # a first warning waits this long for company
DIGEST_GAP = 600.0          # and no two digests closer than this
DIGEST_LINES = 8            # lines shown; the rest is a count
LINE_MAX = 160              # characters per digest line
BOOT_WINDOW = 180.0         # a start this soon after boot is "booted", not "restarted"
DEFAULT_RUN_PATH = Path.home() / ".local" / "share" / "spindle" / "run.json"


class Notifier:
    """The log channel: start cards, error cards, warning digests, recoveries."""

    def __init__(self, cfg: TelegramConfig, clock: Callable[[], float] = time.time,
                 send: Callable[[str, bool], None] | None = None):
        self.cfg = cfg
        self.enabled = bool(cfg.bot_token and cfg.chat_id)
        self._clock = clock
        self._deliver = send or self._post
        self._lock = threading.Lock()
        self._pending: list[tuple[float, str]] = []
        self._last_digest = 0.0
        self._timer: threading.Timer | None = None
        if not self.enabled:
            logger.info("Telegram log channel disabled (no bot_token/chat_id)")

    # ── transport ────────────────────────────────────────────────────────────

    def _post(self, text: str, silent: bool) -> None:
        try:
            resp = requests.post(f"https://api.telegram.org/bot{self.cfg.bot_token}/sendMessage",
                                 json={"chat_id": self.cfg.chat_id, "text": text,
                                       "parse_mode": "HTML", "disable_notification": silent,
                                       "disable_web_page_preview": True}, timeout=10)
            if not resp.ok:
                logger.debug("Telegram sendMessage failed: %s", resp.text)
        except Exception as e:
            logger.debug("Telegram sendMessage error: %s", e)

    def _send(self, text: str, silent: bool | None = None) -> None:
        if not self.enabled:
            return
        quiet = self.cfg.silent if silent is None else silent
        threading.Thread(target=self._deliver, args=(text, quiet), name="telegram",
                         daemon=True).start()

    def _clock_text(self, at: float | None = None) -> str:
        return datetime.fromtimestamp(at or self._clock()).strftime("%H:%M")

    # ── cards ────────────────────────────────────────────────────────────────

    def started(self, facts: dict) -> None:
        """One card per start. `facts`: reason (booted | restarted | crashed | started),
        stopped_for (seconds, after a clean stop), version, display (bool),
        records (int | None), gain_db (float | None), dry_run (bool)."""
        reason = facts.get("reason", "started")
        when = self._clock_text()
        if facts.get("dry_run"):
            head, sub = "🧪 <b>Spindle in dry run</b>", f"identifying only · {when}"
        elif reason == "crashed":
            head = "🟠 <b>Spindle is back after a crash</b>"
            sub = f"the last run ended without a stop · {when}"
        elif reason == "restarted":
            gap = facts.get("stopped_for")
            gap_text = f"stopped for {_duration(gap)} · " if gap is not None else ""
            head, sub = "🟢 <b>Spindle is back</b>", f"restarted · {gap_text}{when}"
        elif reason == "booted":
            head, sub = "🟢 <b>Spindle is up</b>", f"booted · {when}"
        else:
            head, sub = "🟢 <b>Spindle is up</b>", f"started · {when}"
        if facts.get("version"):
            sub = f"v{facts['version']} · {sub}"
        bits = []
        if "display" in facts:
            bits.append("Display on" if facts["display"] else "Display off")
        if facts.get("records") is not None:
            n = facts["records"]
            bits.append(f"{n} record{'s' if n != 1 else ''} on the shelf")
        if facts.get("gain_db") is not None:
            bits.append(f"capture {facts['gain_db']:g} dB")
        lines = [head, f"<i>{_esc(sub)}</i>"]
        if bits:
            lines.append(_esc(" · ".join(bits)))
        self._send("\n".join(lines))

    def error(self, title: str, detail: str = "") -> None:
        """Something failed for good: one card, at once."""
        if not self.cfg.errors:
            return
        lines = [f"🔴 <b>{_esc(title)}</b>"]
        if detail:
            lines.append(_esc(_clip(detail, 400)))
        lines.append(f"<i>{self._clock_text()}</i>")
        self._send("\n".join(lines))

    def recovered(self, count: int) -> None:
        """The offline queue got through."""
        if count <= 0:
            return
        self._send(f"📡 <b>Back online</b> · {count} queued scrobble{'s' if count != 1 else ''} "
                   f"delivered · <i>{self._clock_text()}</i>")

    # ── warnings: a digest ───────────────────────────────────────────────────

    def warning(self, line: str) -> None:
        """Queue a WARNING for the next digest."""
        if not self.cfg.errors or not self.enabled:
            return
        now = self._clock()
        with self._lock:
            self._pending.append((now, _clip(line, LINE_MAX)))
            if self._timer is None:
                wait = max(DIGEST_WINDOW, self._last_digest + DIGEST_GAP - now)
                self._timer = threading.Timer(wait, self.flush_warnings)
                self._timer.daemon = True
                self._timer.start()

    def flush_warnings(self) -> None:
        """Send the pending warnings as one card (the timer calls this; tests may too)."""
        with self._lock:
            pending, self._pending = self._pending, []
            self._timer = None
            if not pending:
                return
            self._last_digest = self._clock()
        counts: dict[str, int] = {}
        for _, line in pending:
            counts[line] = counts.get(line, 0) + 1
        first, last = pending[0][0], pending[-1][0]
        span = self._clock_text(first) if len(pending) == 1 or self._clock_text(first) == \
            self._clock_text(last) else f"{self._clock_text(first)}–{self._clock_text(last)}"
        n = len(pending)
        head = f"⚠️ <b>{n} warning{'s' if n != 1 else ''}</b> · <i>{span}</i>"
        rows = [f"• {_esc(line)}" + (f"  ×{c}" if c > 1 else "") for line, c in counts.items()]
        if len(rows) > DIGEST_LINES:
            rows = rows[:DIGEST_LINES] + [f"…and {len(rows) - DIGEST_LINES} more"]
        self._send("\n".join([head, *rows]), silent=True)

    def logging_handler(self) -> logging.Handler:
        """A handler that turns Spindle's WARNING log lines into digest entries."""
        return _WarningHandler(self)


class _WarningHandler(logging.Handler):
    def __init__(self, notifier: Notifier):
        super().__init__(level=logging.WARNING)
        self.notifier = notifier

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno != logging.WARNING or not record.name.startswith("spindle") \
                or record.name == __name__:
            return
        try:
            where = record.name.rsplit(".", 1)[-1]
            self.notifier.warning(f"{where}: {record.getMessage()}")
        except Exception:                         # a log handler must never raise
            pass


class RunRecord:
    """Remembers whether the previous run stopped cleanly, so the start card can say
    "back after a crash" instead of pretending every start is a restart."""

    def __init__(self, path: Path | None = None, clock: Callable[[], float] = time.time,
                 uptime: Callable[[], float | None] | None = None):
        self.path = path or DEFAULT_RUN_PATH
        self._clock = clock
        self._uptime = uptime or _system_uptime

    def begin(self) -> dict:
        """Record this start; return the reason for it and the gap since the last stop."""
        previous: dict = {}
        with contextlib.suppress(OSError, ValueError):
            previous = json.loads(self.path.read_text())
        now = self._clock()
        up = self._uptime()
        facts: dict = {}
        if up is not None and up < BOOT_WINDOW:
            facts["reason"] = "booted"
        elif previous and not previous.get("clean_exit", True):
            facts["reason"] = "crashed"
        elif previous.get("ended_at"):
            facts["reason"] = "restarted"
            facts["stopped_for"] = max(0.0, now - float(previous["ended_at"]))
        else:
            facts["reason"] = "started"
        self._write({"started_at": now, "pid": os.getpid(), "clean_exit": False})
        return facts

    def end(self) -> None:
        """A clean stop: the next start is a restart, not a recovery."""
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        data.update({"clean_exit": True, "ended_at": self._clock()})
        self._write(data)

    def _write(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            tmp.replace(self.path)
        except OSError as e:
            logger.debug("Run record not written: %s", e)


def _system_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600} h {(s % 3600) // 60:02d}"
    return f"{s // 86400} d"


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

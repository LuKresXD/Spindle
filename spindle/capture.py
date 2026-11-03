"""Continuous audio capture: one long-running arecord, a ring buffer in memory.

The old design recorded 2 s, then stopped recording while it fingerprinted
(up to 20 s of network calls), then recorded another 2 s. The "sliding
window" it fingerprinted was therefore three 2 s pieces from different
moments glued together, which is poison for Chromaprint. Here arecord runs
for the life of the process and a reader thread keeps the last
`window_seconds` (+ a little slack) of raw PCM in a ring buffer. Any caller
can ask for the level of the last N seconds or a contiguous WAV of the last
N seconds at any time, independent of how long identification takes.

If arecord dies (USB device hiccup, unplugged cable) the reader logs it,
waits, and restarts it. `buffered_seconds()` tells the caller how much
real audio it has.
"""

from __future__ import annotations

import contextlib
import logging
import math
import struct
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from .config import AudioConfig

logger = logging.getLogger(__name__)

DEFAULT_STEP = 2            # seconds between engine ticks
SILENCE_FLOOR_DB = -120.0   # reported level when there is no audio at all
_READ_SECONDS = 0.25        # reader granularity
_RESTART_DELAY = 2.0        # seconds to wait before relaunching a dead arecord


def rms_db(pcm: bytes) -> float:
    """RMS level of 16-bit little-endian PCM, in dBFS."""
    if len(pcm) < 2:
        return SILENCE_FLOOR_DB
    data = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype=np.int16)
    if data.size == 0:
        return SILENCE_FLOOR_DB
    rms = math.sqrt(float(np.mean(data.astype(np.float64) ** 2)))
    if rms <= 0:
        return SILENCE_FLOOR_DB
    return 20 * math.log10(rms / 32768.0)


def write_wav(path: Path, pcm: bytes, sample_rate: int, channels: int) -> None:
    """Write raw 16-bit PCM as a canonical WAV file."""
    bps = 16
    with open(path, "wb") as f:
        f.write(b"RIFF")
        f.write(struct.pack("<I", 36 + len(pcm)))
        f.write(b"WAVE")
        f.write(b"fmt ")
        f.write(struct.pack("<IHHIIHH", 16, 1, channels, sample_rate,
                            sample_rate * channels * bps // 8, channels * bps // 8, bps))
        f.write(b"data")
        f.write(struct.pack("<I", len(pcm)))
        f.write(pcm)


class ContinuousCapture:
    """Ring buffer of the most recent audio, fed by a background arecord."""

    def __init__(self, audio_cfg: AudioConfig, window_seconds: int | None = None,
                 slack_seconds: float = 2.0):
        self.cfg = audio_cfg
        self.window_seconds = int(window_seconds or audio_cfg.chunk_duration)
        self.bytes_per_second = audio_cfg.sample_rate * audio_cfg.channels * 2
        self._capacity = int(self.bytes_per_second * (self.window_seconds + slack_seconds))
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._running = False
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self.restarts = 0
        self.last_error: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()
        logger.info("Capture: %s, %d Hz, %d ch, %ds window",
                    self.cfg.device, self.cfg.sample_rate, self.cfg.channels,
                    self.window_seconds)

    def stop(self) -> None:
        self._running = False
        proc = self._proc
        if proc and proc.poll() is None:
            proc.terminate()
        if self._thread:
            self._thread.join(timeout=3)

    @property
    def running(self) -> bool:
        return self._running and self._proc is not None and self._proc.poll() is None

    # ── reads ────────────────────────────────────────────────────────────────

    def buffered_seconds(self) -> float:
        with self._lock:
            return len(self._buf) / self.bytes_per_second

    def last_pcm(self, seconds: float) -> bytes:
        n = int(seconds * self.bytes_per_second)
        n -= n % (self.cfg.channels * 2)              # whole frames only
        with self._lock:
            return bytes(self._buf[-n:]) if n > 0 else b""

    def level_db(self, seconds: float = DEFAULT_STEP) -> float:
        """RMS level of the most recent `seconds` of audio."""
        return rms_db(self.last_pcm(seconds))

    def window_wav(self, seconds: float | None = None, min_seconds: float = 2.0) -> Path | None:
        """Write the last `seconds` (default: the full window) to a temp WAV.

        Returns None when fewer than `min_seconds` are buffered yet. The
        caller owns the file and must unlink it.
        """
        pcm = self.last_pcm(seconds or self.window_seconds)
        if len(pcm) < min_seconds * self.bytes_per_second:
            return None
        tmp = tempfile.NamedTemporaryFile(prefix="spindle-", suffix=".wav", delete=False)
        tmp.close()
        path = Path(tmp.name)
        write_wav(path, pcm, self.cfg.sample_rate, self.cfg.channels)
        return path

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    # ── reader thread ────────────────────────────────────────────────────────

    def _command(self) -> list[str]:
        return [
            "arecord", "-D", self.cfg.device, "-f", "S16_LE",
            "-r", str(self.cfg.sample_rate), "-c", str(self.cfg.channels),
            "-t", "raw", "-q", "-",
        ]

    def _run(self) -> None:
        read_size = int(self.bytes_per_second * _READ_SECONDS)
        while self._running:
            try:
                self._proc = subprocess.Popen(
                    self._command(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            except OSError as e:
                self.last_error = str(e)
                logger.error("Capture: cannot start arecord: %s", e)
                time.sleep(_RESTART_DELAY)
                continue
            proc = self._proc
            assert proc.stdout is not None
            while self._running:
                chunk = proc.stdout.read(read_size)
                if not chunk:
                    break
                with self._lock:
                    self._buf.extend(chunk)
                    if len(self._buf) > self._capacity:
                        del self._buf[: len(self._buf) - self._capacity]
            # arecord ended (device gone, or we are stopping)
            err = b""
            if proc.stderr is not None:
                with contextlib.suppress(OSError):
                    err = proc.stderr.read()
            if proc.poll() is None:
                proc.terminate()
            if self._running:
                self.restarts += 1
                self.last_error = err.decode(errors="replace").strip() or "arecord exited"
                logger.warning("Capture: arecord stopped (%s); restarting in %.0fs",
                               self.last_error, _RESTART_DELAY)
                self.clear()
                time.sleep(_RESTART_DELAY)

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
_RESTART_MAX_DELAY = 30.0   # ... growing to this while it keeps failing

# Spectral motion: 1/3-octave bands from 100 Hz to ~10 kHz. Music moves between
# bands from second to second; the noise of an empty turntable (motor, preamp,
# mains) keeps the same spectrum for as long as it runs.
MOTION_BAND_EDGES = tuple(100.0 * 2 ** (i / 3) for i in range(21))
MOTION_SUB_SECONDS = 1.0


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


def _mono(pcm: bytes, channels: int) -> np.ndarray:
    data = np.frombuffer(pcm[: len(pcm) - len(pcm) % (2 * channels)], dtype=np.int16)
    x = data.astype(np.float64) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x


def _band_profile(seg: np.ndarray, sample_rate: int) -> np.ndarray:
    """log10 of the fraction of the segment's power in each motion band."""
    win = np.hanning(len(seg))
    power = np.abs(np.fft.rfft(seg * win)) ** 2
    freqs = np.fft.rfftfreq(len(seg), 1.0 / sample_rate)
    bands = np.array([power[(freqs >= lo) & (freqs < hi)].sum()
                      for lo, hi in zip(MOTION_BAND_EDGES, MOTION_BAND_EDGES[1:], strict=False)])
    bands = bands / max(float(bands.sum()), 1e-20)
    return np.log10(bands + 1e-9)


def spectral_motion(pcm: bytes, sample_rate: int, channels: int,
                    sub_seconds: float = MOTION_SUB_SECONDS) -> float | None:
    """How much the spectrum moves from one second to the next, averaged over
    the whole buffer: the mean absolute change of the log band profile between
    consecutive `sub_seconds` pieces.

    Steady noise scores well under 0.1 (an empty turntable measured 0.05-0.07);
    music scores 0.3 and up, a sustained ambient drone 0.5. None when the
    buffer holds fewer than two pieces.
    """
    x = _mono(pcm, channels)
    n = int(sub_seconds * sample_rate)
    if n <= 0 or len(x) < 2 * n:
        return None
    pieces = [x[i:i + n] for i in range(0, len(x) - n + 1, n)]
    profiles = [_band_profile(p, sample_rate) for p in pieces]
    moves = [float(np.mean(np.abs(b - a)))
             for a, b in zip(profiles, profiles[1:], strict=False)]
    return float(np.mean(moves))


def mixer_device_for(capture_device: str) -> str | None:
    """The ALSA control device for a capture PCM name: plughw:CARD=Device,DEV=0
    -> hw:CARD=Device; hw:3,0 -> hw:3. None for "default" and other names that
    carry no card (amixer would then address whatever card is default)."""
    name = capture_device.strip()
    if ":" not in name:
        return None
    _, spec = name.split(":", 1)
    parts = [p for p in spec.split(",") if p]
    if not parts:
        return None
    card = parts[0]
    if card.upper().startswith("CARD="):
        return f"hw:{card}"
    if card.isdigit():
        return f"hw:{card}"
    return f"hw:CARD={card}"


def set_capture_gain(capture_device: str, control: str, gain_db: float) -> str | None:
    """Set the card's capture gain through amixer. Returns the readback line, or
    None when the card cannot be addressed or amixer refused (logged, never fatal:
    a wrong gain is a worse-sounding capture, not a dead service)."""
    dev = mixer_device_for(capture_device)
    if dev is None:
        logger.warning("Capture gain: cannot tell the card from device '%s'; not set",
                       capture_device)
        return None
    try:
        subprocess.run(["amixer", "-D", dev, "sset", control, "capture", f"{gain_db:g}dB"],
                       check=True, capture_output=True, text=True, timeout=5)
        out = subprocess.run(["amixer", "-D", dev, "sget", control],
                             check=True, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        logger.warning("Capture gain: amixer failed on %s '%s': %s", dev, control,
                       str(detail).strip())
        return None
    readback = next((ln.strip() for ln in out.splitlines() if "Capture" in ln), "").strip()
    logger.info("Capture gain: %s '%s' set to %g dB (%s)", dev, control, gain_db,
                readback or "no readback")
    return readback or None


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
        if self.cfg.capture_gain_db is not None:
            set_capture_gain(self.cfg.device, self.cfg.mixer_control, self.cfg.capture_gain_db)
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
        """True while audio can be expected: arecord is alive, or the reader thread
        has not had its first chance to launch it yet. False once it has died."""
        if not self._running:
            return False
        proc = self._proc
        if proc is None:
            return self.restarts == 0 and self.last_error is None   # still launching
        return proc.poll() is None

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

    def motion(self, seconds: float | None = None) -> float | None:
        """Spectral motion of the most recent `seconds` (default: the window)."""
        return spectral_motion(self.last_pcm(seconds or self.window_seconds),
                               self.cfg.sample_rate, self.cfg.channels)

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
                self.restarts += 1
                self.last_error = str(e)
                delay = min(_RESTART_DELAY * 2 ** min(self.restarts - 1, 4), _RESTART_MAX_DELAY)
                logger.error("Capture: cannot start arecord: %s (retry in %.0fs)", e, delay)
                self.clear()
                time.sleep(delay)
                continue
            proc = self._proc
            assert proc.stdout is not None
            got_audio = False
            while self._running:
                chunk = proc.stdout.read(read_size)
                if not chunk:
                    break
                if not got_audio:
                    got_audio = True
                    if self.restarts:
                        logger.info("Capture: arecord is back")
                        self.restarts = 0
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
                delay = min(_RESTART_DELAY * 2 ** min(self.restarts - 1, 4), _RESTART_MAX_DELAY)
                logger.warning("Capture: arecord stopped (%s); restarting in %.0fs",
                               self.last_error, delay)
                self.clear()
                time.sleep(delay)

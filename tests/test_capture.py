import struct

import numpy as np

from spindle.capture import SILENCE_FLOOR_DB, ContinuousCapture, rms_db, write_wav
from spindle.config import AudioConfig


def pcm_tone(seconds, amplitude, sr=44100, ch=2):
    n = int(seconds * sr)
    t = np.arange(n) / sr
    mono = (np.sin(2 * np.pi * 440 * t) * amplitude * 32767).astype(np.int16)
    return np.repeat(mono, ch).tobytes()


def test_rms_db_levels():
    assert rms_db(b"") == SILENCE_FLOOR_DB
    assert rms_db(bytes(4000)) == SILENCE_FLOOR_DB           # digital silence
    loud = rms_db(pcm_tone(0.5, 0.5))
    quiet = rms_db(pcm_tone(0.5, 0.005))
    assert -10 < loud < -8                                  # sine at 0.5 FS ~ -9 dBFS
    assert quiet < -45 < loud


def test_ring_buffer_keeps_only_the_window(tmp_path):
    cfg = AudioConfig(device="null", sample_rate=1000, channels=1, chunk_duration=4)
    cap = ContinuousCapture(cfg, slack_seconds=1.0)
    bps = cap.bytes_per_second
    assert bps == 2000
    # feed 10 s of audio the way the reader thread would
    for i in range(10):
        chunk = bytes([i % 256]) * bps
        with cap._lock:
            cap._buf.extend(chunk)
            if len(cap._buf) > cap._capacity:
                del cap._buf[: len(cap._buf) - cap._capacity]
    assert cap.buffered_seconds() == 5.0                      # 4 s window + 1 s slack
    last2 = cap.last_pcm(2.0)
    assert len(last2) == 2 * bps and last2[-1] == 9 and last2[0] == 8
    wav = cap.window_wav()
    try:
        data = wav.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WAVE"
        assert struct.unpack("<I", data[40:44])[0] == 4 * bps  # 4 s of frames
    finally:
        wav.unlink()


def test_window_wav_needs_minimum_audio():
    cfg = AudioConfig(device="null", sample_rate=1000, channels=1, chunk_duration=4)
    cap = ContinuousCapture(cfg)
    with cap._lock:
        cap._buf.extend(bytes(1500))                          # 0.75 s
    assert cap.window_wav() is None


def test_write_wav_roundtrip(tmp_path):
    p = tmp_path / "x.wav"
    write_wav(p, b"\x01\x00" * 100, 8000, 1)
    d = p.read_bytes()
    assert len(d) == 44 + 200
    assert struct.unpack("<I", d[24:28])[0] == 8000


def test_running_grace_before_first_launch_and_false_after_death():
    cfg = AudioConfig(device="null", sample_rate=1000, channels=1, chunk_duration=4)
    cap = ContinuousCapture(cfg)
    assert not cap.running                                    # not started
    cap._running = True                                       # started, arecord not launched yet
    assert cap.running
    cap.restarts, cap.last_error = 1, "No such device"        # a launch attempt failed
    assert not cap.running


def _pcm(mono: np.ndarray, ch=2) -> bytes:
    return np.repeat((mono * 32767).astype(np.int16), ch).tobytes()


def test_spectral_motion_tells_steady_noise_from_music_like_audio():
    from spindle.capture import spectral_motion
    sr = 8000
    rng = np.random.default_rng(1)
    steady = rng.normal(0, 0.05, 6 * sr)                          # same spectrum every second
    assert spectral_motion(_pcm(steady), sr, 2) < 0.15
    t = np.arange(sr) / sr
    pieces = []
    for i in range(6):                                            # the spectrum changes each second
        f = 200.0 if i % 2 == 0 else 2000.0
        pieces.append(np.sin(2 * np.pi * f * t) * 0.2 + rng.normal(0, 0.01, sr))
    moving = np.concatenate(pieces)
    assert spectral_motion(_pcm(moving), sr, 2) > 0.5
    assert spectral_motion(_pcm(steady[: sr + 100]), sr, 2) is None   # under two pieces


def test_mixer_device_for_capture_device_names():
    from spindle.capture import mixer_device_for
    assert mixer_device_for("plughw:CARD=Device,DEV=0") == "hw:CARD=Device"
    assert mixer_device_for("hw:3,0") == "hw:3"
    assert mixer_device_for("plughw:Device") == "hw:CARD=Device"
    assert mixer_device_for("default") is None


def test_set_capture_gain_runs_amixer_and_reads_back(monkeypatch):
    import subprocess

    from spindle.capture import set_capture_gain
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        out = "  Mono: Playback 8 [2%] [0.50dB] [off] Capture 320 [65%] [20.00dB] [on]\n"
        stdout = out if cmd[3] == "sget" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rb = set_capture_gain("plughw:CARD=Device,DEV=0", "Mic", 20)
    assert calls[0] == ["amixer", "-D", "hw:CARD=Device", "sset", "Mic", "capture", "20dB"]
    assert calls[1][:4] == ["amixer", "-D", "hw:CARD=Device", "sget"]
    assert rb and "20.00dB" in rb
    assert set_capture_gain("default", "Mic", 20) is None          # no card to address

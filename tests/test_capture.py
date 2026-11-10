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
        out = ("  Capture channels: Mono\n"
               "  Mono: Playback 8 [2%] [0.50dB] [off] Capture 320 [65%] [20.00dB] [on]\n")
        stdout = out if cmd[3] == "sget" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    rb = set_capture_gain("plughw:CARD=Device,DEV=0", "Mic", 20)
    assert calls[0] == ["amixer", "-D", "hw:CARD=Device", "sset", "Mic", "capture", "20dB"]
    assert calls[1][:4] == ["amixer", "-D", "hw:CARD=Device", "sget"]
    assert rb and "20.00dB" in rb
    assert set_capture_gain("default", "Mic", 20) is None          # no card to address


def _pcm(mono, sr=44100, ch=2):
    return np.repeat((np.clip(mono, -1, 1) * 32767).astype(np.int16), ch).tobytes()


def _noise(n, amp, seed):
    return np.random.default_rng(seed).normal(0, amp, n)


def test_revolution_pulse_hears_the_lead_out_and_not_music():
    from spindle.capture import is_runout, revolution_pulse
    sr = 44100
    t = np.arange(int(10 * sr)) / sr
    # the lead-out: surface noise with one thump every 1.8 s (33 rpm, a touch slow), 40 ms long
    runout = _noise(len(t), 0.004, 1)
    for k in range(6):
        at = int((0.4 + k * 1.82) * sr)
        runout[at:at + int(0.04 * sr)] += _noise(int(0.04 * sr), 0.25, 10 + k)
    pulse, duty = revolution_pulse(_pcm(runout), sr, 2)
    assert pulse > 0.6 and duty < 0.1 and is_runout(pulse, duty)
    # music: a dense envelope with a beat every 0.5 s on top of sustained sound
    music = _noise(len(t), 0.12, 2) * (0.7 + 0.3 * np.sin(2 * np.pi * t / 0.5))
    pulse, duty = revolution_pulse(_pcm(music), sr, 2)
    assert duty > 0.3 and not is_runout(pulse, duty)
    # a sparse beat: one hit every 0.6 s over near silence (a third of a revolution, so it
    # correlates at the revolution too) must not read as the lead-out
    sparse = _noise(len(t), 0.003, 4)
    for k in range(16):
        at = int((0.3 + k * 0.6) * sr)
        sparse[at:at + int(0.05 * sr)] += _noise(int(0.05 * sr), 0.3, 20 + k)
    pulse, duty = revolution_pulse(_pcm(sparse), sr, 2)
    assert pulse < 0.3 and not is_runout(pulse, duty)
    # an empty turntable's steady hum: nothing repeats, nothing sparse
    hum = 0.02 * np.sin(2 * np.pi * 50 * t) + _noise(len(t), 0.003, 3)
    pulse, duty = revolution_pulse(_pcm(hum), sr, 2)
    assert not is_runout(pulse, duty)
    # too little audio: no verdict
    assert revolution_pulse(_pcm(runout[: int(0.1 * sr)]), sr, 2) == (0.0, 0.0)
    assert revolution_pulse(b"", sr, 2) == (0.0, 0.0)


def test_a_capture_that_stops_delivering_is_seen_and_restarted(monkeypatch):
    """arecord alive, nothing on its pipe: the ring buffer kept answering with the same last
    seconds and nothing noticed."""
    import time as _time

    from spindle import capture as cap_mod
    from spindle.config import AudioConfig

    class Proc:
        killed = False
        def poll(self):
            return -9 if self.killed else None
        def kill(self):
            self.killed = True

    cap = cap_mod.ContinuousCapture(AudioConfig())
    cap._running = True
    cap._proc = Proc()
    cap._last_chunk = _time.monotonic()
    assert cap.running and not cap.stalled
    cap._last_chunk = _time.monotonic() - 10            # ten seconds of nothing
    assert cap.stalled and not cap.running
    cap.health_check()
    assert cap._proc.killed

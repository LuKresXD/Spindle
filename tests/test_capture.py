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

<p align="center">
  <img src="docs/logo.png" alt="Spindle" width="120">
</p>

<h1 align="center">Spindle</h1>

<p align="center">
  <strong>Vinyl scrobbler for Raspberry Pi</strong><br>
  Listens to your turntable, identifies tracks via audio fingerprinting, and scrobbles to Last.fm with Spotify-accurate metadata.
</p>

<p align="center">
  <a href="https://github.com/LuKresXD/spindle/actions/workflows/ci.yml"><img src="https://github.com/LuKresXD/spindle/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/LuKresXD/spindle/blob/main/LICENSE"><img src="https://img.shields.io/github/license/LuKresXD/spindle" alt="License"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.10+-blue" alt="Python 3.10+"></a>
  <a href="https://www.last.fm/user/LuKresXD"><img src="https://img.shields.io/badge/Last.fm-scrobbling-red" alt="Last.fm"></a>
</p>

---

## How it works

```
Turntable → RCA Y-splitter → USB audio adapter → Raspberry Pi → Last.fm
                ↓
            Speakers
```

Spindle records audio from a USB adapter in short chunks, fingerprints each chunk to identify the track, then scrobbles to Last.fm. It uses Spotify's API for canonical artist/title formatting so vinyl scrobbles stack perfectly with your Spotify plays.

### Key features

- **Dual fingerprinting** — [AcoustID](https://acoustid.org) (Chromaprint) primary, [ShazamIO](https://github.com/dotX12/shazamio) fallback. Catches everything from mainstream to niche.
- **Sliding window capture** — Records in 2-second steps with 10-second overlapping windows. New fingerprint attempt every 2 seconds for fast identification without sacrificing audio quality.
- **Album-lock** — Identify one track, scrobble the whole side. Uses Spotify's tracklist + track durations to predict and scrobble tracks even when fingerprinting misses. Retroactively backfills tracks that played before the first identification.
- **Spotify-canonical names** — Scrobbles use Spotify's formatting (e.g., `Playboi Carti — Poke It Out (with Nicki Minaj)`) so they stack with Spotify plays on Last.fm.
- **Offline queue** — WiFi drops? Scrobbles are saved locally and flushed when the connection is back. Survives reboots.
- **Telegram bot** — Live notifications when album-lock activates, tracks advance, and sides finish. Interactive commands: `/status`, `/history`, `/stats`.
- **Vinyl-aware timing** — Accounts for ±3% speed drift, inter-track gaps, and needle-drop imprecision.
- **30-second rule** — Respects Last.fm's scrobble spec: tracks must play ≥30s to count.
- **systemd service** — Starts on boot, restarts on crash. Set it and forget it.

## Hardware

| Component | Example | Notes |
|-----------|---------|-------|
| Raspberry Pi | Pi 4 / Pi 5 | Any model with USB + network |
| USB audio adapter | [Plugable USB-AUDIO](https://plugable.com/products/usb-audio) | Stereo input, line level |
| RCA Y-splitters | Any | Split turntable output to speakers + Pi |
| RCA → 3.5mm cable | Any | Connect splitter to USB adapter |
| *(Optional)* Display | 13.3" 1080p OLED portable monitor on HDMI0 | Album art + track; black when idle. Any HDMI screen or the Waveshare 3.5" SPI LCD works (geometry auto-detected) |

### Signal chain

```
Turntable (phono out) → Preamp/Receiver (line out)
    ↓
RCA Y-splitter
    ├── Speakers / Powered monitors
    └── RCA → 3.5mm → USB audio adapter → Pi
```

> **Important:** The Pi needs a **line-level** signal, not phono. If your turntable has a built-in preamp or you're using a receiver's line/tape out, you're good.

## Quick start

### 1. System dependencies

```bash
sudo apt install -y python3-pip python3-venv ffmpeg libchromaprint-tools portaudio19-dev alsa-utils
```

### 2. Clone and install

```bash
git clone https://github.com/LuKresXD/spindle.git
cd spindle
python3 -m venv venv
source venv/bin/activate
pip install -e .
```

### 3. Configure

```bash
cp config.example.yaml config.yaml
```

Edit `config.yaml` with your API keys:

| Service | Get your key | Required |
|---------|-------------|----------|
| [AcoustID](https://acoustid.org/new-application) | Register an application | ✅ |
| [Last.fm](https://www.last.fm/api/account/create) | Create API account | ✅ |
| [Spotify](https://developer.spotify.com/dashboard) | Create an app | Recommended |

> Spotify enables album-lock and canonical naming. Without it, Spindle still works but falls back to basic fingerprint → scrobble mode.

Find your audio device:

```bash
arecord -l
# Look for your USB adapter, e.g.: card 1: Device [USB Audio Device], device 0
# → device string: plughw:CARD=Device,DEV=0
```

### 4. Test

```bash
# Dry run — identify tracks without scrobbling
spindle --dry-run -v

# With Last.fm canonicalization preview
spindle --dry-run --canonicalize-preview -v
```

### 5. Run

```bash
spindle -v
```

### 6. Install as service (recommended)

```bash
# Edit spindle.service if your paths differ
sudo cp spindle.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spindle
```

Now Spindle starts on boot and restarts on crash. Check logs with:

```bash
journalctl -u spindle -f
```

## Album-lock scrobbling

Spindle's signature feature. When a track is identified:

1. **Lock** — Fetches the album tracklist from Spotify
2. **Backfill** — Calculates how long music has been playing. Walks backwards through the tracklist to scrobble tracks that played before identification
3. **Predict** — Tracks forward using durations. Auto-advances and scrobbles when each track's time elapses
4. **Confirm** — Subsequent fingerprint hits confirm or correct the prediction
5. **End** — Silence (needle lift, side flip, end of record) cleanly ends the session

This means: **drop the needle, walk away, come back to a fully scrobbled side** — even if fingerprinting only caught 1 out of 6 tracks.

### Edge cases handled

- Needle dropped mid-album → only backfills tracks that fit in elapsed time
- 20-second interludes → advance buffer scales (3s for short tracks, 15s for long)
- 13-minute epics → same system, just longer between advances
- Side flip → silence breaks the lock, Side B starts a fresh session
- Different record → old session finalized, new album locked
- Vinyl speed drift → ±3% tolerance on all timing calculations

## Configuration

See [`config.example.yaml`](config.example.yaml) for all options. Key settings:

```yaml
audio:
  device: "plughw:CARD=Device,DEV=0"
  chunk_duration: 10        # fingerprint window size (seconds)
  # Capture step is 2s — new window every 2s for fast identification

silence:
  threshold_db: -22         # vinyl surface noise is ~-25 dB
  min_silence_seconds: 3

scrobble:
  min_play_seconds: 30      # Last.fm 30-second rule
  dedup_window: 300         # ignore same track within 5 min

telegram:
  bot_token: ""             # from @BotFather
  chat_id: ""               # your Telegram chat ID
  silent: false             # silent notifications
  verbose: false            # notify on every scrobble
  errors: true              # notify on errors

logging:
  file: ""                  # optional log file path
  max_bytes: 5000000        # 5 MB rotation
  backup_count: 3           # keep 3 rotated files
```

## Architecture

```
spindle/
├── cli.py          # Wiring + the 2 s tick loop
├── engine.py       # Player: silence/music transitions, identification, scrobbling, ScreenState
├── state.py        # ScreenState / TrackRow: what the screen (or anything) renders from
├── capture.py      # Continuous arecord -> ring buffer; levels and contiguous WAV windows
├── fingerprint.py  # AcoustID (best-supported recording) + ShazamIO fallback, corrections
├── spotify.py      # Canonical names, album tracklists, release year
├── session.py      # Album-lock + compilation session, backfill, timing advance
├── scrobbler.py    # Last.fm scrobbling, offline queue, album play count
├── art.py          # Cover art: Apple 1200 px -> Deezer -> Spotify, disk cache
├── discogs.py      # Vinyl side/position (A3) from your Discogs collection
├── display.py      # Framebuffer renderer (HDMI OLED, or the Waveshare SPI LCD)
├── history.py      # Scrobble history (JSONL log for stats)
├── notify.py       # Telegram notifications
├── bot.py          # Telegram bot command handler
└── config.py       # YAML config loader
```

## Roadmap

- [x] Audio capture + dual fingerprinting (AcoustID + ShazamIO)
- [x] Last.fm scrobbling with dedup + canonicalization
- [x] Spotify lookup for canonical artist/title format
- [x] Album-lock scrobbling with retroactive backfill
- [x] Offline scrobble queue (survives WiFi drops + reboots)
- [x] systemd service (autostart, auto-restart)
- [x] Telegram bot (notifications + `/status`, `/history`, `/stats`)
- [x] Scrobble history + statistics
- [x] Rotating log files
- [x] Framebuffer display: album art + track info, scales to the panel (13.3" HDMI OLED, Waveshare 3.5" LCD)
- [x] Continuous capture (one arecord, ring buffer; fingerprints are always contiguous audio)
- [x] Now-playing screen v2: tracklist with the current track lit, progress + time, side/position from Discogs, 1200 px art, listening state, scrobbled tick + play count
- [x] Last.fm's half-or-4-minutes rule for early-ended tracks; `silence.min_silence_seconds` honoured
- [x] Tests (`pytest`), lint (`ruff`)

## Screen

With `display.enabled: true` Spindle draws straight to `/dev/fb0` (no X, no
Wayland): the Pi boots to the console and owns the panel. Idle is pure black,
so an OLED shows nothing until the needle drops. While a side plays: cover on
the left, artist / title / album · year, the album's tracklist with the
current track lit and finished ones ticked, and a footer with the vinyl
position from your Discogs collection (`discogs:` in config), a progress bar
with elapsed / remaining time, your Last.fm play count for the album, and a
tick once the track is scrobbled. Static parts re-render once a minute with a
small pixel shift (burn-in); the progress band refreshes once a second.

Frames can be rendered offscreen for a look without hardware:

```python
from spindle.display import Renderer
from spindle.state import Phase, ScreenState, TrackRow
r = Renderer(1920, 1080)
st = ScreenState(phase=Phase.PLAYING, artist="Madvillain", title="Accordion", album="Madvillainy",
                 tracklist=[TrackRow("Accordion", 72)], current_index=0, track_elapsed=10, track_duration=72)
frame = r.playing(st, shift=False)
y0, band = r.playing_band(frame, st)
frame.image.paste(band, (0, y0)); frame.image.save("preview.png")
```

## Development

```bash
pip install -e ".[display,dev]"
ruff check spindle/ tests/
pytest
```

## License

[MIT](LICENSE)

## Credits

Built with [pyacoustid](https://github.com/beetbox/pyacoustid), [ShazamIO](https://github.com/dotX12/shazamio), [pylast](https://github.com/pylast/pylast), and the [Spotify Web API](https://developer.spotify.com/documentation/web-api).

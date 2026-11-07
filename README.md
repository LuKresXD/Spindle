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

Spindle listens to a USB audio adapter, fingerprints the last few seconds every 2 s, works out which record is on the platter (your Discogs collection decides when a track lives on several albums), locks onto its tracklist, and scrobbles the side to Last.fm with Spotify-canonical names so vinyl plays stack with your streaming plays. The design is written up in [`docs/ENGINE.md`](docs/ENGINE.md).

### Key features

- **Dual fingerprinting** — [AcoustID](https://acoustid.org) (Chromaprint) primary, [ShazamIO](https://github.com/dotX12/shazamio) fallback, off the main loop so the screen and silence detection never stall.
- **Continuous capture** — one long-running `arecord` feeding a ring buffer; every fingerprint window is contiguous audio, a new attempt every 2 s.
- **Your collection decides** — the Discogs collection is the prior: when a track sits on the album, a single and three compilations, the pressing you own wins. Records Spotify never got still lock from the Discogs tracklist alone.
- **Album-lock, side-aware** — identify one track, scrobble the whole side. Backfills what played before the first ID, forward-fills gaps, advances by duration, and stops at the end of a side to wait for the flip. Re-locks onto a better tracklist when one appears (the album after a single, the owned pressing once the collection loaded).
- **Spotify-canonical names** — `Playboi Carti — Poke It Out (with Nicki Minaj)`, so vinyl plays stack with streaming plays on Last.fm.
- **Last.fm's rules, properly** — half the track or 4 minutes for early-ended tracks; an outbox thread with an offline queue that survives reboots; rejected scrobbles are reported, not retried forever.
- **Telegram bot** — album-lock card, live now-playing edits, side summaries; `/now`, `/album`, `/history`, `/stats`, `/collection`.
- **Screen** — cover, tracklist with the current track lit, `A3 · SIDE A`, progress, play count, on any framebuffer (13.3" HDMI OLED or the Waveshare SPI LCD).
- **systemd service** — starts on boot, restarts on crash.

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
| [Discogs](https://www.discogs.com/settings/developers) | Personal access token + your username | Recommended |

> Spotify gives canonical names and tracklists; Discogs gives the records you actually own (album choice, side positions, tracklists Spotify lacks). Album-lock works with either; with neither, Spindle falls back to plain fingerprint → scrobble.

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

Spindle's signature feature. When a track is identified (twice: one-off false matches never count):

1. **Resolve** — which album? Your Discogs collection first, then the album already locked, then album > single > compilation. Owned pressing + Spotify edition are merged: record order and `A1…D3` positions, Spotify names and durations.
2. **Lock** — the session takes that tracklist
3. **Backfill** — how long has music been playing? Walk backwards through the side to scrobble tracks that played before the first ID
4. **Predict** — advance by duration; scrobble each track as its time elapses; stop at the end of the side
5. **Confirm** — later fingerprint hits confirm, correct, or re-lock onto a better tracklist
6. **End** — silence (needle lift, side flip, end of record) ends the session

This means: **drop the needle, walk away, come back to a fully scrobbled side** — even if fingerprinting only caught 1 out of 6 tracks.

### Edge cases handled

- Needle dropped mid-side → backfills only what fits in the elapsed time, never across a side boundary
- Track on the album *and* a single *and* a compilation → the record you own wins; otherwise the album
- Record not on Spotify → Discogs tracklist locks the album; Shazam/AcoustID name the tracks
- Locked onto a single by mistake → re-locks onto the album when the next track arrives
- Same artist, wrong title (sample, catalogue confusion) → ignored, the lock holds
- Two different artists in a row → compilation mode, each track scrobbled when it ends
- Needle lifted early → Last.fm's half-or-4-minutes rule decides
- 20-second interludes / 13-minute epics → advance buffer scales (3–15 s)
- Side flip → silence ends the session; side B starts fresh
- Vinyl speed drift → ±3 % tolerance on all timing
- USB adapter hiccup → the session holds instead of hearing "silence"; `arecord` is relaunched with backoff
- WiFi down → scrobbles queue on disk and flush when it returns; a scrobble Last.fm rejects is reported, not retried forever

## Configuration

See [`config.example.yaml`](config.example.yaml) for all options. Key settings:

```yaml
audio:
  device: "plughw:CARD=Device,DEV=0"
  chunk_duration: 10        # fingerprint window size (seconds)
  capture_gain_db: 20       # set the card's capture gain at start (a USB mic input at its
                            # full 31 dB clips a line-level turntable and lifts its idle noise)
  # Capture step is 2s — new window every 2s for fast identification

silence:
  threshold_db: -45         # line-level USB capture: idle floor is ~-50 dB
  min_silence_seconds: 8    # quiet this long = side over (run-out groove)
  start_margin_db: 6        # music must clear threshold + margin to start a session

discogs:
  token: ""                 # personal access token
  username: ""              # your Discogs username

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
├── engine.py       # Player + IdentifyWorker: transitions, identification off-thread, ScreenState
├── state.py        # ScreenState / TrackRow: what the screen (or anything) renders from
├── capture.py      # Continuous arecord -> ring buffer; levels and contiguous WAV windows
├── fingerprint.py  # AcoustID (best-supported recording) + ShazamIO fallback, corrections
├── resolve.py      # Fingerprint -> album: Discogs collection, locked album, album > single
├── spotify.py      # Track candidates, canonical names, album tracklists
├── discogs.py      # Your collection: releases, tracklists, positions (background refresh)
├── tracklist.py    # AlbumTracklist, sides, Discogs <-> Spotify merge
├── session.py      # Album-lock (side-aware, re-lock) + compilation session, backfill, timing
├── scrobbler.py    # Last.fm outbox thread, offline queue, error classes, album play count
├── art.py          # Cover art: Apple 1200 px -> Deezer -> Spotify, disk cache
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
- [x] Telegram bot (notifications + `/now`, `/album`, `/history`, `/stats`, `/collection`)
- [x] Scrobble history + statistics
- [x] Rotating log files
- [x] Framebuffer display: album art + track info, scales to the panel (13.3" HDMI OLED, Waveshare 3.5" LCD)
- [x] Continuous capture (one arecord, ring buffer; fingerprints are always contiguous audio)
- [x] Now-playing screen v2: tracklist with the current track lit, progress + time, side/position from Discogs, 1200 px art, listening state, scrobbled tick + play count
- [x] Last.fm's half-or-4-minutes rule for early-ended tracks; `silence.min_silence_seconds` honoured
- [x] Tests (`pytest`), lint (`ruff`), types (`mypy`)
- [x] v0.2: the Discogs collection as the identification prior (owned album wins, Discogs-only locks, merged pressings), side-aware timing, re-lock, compilation-mode timing, identification and Last.fm off the tick thread, scrobble error classes
- [ ] Touch "Claw mode" for the wall screen (Wayland kiosk)

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

Built with [pyacoustid](https://github.com/beetbox/pyacoustid), [ShazamIO](https://github.com/dotX12/shazamio), [pylast](https://github.com/pylast/pylast), the [Spotify Web API](https://developer.spotify.com/documentation/web-api) and the [Discogs API](https://www.discogs.com/developers).

## Touch display (the 13.3" OLED)

Since v0.3.0 the screen is a web page Spindle serves itself, shown full screen by
Chromium under the `cage` Wayland compositor. The framebuffer renderer
(`display.enabled: true`) is the fallback for a plain HDMI panel.

```yaml
web:
  enabled: true           # serve the display at http://127.0.0.1:8787/
display:
  enabled: false          # the kiosk owns the screen; fb0 writes would vanish behind it
```

Install the kiosk once (Raspberry Pi OS, console boot, user `luka`):

```bash
sudo apt install cage chromium
sudo cp spindle-kiosk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now spindle-kiosk
```

The page: cover, title, progress with "Side A · 10 min left", this side's
tracklist; tap the title or **Lyrics** for time-synced lyrics (LRCLIB); swipe
up or **Info** for the pressing (Discogs), credits, the story (Wikipedia) and
your plays (Last.fm); a card when a side finishes; swipe right for a black
screen, tap to wake. Idle is pure black (OLED off).

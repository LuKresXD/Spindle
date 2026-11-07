"""Configuration loader for Spindle.

Unknown keys are reported and ignored rather than crashing the service: a
typo in config.yaml should cost a warning line, not a restart loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    Path("config.yaml"),
    Path.home() / ".config" / "spindle" / "config.yaml",
    Path("/etc/spindle/config.yaml"),
]


@dataclass
class AcoustIDConfig:
    api_key: str = ""


@dataclass
class LastFMConfig:
    api_key: str = ""
    api_secret: str = ""
    username: str = ""
    password_hash: str = ""
    session_key: str = ""     # preferred over password_hash when set (pylast session key)


@dataclass
class AudioConfig:
    device: str = "default"
    sample_rate: int = 44100
    channels: int = 2
    chunk_duration: int = 6
    # ALSA capture gain applied at start (dB, as the card's mixer reports it); None = leave
    # the card alone. A USB "mic" input at full gain clips a line-level turntable and lifts
    # the empty-turntable noise above the music threshold.
    capture_gain_db: float | None = None
    mixer_control: str = "Mic"     # the simple mixer control that carries the capture gain


@dataclass
class FingerprintConfig:
    acoustid_timeout: int = 10
    shazam_fallback: bool = True
    min_confidence: float = 0.5
    corrections: str = ""     # path to corrections.yaml (default: ~/spindle/corrections.yaml)


@dataclass
class ScrobbleConfig:
    min_play_seconds: int = 30
    min_play_fraction: float = 0.5
    now_playing: bool = True
    dedup_window: int = 300


@dataclass
class SilenceConfig:
    threshold_db: float = -45.0
    min_silence_seconds: float = 8.0   # quiet this long = the side is over
    start_margin_db: float = 6.0       # music must clear threshold + margin to START a session
    start_ticks: int = 2               # ... for this many consecutive ticks (2 s each)


@dataclass
class DisplayConfig:
    enabled: bool = False
    fb_path: str = "/dev/fb0"


@dataclass
class SpotifyConfig:
    client_id: str = ""
    client_secret: str = ""


@dataclass
class DiscogsConfig:
    token: str = ""
    username: str = ""


@dataclass
class WebConfig:
    """The touch display: a local web UI served by Spindle, shown by a kiosk browser."""
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8787


@dataclass
class ArtConfig:
    itunes: bool = True       # Apple catalog, 1200 px
    deezer: bool = True       # Deezer, has the explicit albums Apple's search hides


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    silent: bool = False      # send notifications silently (no sound)
    verbose: bool = False     # notify on every individual scrobble
    errors: bool = True       # notify on errors


@dataclass
class LoggingConfig:
    file: str = ""                  # log file path (empty = stdout only)
    max_bytes: int = 5_000_000     # 5 MB
    backup_count: int = 3          # keep 3 rotated files


@dataclass
class SpindleConfig:
    acoustid: AcoustIDConfig = field(default_factory=AcoustIDConfig)
    lastfm: LastFMConfig = field(default_factory=LastFMConfig)
    spotify: SpotifyConfig = field(default_factory=SpotifyConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    fingerprint: FingerprintConfig = field(default_factory=FingerprintConfig)
    scrobble: ScrobbleConfig = field(default_factory=ScrobbleConfig)
    silence: SilenceConfig = field(default_factory=SilenceConfig)
    display: DisplayConfig = field(default_factory=DisplayConfig)
    discogs: DiscogsConfig = field(default_factory=DiscogsConfig)
    web: WebConfig = field(default_factory=WebConfig)
    art: ArtConfig = field(default_factory=ArtConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: Path | None = None) -> SpindleConfig:
    """Load config from YAML. Searches the default paths when none is given."""
    paths = [Path(path)] if path is not None else DEFAULT_CONFIG_PATHS
    for p in paths:
        if p.exists():
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            cfg = _parse_config(raw)
            if cfg.fingerprint.corrections and not Path(cfg.fingerprint.corrections).is_absolute():
                cfg.fingerprint.corrections = str(p.parent / cfg.fingerprint.corrections)
            return cfg
    return SpindleConfig()


def _section(cls, raw: dict, name: str):
    """Build a dataclass from a YAML section, ignoring (and naming) unknown keys."""
    data = raw.get(name)
    if data is None:
        return cls()
    if not isinstance(data, dict):
        logger.warning("config: '%s' should be a mapping, ignoring", name)
        return cls()
    known = {f.name for f in fields(cls)}
    unknown = sorted(k for k in data if k not in known)
    if unknown:
        logger.warning("config: unknown key(s) in '%s': %s (ignored)", name, ", ".join(unknown))
    kwargs = {k: v for k, v in data.items() if k in known}
    for f in fields(cls):
        if f.name in kwargs and kwargs[f.name] is None:
            del kwargs[f.name]                       # "key:" with no value = default
    return cls(**kwargs)


def _parse_config(raw: dict) -> SpindleConfig:
    cfg = SpindleConfig()
    for f in fields(SpindleConfig):
        setattr(cfg, f.name, _section(f.type if isinstance(f.type, type) else
                                      type(getattr(cfg, f.name)), raw, f.name))
    known = {f.name for f in fields(SpindleConfig)}
    unknown = sorted(k for k in raw if k not in known)
    if unknown:
        logger.warning("config: unknown top-level key(s): %s (ignored)", ", ".join(unknown))
    return cfg

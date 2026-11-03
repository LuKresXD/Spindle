"""Configuration loader for Spindle."""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

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


@dataclass
class AudioConfig:
    device: str = "default"
    sample_rate: int = 44100
    channels: int = 2
    chunk_duration: int = 6


@dataclass
class FingerprintConfig:
    acoustid_timeout: int = 10
    shazam_fallback: bool = True
    min_confidence: float = 0.5


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
    art: ArtConfig = field(default_factory=ArtConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)


def load_config(path: Path | None = None) -> SpindleConfig:
    """Load config from YAML file. Searches default paths if none given."""
    paths = [Path(path)] if path is not None else DEFAULT_CONFIG_PATHS

    for p in paths:
        if p.exists():
            with open(p) as f:
                raw = yaml.safe_load(f) or {}
            return _parse_config(raw)

    # No config file found — return defaults
    return SpindleConfig()


def _parse_config(raw: dict) -> SpindleConfig:
    """Parse raw YAML dict into SpindleConfig."""
    cfg = SpindleConfig()

    if "acoustid" in raw:
        cfg.acoustid = AcoustIDConfig(**raw["acoustid"])
    if "lastfm" in raw:
        cfg.lastfm = LastFMConfig(**raw["lastfm"])
    if "spotify" in raw:
        cfg.spotify = SpotifyConfig(**raw["spotify"])
    if "audio" in raw:
        cfg.audio = AudioConfig(**raw["audio"])
    if "fingerprint" in raw:
        cfg.fingerprint = FingerprintConfig(**raw["fingerprint"])
    if "scrobble" in raw:
        cfg.scrobble = ScrobbleConfig(**raw["scrobble"])
    if "silence" in raw:
        cfg.silence = SilenceConfig(**raw["silence"])
    if "display" in raw:
        cfg.display = DisplayConfig(**raw["display"])
    if "discogs" in raw:
        cfg.discogs = DiscogsConfig(**raw["discogs"])
    if "art" in raw:
        cfg.art = ArtConfig(**raw["art"])
    if "telegram" in raw:
        cfg.telegram = TelegramConfig(**raw["telegram"])
    if "logging" in raw:
        cfg.logging = LoggingConfig(**raw["logging"])

    return cfg

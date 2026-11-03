"""Spindle CLI: wiring + the tick loop. The behaviour lives in engine.Player."""

from __future__ import annotations

import argparse
import functools
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .art import ArtProvider
from .bot import SpindleBot
from .capture import DEFAULT_STEP, ContinuousCapture
from .config import SpindleConfig, load_config
from .display import Display
from .engine import Extras, Player
from .fingerprint import identify
from .history import ScrobbleHistory
from .notify import Notifier
from .scrobbler import Scrobbler
from .session import ScrobbleSession
from .spotify import SpotifyClient

logger = logging.getLogger("spindle")

QUEUE_FLUSH_EVERY = 600     # seconds; retry queued offline scrobbles even with nothing playing

_running = True


def _signal_handler(sig, frame):
    global _running
    logger.info("Shutting down...")
    _running = False


def _setup_logging(cfg: SpindleConfig, verbose: bool) -> None:
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    log_datefmt = "%H:%M:%S"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if cfg.logging.file:
        log_path = Path(cfg.logging.file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=cfg.logging.max_bytes, backupCount=cfg.logging.backup_count,
        )
        fh.setFormatter(logging.Formatter(log_format, datefmt=log_datefmt))
        handlers.append(fh)
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format=log_format, datefmt=log_datefmt, handlers=handlers)
    for noisy in ("httpcore", "httpx", "urllib3", "pylast", "asyncio", "aiohttp_retry",
                  "aiohttp", "shazamio", "shazamio_core", "shazamio.request", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def build_extras(cfg: SpindleConfig, scrobbler: Scrobbler | None) -> Extras:
    """Optional screen enrichments; each one is independent and may be absent."""
    extras = Extras()
    if cfg.discogs.token and cfg.discogs.username:
        try:
            from .discogs import DiscogsCollection
            collection = DiscogsCollection(cfg.discogs.token, cfg.discogs.username)
            extras.position = collection.position
            logger.info("Discogs positions enabled for %s", cfg.discogs.username)
        except ImportError:
            logger.warning("Discogs configured but module missing")
    if scrobbler is not None:
        extras.album_playcount = scrobbler.album_playcount
    return extras


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="spindle",
        description="Vinyl scrobbler: fingerprint what's playing and scrobble to Last.fm",
    )
    parser.add_argument("-c", "--config", type=Path, help="Path to config.yaml")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    parser.add_argument("--dry-run", action="store_true", help="Identify only, don't scrobble")
    parser.add_argument("--version", action="version", version=f"spindle {__version__}")
    args = parser.parse_args()

    cfg = load_config(args.config)
    _setup_logging(cfg, args.verbose)
    logger.info("Spindle v%s starting", __version__)

    if not cfg.acoustid.api_key:
        logger.error("AcoustID API key not set: check config.yaml")
        sys.exit(1)

    # Last.fm
    scrobbler = None
    if not args.dry_run:
        if not cfg.lastfm.api_key or not cfg.lastfm.username:
            logger.error("Last.fm credentials not set: check config.yaml")
            sys.exit(1)
        scrobbler = Scrobbler(cfg.lastfm, cfg.scrobble)
        scrobbler.connect()

    # Spotify + smart session
    spotify = session = None
    if cfg.spotify.client_id and cfg.spotify.client_secret:
        spotify = SpotifyClient(cfg.spotify)
        session = ScrobbleSession(
            spotify,
            min_play_seconds=cfg.scrobble.min_play_seconds,
            chunk_duration=DEFAULT_STEP,
            min_play_fraction=cfg.scrobble.min_play_fraction,
        )
        logger.info("Spotify lookup + smart session enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials): simple mode only")

    notifier = Notifier(cfg.telegram)
    history = ScrobbleHistory()
    art = ArtProvider(itunes=cfg.art.itunes, deezer=cfg.art.deezer)

    display = Display(enabled=cfg.display.enabled, fb_path=cfg.display.fb_path)
    display.init()

    capture = ContinuousCapture(cfg.audio)
    capture.start()
    logger.info("Listening on %s (window %ds, step %ds)",
                cfg.audio.device, cfg.audio.chunk_duration, DEFAULT_STEP)

    player = Player(
        cfg, capture,
        functools.partial(identify, acoustid_cfg=cfg.acoustid, fp_cfg=cfg.fingerprint),
        session=session, spotify=spotify, scrobbler=scrobbler,
        notifier=notifier, history=history, art=art,
        extras=build_extras(cfg, scrobbler), dry_run=args.dry_run,
    )

    bot = None
    if cfg.telegram.bot_token and cfg.telegram.chat_id and not args.dry_run:
        bot = SpindleBot(bot_token=cfg.telegram.bot_token, chat_id=cfg.telegram.chat_id,
                         session=session, history=history, notifier=notifier, player=player)
        bot.start()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.dry_run:
        logger.info("DRY RUN: will identify but not scrobble")
    else:
        notifier.started()

    # Tick loop: a steady STEP cadence regardless of how long a tick takes.
    next_tick = time.monotonic()
    last_flush = time.monotonic()
    while _running:
        try:
            state = player.tick()
            display.update(state)
            if scrobbler and time.monotonic() - last_flush >= QUEUE_FLUSH_EVERY:
                last_flush = time.monotonic()
                if scrobbler.queue_size():
                    scrobbler.flush_queue()
        except Exception as e:
            logger.error("Error in tick: %s", e, exc_info=True)
            notifier.error(str(e))
            time.sleep(5)
        next_tick += DEFAULT_STEP
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:                         # a slow tick: resync instead of racing to catch up
            next_tick = time.monotonic()

    player.close()
    if bot:
        bot.stop()
    display.clear()
    display.stop()
    capture.stop()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()

"""Spindle CLI: wiring + the tick loop. The behaviour lives in engine.Player."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import sys
import time
from pathlib import Path

from . import __version__
from .art import ArtProvider
from .capture import DEFAULT_STEP, ContinuousCapture
from .config import SpindleConfig, load_config
from .display import Display
from .engine import Extras, IdentifyWorker, Player
from .fingerprint import Corrections, Identifier
from .history import ScrobbleHistory
from .notify import Notifier, RunRecord
from .resolve import Resolver
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

    notifier = Notifier(cfg.telegram)
    logging.getLogger().addHandler(notifier.logging_handler())   # WARNING lines → a digest
    run = RunRecord()
    run_facts = run.begin()

    # Last.fm (its own outbox thread; the engine never waits on it)
    scrobbler = None
    if not args.dry_run:
        if not cfg.lastfm.api_key or not cfg.lastfm.username:
            logger.error("Last.fm credentials not set: check config.yaml")
            sys.exit(1)
        scrobbler = Scrobbler(cfg.lastfm, cfg.scrobble, on_error=notifier.error,
                              on_recovered=notifier.recovered)
        scrobbler.connect()

    # Spotify (canonical names, tracklists) and the Discogs collection (the records he owns)
    spotify = None
    if cfg.spotify.client_id and cfg.spotify.client_secret:
        spotify = SpotifyClient(cfg.spotify)
        logger.info("Spotify lookup enabled")
    else:
        logger.info("Spotify lookup disabled (no credentials)")

    collection = None
    if cfg.discogs.token and cfg.discogs.username:
        from .discogs import DiscogsCollection
        collection = DiscogsCollection(cfg.discogs.token, cfg.discogs.username)
        collection.start()
        st = collection.stats()
        logger.info("Discogs collection for %s: %d releases cached (%d with tracklists); "
                    "refreshing in the background", cfg.discogs.username, st["releases"],
                    st["loaded"])

    # Album lock needs a tracklist source: Spotify or the collection
    session = None
    if spotify or collection:
        session = ScrobbleSession(
            min_play_seconds=cfg.scrobble.min_play_seconds,
            chunk_duration=DEFAULT_STEP,
            min_play_fraction=cfg.scrobble.min_play_fraction,
            window_seconds=cfg.audio.chunk_duration,
        )
        sources = [n for n, on in (("spotify", spotify), ("discogs", collection)) if on]
        logger.info("Album-lock session enabled (%s)", " + ".join(sources))
    else:
        logger.info("No tracklist source: simple mode only")

    history = ScrobbleHistory()
    art = ArtProvider(itunes=cfg.art.itunes, deezer=cfg.art.deezer)

    corrections = Corrections(Path(cfg.fingerprint.corrections)) if cfg.fingerprint.corrections \
        else Corrections()
    identifier = Identifier(cfg.acoustid, cfg.fingerprint, corrections)
    worker = IdentifyWorker(identifier.identify, Resolver(spotify, collection), art)

    extras = Extras()
    if collection is not None:
        extras.position = collection.position
    if scrobbler is not None:
        extras.album_playcount = scrobbler.album_playcount

    display = Display(enabled=cfg.display.enabled, fb_path=cfg.display.fb_path)
    display.init()

    capture = ContinuousCapture(cfg.audio)
    capture.start()
    logger.info("Listening on %s (window %ds, step %ds)",
                cfg.audio.device, cfg.audio.chunk_duration, DEFAULT_STEP)

    player = Player(
        cfg, capture, worker,
        session=session, scrobbler=scrobbler, history=history,
        extras=extras, dry_run=args.dry_run,
    )

    web = None
    if cfg.web.enabled:
        from .web import WebServer
        web = WebServer(cfg.web, collection=collection, scrobbler=scrobbler, history=history)
        web.start()
        player.side_listeners.append(web.hub.side_ended)
        logger.info("Touch display served at http://%s:%d/", cfg.web.host, cfg.web.port)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if args.dry_run:
        logger.info("DRY RUN: will identify but not scrobble")
    notifier.started({
        **run_facts, "version": __version__, "dry_run": args.dry_run,
        "display": cfg.display.enabled or cfg.web.enabled,
        "records": collection.stats()["releases"] if collection is not None else None,
        "gain_db": cfg.audio.capture_gain_db,
    })

    # Tick loop: a steady STEP cadence regardless of how long a tick takes.
    next_tick = time.monotonic()
    last_flush = time.monotonic()
    while _running:
        try:
            state = player.tick()
            display.update(state)
            if web is not None:
                web.hub.publish(state)
            if scrobbler and time.monotonic() - last_flush >= QUEUE_FLUSH_EVERY:
                last_flush = time.monotonic()
                if scrobbler.queue_size():
                    scrobbler.flush_queue()
        except Exception as e:
            logger.error("Error in tick: %s", e, exc_info=True)
            notifier.error("A tick failed", str(e))
            time.sleep(5)
        next_tick += DEFAULT_STEP
        delay = next_tick - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:                         # a slow tick: resync instead of racing to catch up
            next_tick = time.monotonic()

    player.close()
    run.end()
    if web is not None:
        web.stop()
    display.clear()
    display.stop()
    capture.stop()
    worker.close()
    identifier.close()
    if collection is not None:
        collection.stop()
    if scrobbler is not None:
        scrobbler.close()
    logger.info("Spindle stopped")


if __name__ == "__main__":
    main()

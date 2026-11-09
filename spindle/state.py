"""What the screen (and anything else) needs to know about playback.

The engine builds one `ScreenState` per tick; the display renders from it
and nothing else. Keeping this a plain data object means the renderer can
be exercised offline with made-up states, and a future second mode of the
wall screen only needs to read the same object.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Phase(Enum):
    IDLE = "idle"               # silence: screen is black
    LISTENING = "listening"     # music detected, nothing identified yet
    PLAYING = "playing"         # we know what is playing


@dataclass
class TrackRow:
    """One row of the on-screen tracklist."""
    title: str
    duration: int | None = None     # seconds
    position: str = ""              # vinyl position from Discogs, e.g. "A3"
    scrobbled: bool = False
    base: str = ""                  # the song without credits or edition ("Demonz")
    featured: list[str] = field(default_factory=list)
    version: str = ""               # "Interlude", "2017 Remaster", ... or ""


@dataclass
class ScreenState:
    phase: Phase = Phase.IDLE
    level_db: float = -120.0        # most recent audio level (listening state)
    motion: float | None = None     # spectral motion at the music gate (None = not measured)
    steady_noise: bool = False      # loud but standing still: an empty turntable, not music
    deck_speed: float | None = None # measured turntable speed, 1.03 = 3 % fast (None = unknown)

    artist: str = ""
    title: str = ""                 # as scrobbled: credits and edition included
    album: str = ""
    year: str = ""
    # for the screen: the song, its features and its edition taken apart (spindle.titles)
    base: str = ""
    featured: list[str] = field(default_factory=list)
    version: str = ""
    album_display: str = ""         # the album without its edition tag
    art: bytes | None = None        # cover image bytes (any format PIL reads)
    art_source: str = ""            # "itunes" / "spotify" / ""

    tracklist: list[TrackRow] = field(default_factory=list)
    current_index: int = -1         # index into tracklist, -1 = unknown
    track_elapsed: float | None = None   # seconds into the current track
    track_duration: float | None = None  # seconds; None = unknown
    start_exact: bool = False       # the track's start is pinned by fingerprint offsets

    side: str = ""                  # "A" / "B" ... from Discogs
    position: str = ""              # "A3" from Discogs
    owned: bool = False             # the pressing is in the Discogs collection

    scrobbled: bool = False         # current track already scrobbled
    album_playcount: int | None = None   # Luka's Last.fm plays of this album
    session_scrobbles: int = 0

    updated_at: float = field(default_factory=time.time)

    # ── helpers ──────────────────────────────────────────────────────────────

    def identity(self) -> tuple:
        """Everything that changes the static part of the frame."""
        return (
            self.phase, self.artist, self.title, self.album, self.year,
            len(self.art or b""), self.current_index,
            tuple((r.title, r.position, r.scrobbled) for r in self.tracklist),
            self.side, self.position, self.scrobbled, self.album_playcount,
            self.session_scrobbles,
        )

    def elapsed_now(self) -> float | None:
        """Track elapsed time extrapolated to now."""
        if self.track_elapsed is None:
            return None
        e = self.track_elapsed + (time.time() - self.updated_at)
        if self.track_duration:
            e = min(e, self.track_duration)
        return max(0.0, e)

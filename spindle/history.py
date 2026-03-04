"""Scrobble history — append-only JSONL log for stats and history queries."""

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .fingerprint import TrackInfo

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = Path.home() / ".local" / "share" / "spindle" / "history.jsonl"


@dataclass
class HistoryEntry:
    artist: str
    title: str
    album: str
    duration: Optional[int]
    timestamp: float  # unix epoch
    source: str  # "live", "backfill", "queue_flush"

    def to_dict(self) -> dict:
        return {
            "artist": self.artist,
            "title": self.title,
            "album": self.album,
            "duration": self.duration,
            "timestamp": self.timestamp,
            "source": self.source,
        }

    @staticmethod
    def from_dict(d: dict) -> "HistoryEntry":
        return HistoryEntry(
            artist=d["artist"],
            title=d["title"],
            album=d.get("album", ""),
            duration=d.get("duration"),
            timestamp=d["timestamp"],
            source=d.get("source", "live"),
        )


class ScrobbleHistory:
    """Append-only JSONL log of all scrobbles."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or DEFAULT_HISTORY_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(self, track: TrackInfo, timestamp: float,
            source: str = "live") -> None:
        entry = HistoryEntry(
            artist=track.artist,
            title=track.title,
            album=track.album or "",
            duration=track.duration,
            timestamp=timestamp,
            source=source,
        )
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(entry.to_dict()) + "\n")
        except OSError as e:
            logger.warning("Failed to write history: %s", e)

    def recent(self, count: int = 10) -> list[HistoryEntry]:
        """Get the most recent N entries."""
        if not self.path.exists():
            return []
        try:
            lines = self.path.read_text().strip().split("\n")
            entries = []
            for line in reversed(lines):
                if not line.strip():
                    continue
                try:
                    entries.append(HistoryEntry.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
                if len(entries) >= count:
                    break
            return entries
        except OSError:
            return []

    def stats(self) -> dict:
        """Get scrobble statistics."""
        if not self.path.exists():
            return {"total": 0, "today": 0, "week": 0, "top_artists": []}

        now = time.time()
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0,
        ).timestamp()
        week_start = now - 7 * 86400

        total = 0
        today = 0
        week = 0
        artist_counts: dict[str, int] = {}

        try:
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    total += 1
                    ts = d.get("timestamp", 0)
                    if ts >= today_start:
                        today += 1
                    if ts >= week_start:
                        week += 1
                    artist = d.get("artist", "Unknown")
                    artist_counts[artist] = artist_counts.get(artist, 0) + 1
        except OSError:
            pass

        top = sorted(artist_counts.items(), key=lambda x: -x[1])[:5]

        return {
            "total": total,
            "today": today,
            "week": week,
            "top_artists": top,
        }

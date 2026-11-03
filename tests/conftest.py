import pytest

from spindle.fingerprint import TrackInfo
from spindle.spotify import AlbumTracklist, SpotifyTrack


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class FakeSpotify:
    """Serves one album's tracklist; no network."""

    def __init__(self, tracklist: AlbumTracklist):
        self.tracklist = tracklist

    def get_album_tracklist(self, album_id: str):
        return self.tracklist if album_id == self.tracklist.album_id else None

    def lookup(self, artist: str, title: str):
        for i, t in enumerate(self.tracklist.tracks):
            if t.title.lower() == title.lower():
                return SpotifyTrack(track=t, album_id=self.tracklist.album_id,
                                    album_name=self.tracklist.album_name,
                                    track_number=i + 1, total_tracks=len(self.tracklist.tracks),
                                    album_art_url="https://example.invalid/art.jpg")
        return None

    def fetch_art(self, url):
        return None


def make_album(durations, artist="Madvillain", album="Madvillainy", album_id="alb1"):
    tracks = [
        TrackInfo(title=f"Track {i + 1}", artist=artist, album=album, duration=d,
                  source="spotify_album", confidence=1.0)
        for i, d in enumerate(durations)
    ]
    return AlbumTracklist(album_id=album_id, album_name=album, artist=artist,
                          tracks=tracks, release_date="2004-03-23")


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def album():
    return make_album([115, 72, 109, 68, 150, 234])

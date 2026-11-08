import pytest

from spindle.fingerprint import TrackInfo
from spindle.resolve import Resolved
from spindle.tracklist import AlbumTracklist


class FakeClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def make_album(durations, artist="Madvillain", album="Madvillainy", album_id="alb1",
               positions=None, owned=False, source="spotify", titles=None):
    tracks = [
        TrackInfo(title=(titles[i] if titles else f"Track {i + 1}"), artist=artist, album=album,
                  duration=d, source="spotify_album", confidence=1.0)
        for i, d in enumerate(durations)
    ]
    return AlbumTracklist(album_id=album_id, album_name=album, artist=artist, tracks=tracks,
                          release_date="2004-03-23", positions=list(positions or []),
                          owned=owned, source=source)


def resolved_for(tracklist: AlbumTracklist, title: str, art_url="https://example.invalid/a.jpg"):
    """A Resolved pointing at `title` inside `tracklist` (what the resolver would hand back)."""
    idx = tracklist.find_track_index(title)
    track = tracklist.tracks[idx] if idx is not None else TrackInfo(title=title,
                                                                      artist=tracklist.artist)
    return Resolved(track=track, album_id=tracklist.album_id, tracklist=tracklist,
                    art_url=art_url, owned=tracklist.owned, reason="test")


class FakeResolver:
    """Resolves by title against one or more known albums; unknown titles -> no album."""

    def __init__(self, *albums: AlbumTracklist):
        self.albums = list(albums)
        self.calls = []

    def resolve(self, track: TrackInfo, ctx=None) -> Resolved:
        self.calls.append((track.title, ctx))
        for al in self.albums:
            if al.find_track_index(track.title) is not None:
                return resolved_for(al, track.title)
        return Resolved(track=track, reason="none")       # as the real Resolver reports it


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def album():
    return make_album([115, 72, 109, 68, 150, 234])

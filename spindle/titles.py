"""Song titles as the catalogues write them, taken apart for the screen.

Streaming names carry the credits and the edition inside the title: "Demonz (feat. Brent
Faiyaz) - Interlude", "All The Stars (with SZA) - From 'Black Panther'", "Heroes (2017
Remaster)", "Mo Bamba [Remix]". Scrobbles keep the full name (that is what Last.fm
aggregates on); the screen shows the song, names the features on their own line and
keeps the edition as a small tag. Everything here is conservative: a bracket or a dash
is only taken off when its text is a credit or a known version word, so a title that
really contains brackets ("Intro (Skit)" keeps Skit as its version, "(Sandy) Alex G"
keeps everything) is left whole when in doubt.
"""
from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# a credit lead: "feat. X", "ft X", "featuring X", "w/ X"; "with X" only in Spotify's
# lowercase form (a title's own "(With Me)" is Title Case)
_FEAT_LEAD = re.compile(r"^(?:feat\.?|ft\.?|featuring|w/)\s+(?P<names>.+)$", re.IGNORECASE)
_WITH_LEAD = re.compile(r"^with\s+(?P<names>.+)$")
# a trailing bracketed group, or a " - " suffix, at the very end of the title
_TAIL_BRACKET = re.compile(r"^(?P<head>.*\S)\s*[\(\[]\s*(?P<inner>[^\(\)\[\]]+?)\s*[\)\]]\s*$")
_TAIL_DASH = re.compile(r"^(?P<head>.*\S)\s+[-–—]\s+(?P<inner>[^-–—]+?)\s*$")
_BARE_FEAT = re.compile(r"^(?P<head>.*\S)\s+(?:feat\.?|ft\.|featuring)\s+(?P<names>.+)$",
                        re.IGNORECASE)
# version words a bracket or a dash suffix may hold; "Pt. 2" / "Part 2" are identity, not here
_VERSION = re.compile(
    r"^(?:"
    r"live(?:\s.*)?|interlude|skit|intro|outro|reprise|"
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+version)?|"
    r"(?:.*\s)?remix|(?:.*\s)?mix|(?:.*\s)?edit|(?:.*\s)?version|"
    r"radio edit|single version|album version|extended(?:\s+version|\s+mix)?|original mix|"
    r"instrumental|acoustic|a ?cappella|demo|mono|stereo|unplugged|"
    r"bonus track|bonus|explicit|clean|sped up|slowed(?:\s*\+\s*reverb)?|"
    r"alternate(?:\s+take|\s+version)?|take \d+|rehearsal|session|"
    r"from\s.*"
    r")$",
    re.IGNORECASE,
)
_SPLIT_NAMES = re.compile(r"\s*(?:,|&|\band\b|/|\+)\s*", re.IGNORECASE)
# album editions: taken off the album line, never off what is scrobbled
_ALBUM_EDITION = re.compile(
    r"^(?:"
    r"bonus track(?:s)?(?:\s+version|\s+edition)?|deluxe(?:\s+edition|\s+version)?|"
    r"super deluxe(?:\s+edition)?|"
    r"expanded(?:\s+edition|\s+version)?|extended(?:\s+edition|\s+version)?|"
    r"(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?(?:\s+edition|\s+version)?|"
    r"(?:.*\s)?anniversary(?:\s+edition|\s+version)?|special edition|limited edition|"
    r"collector'?s edition|"
    r"complete edition|tour edition|japan(?:ese)? edition|international version|"
    r"explicit(?:\s+version)?|clean(?:\s+version)?|re-?issue|the remixes?|"
    r"(?:.*\s)?edition|(?:.*\s)?version"
    r")$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TitleParts:
    base: str                       # the song: "Demonz"
    featured: tuple[str, ...] = ()  # ("Brent Faiyaz",)
    version: str = ""               # "Interlude", "2017 Remaster", "Live" ... or ""

    @property
    def feat_line(self) -> str:
        return "feat. " + ", ".join(self.featured) if self.featured else ""


def _names(text: str, known: Sequence[str]) -> list[str]:
    """Credited names out of "Drake, Swae Lee & Big Hawk". Names the catalogue already
    lists are matched whole first, so "Tyler, The Creator" survives the comma."""
    out: list[str] = []
    rest = text.strip()
    for name in sorted((k for k in known if k), key=len, reverse=True):
        if name.lower() in rest.lower():
            out.append(name)
            i = rest.lower().index(name.lower())
            rest = (rest[:i] + " " + rest[i + len(name):]).strip()
    for piece in _SPLIT_NAMES.split(rest):
        piece = piece.strip(" .")
        if piece and piece.lower() not in {n.lower() for n in out}:
            out.append(piece)
    return out


def split_title(title: str, artists: Sequence[str] = ()) -> TitleParts:
    """Take the credits and the edition off the end of a catalogue title.

    `artists` is the catalogue's own list for the track (primary first) when known; the
    features are then the names after the primary, whatever the title says.
    """
    base = (title or "").strip()
    featured: list[str] = []
    versions: list[str] = []
    known = [a for a in artists if a]
    primary = known[0] if known else ""
    while True:
        m = _TAIL_BRACKET.match(base) or _TAIL_DASH.match(base)
        if not m:
            m2 = _BARE_FEAT.match(base)
            if m2:
                featured = _names(m2.group("names"), known) + featured
                base = m2.group("head").strip()
                continue
            break
        inner = m.group("inner").strip()
        lead = _FEAT_LEAD.match(inner) or _WITH_LEAD.match(inner)
        if lead:
            featured = _names(lead.group("names"), known) + featured
        elif _VERSION.match(inner):
            versions.insert(0, inner)
        else:
            break
        base = m.group("head").strip()
    if not base:                                    # the whole title was a credit: keep it
        return TitleParts(base=(title or "").strip())
    # the catalogue's list wins over whatever the title spelled
    if len(known) > 1:
        listed = [a for a in known[1:] if a.lower() != primary.lower()]
        for name in listed:
            if name.lower() not in {f.lower() for f in featured}:
                featured.append(name)
    if primary:
        featured = [f for f in featured if f.lower() != primary.lower()]
    uniq: list[str] = []
    for f in featured:
        if f.lower() not in {u.lower() for u in uniq}:
            uniq.append(f)
    return TitleParts(base=base, featured=tuple(uniq), version=" · ".join(versions))


def clean_album(name: str) -> str:
    """The album without its edition tag: "Death Race For Love (Bonus Track Version)" ->
    "Death Race For Love"; "Love Deluxe" stays (no bracket, no dash)."""
    out = (name or "").strip()
    while True:
        m = _TAIL_BRACKET.match(out) or _TAIL_DASH.match(out)
        if not m or not _ALBUM_EDITION.match(m.group("inner").strip()):
            return out
        out = m.group("head").strip() or out
        if out == name:
            return out

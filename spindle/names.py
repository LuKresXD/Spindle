"""Names, compared as names.

Three modules each compared artists with a substring test, so "Ye" was "Yeat" and "Kanye
West", "Che" was "Cher", and "¥$" (which normalised to the single letter "s") was everybody
with an s in the name. And every normaliser kept Latin and Cyrillic letters only, so a title
in Greek, Japanese or Korean normalised to nothing and could never match anything.

Here a name becomes a key (case, accents, punctuation and Discogs' "(2)" suffix folded away,
"$" read as the "s" it stands for, letters of every script kept), a credit line is taken
apart into its separate names, and two credits are the same artist when they share one
whole name. What a pair of strings cannot say (Ye is Kanye West, Кино is Kino) comes from
data: the collection adds the name variations and aliases Discogs lists for an artist.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

_VARIOUS = {"various", "various artists"}
# what separates the names in a credit line; " x " and the words only between two names
_SEP = re.compile(r"\s*(?:,|&|/|\+|;)\s*|\s+(?:x|×|and|with|vs\.?|feat\.?|ft\.?|featuring)\s+",
                  re.IGNORECASE)
_DISCOGS_NUMBER = re.compile(r"\s*\(\d+\)(?=\s*(?:$|[,&/+;]))")


def fold(s: str) -> str:
    """Lower case with accents and other marks removed: "JAŸ-Z" -> "jay-z", "Alivë" -> "alive"."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def words(s: str) -> str:
    """The letters and digits of an already folded string, any script, single-spaced."""
    return " ".join(re.sub(r"[\W_]+", " ", s).split())


@lru_cache(maxsize=4096)
def artist_key(name: str) -> str:
    """One artist's name as a comparison key."""
    s = fold(name).replace("'", "").replace("’", "")
    s = _DISCOGS_NUMBER.sub("", s)
    s = re.sub(r"(?<=[a-z0-9])\$|\$(?=[a-z0-9])", "s", s)      # A$AP, Ty Dolla $ign, Ke$ha
    key = words(s)
    if key.startswith("the ") and len(key) > 4:
        key = key[4:]
    return key or " ".join(s.split())           # "¥$" has no letter to fold to: it is its own key


def artist_names(credit: str) -> list[str]:
    """The separate names in a credit line: "A, B & C feat. D" -> ["A", "B", "C", "D"]."""
    s = _DISCOGS_NUMBER.sub("", credit or "")
    s = re.sub(r"[()\[\]]", " ", s)
    return [p.strip() for p in _SEP.split(s) if p and p.strip()]


@lru_cache(maxsize=4096)
def artist_keys(credit: str) -> frozenset[str]:
    """Every key a credit answers to: the whole line (a name may contain a comma: "Tyler, The
    Creator") and each separate name in it."""
    keys = {artist_key(credit)}
    keys.update(k for k in (artist_key(p) for p in artist_names(credit)) if len(k) > 1)
    keys.discard("")
    return frozenset(keys)


def same_artist(a: str, b: str) -> bool:
    """Do two credit lines share an artist? Whole names only: "Ye" is not "Yeat"."""
    return bool(artist_keys(a) & artist_keys(b))


def is_various(name: str) -> bool:
    return artist_key(name) in _VARIOUS

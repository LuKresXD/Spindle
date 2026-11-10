#!/usr/bin/env python3
"""Check the resolver and the session against your own shelf.

Unit tests cover the rules; this covers the records. It reads the Discogs collection cache
Spindle keeps (nothing is asked of Discogs), names each track the way a fingerprint would,
and reports where the engine would not land on your pressing, at the right position, with
every track of the side scrobbled once.

    python tools/shelf_replay.py lookups A.json     # first track of every side + the last track
    python tools/shelf_replay.py sides A.json       # play every side through the session
    python tools/shelf_replay.py diff A.json B.json # what changed between two runs

    --journal FILE   also resolve every "Identified via ShazamIO: artist - title" in a saved
                     journal (journalctl -u spindle > FILE): the names Shazam really returned
    --cache-dir DIR  where collection-<user>.json lives (default ~/.cache/spindle/discogs)
    --fixtures DIR   recorded catalogue answers (default ~/.cache/spindle/replay)

The catalogue is Deezer's public API (no key, no account). Every answer is recorded under
--fixtures and replayed from there the next time, so a second run costs no network and two
versions of the code see exactly the same catalogue: run it before a change and after, then
`diff`. Live calls are spaced 0.2 s apart (Deezer allows 50 in 5 s). Spotify is never asked:
its limits are per app, and the app's credentials are the ones your deck runs on.

Discogs titles are a stand-in for fingerprints, and a generous one (a fingerprint may name a
track differently). `--journal` adds the real strings for whatever has been played.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import logging
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from spindle.deezer import Catalogue, DeezerClient  # noqa: E402
from spindle.discogs import DiscogsCollection, clean_artist  # noqa: E402
from spindle.fingerprint import TrackInfo  # noqa: E402
from spindle.resolve import ResolveContext, Resolver  # noqa: E402
from spindle.session import ScrobbleSession  # noqa: E402
from spindle.tracklist import side_of, titles_match  # noqa: E402

LIVE_GAP = 0.2
HIT_EVERY = 8           # seconds between identifications while a side plays
UNKNOWN_LENGTH = 200    # seconds a track is "played" when nobody knows its length


class _Answer:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._data


class Recorded:
    """A requests.Session stand-in: the disk first, then the network (spaced), recorded."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.headers: dict = {}
        self.live = self.replayed = 0
        self._http = requests.Session()
        self._last = 0.0

    def get(self, url, params=None, timeout=10, **_):
        key = hashlib.sha1(json.dumps([url, sorted((params or {}).items())],
                                      ensure_ascii=False).encode()).hexdigest()[:24]
        path = self.dir / f"{key}.json"
        if path.exists():
            self.replayed += 1
            return _Answer(json.loads(path.read_text()))
        data: object = {}
        for _attempt in range(4):
            wait = LIVE_GAP - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            resp = self._http.get(url, params=params, timeout=timeout,
                                  headers={"User-Agent": "spindle-replay/0.1"})
            self._last = time.time()
            self.live += 1
            resp.raise_for_status()
            data = resp.json()
            err = data.get("error") if isinstance(data, dict) else None
            if not (err and err.get("code") == 4):
                break
            time.sleep(6)                      # quota: one window, then ask again
        path.write_text(json.dumps(data, ensure_ascii=False))
        return _Answer(data)


def fingerprint_artist(rel, track: dict) -> str:
    """Who a fingerprint would name: the track's own artist, else the record's first."""
    return (clean_artist((track.get("artist") or "").split(",")[0])
            or clean_artist(rel.artist.split(",")[0]))


def describe(fp: TrackInfo, out) -> dict:
    tl = out.tracklist
    row = {"q_artist": fp.artist, "q_title": fp.title, "reason": out.reason, "owned": out.owned,
           "album": tl.album_name if tl else "", "src": tl.source if tl else "",
           "n": len(tl.tracks) if tl else 0, "name": out.track.title, "by": out.track.artist,
           "dur": out.track.duration}
    if tl:
        idx = tl.find_track_index(out.track.title)
        row["pos"] = tl.position_at(idx) if idx is not None else None
        row["no_dur"] = sum(1 for t in tl.tracks if not t.duration)
        row["rid"] = tl.discogs_release_id
    return row


def journal_pairs(path: Path) -> dict:
    pairs: dict = collections.OrderedDict()
    for line in path.read_text(errors="replace").splitlines():
        m = re.search(r"Identified via ShazamIO: (.+)$", line)
        if m and " - " in m.group(1):
            artist, title = m.group(1).strip().split(" - ", 1)
            key = (artist.strip(), title.strip())
            pairs[key] = pairs.get(key, 0) + 1
    return pairs


def _same_album(col, rid: int | None, rel) -> bool:
    """Is release `rid` another copy of `rel`'s album on the shelf? Either is "your pressing"."""
    other = col.release_by_id(rid) if rid else None
    return (other is not None and other.artist == rel.artist
            and other.title.strip().lower() == rel.title.strip().lower())


def run_lookups(col, http, journal: Path | None) -> list[dict]:
    resolver = Resolver(Catalogue(None, DeezerClient(session=http)), col)
    rows = []
    for rel in sorted(col.all_releases(), key=lambda r: (r.artist, r.title, r.id)):
        if not rel.tracks:
            continue
        picks, seen = [], set()
        for i, t in enumerate(rel.tracks):
            side = side_of(t.get("position", ""))
            if side not in seen:
                seen.add(side)
                picks.append(i)
        if len(rel.tracks) - 1 not in picks:
            picks.append(len(rel.tracks) - 1)
        for i in picks[:7]:
            t = rel.tracks[i]
            fp = TrackInfo(title=t.get("title", ""), artist=fingerprint_artist(rel, t),
                           source="shazam")
            row = describe(fp, resolver.resolve(fp))
            row.update(set="shelf", rel=rel.title.strip(), rel_artist=rel.artist, rel_id=rel.id,
                       want_pos=t.get("position", ""))
            if not row["album"]:
                row["kind"] = "NONE"
            elif not row["owned"]:
                row["kind"] = "NOT-OWNED"
            elif row["src"] not in ("merged", "discogs"):
                row["kind"] = "STREAMING-LIST"
            elif row["rid"] != rel.id and not _same_album(col, row["rid"], rel):
                row["kind"] = "OTHER-RELEASE"
            elif row.get("pos") != row["want_pos"]:
                row["kind"] = "WRONG-POS"
            else:
                row["kind"] = "ok"
            rows.append(row)
    for (artist, title), heard in (journal_pairs(journal) if journal else {}).items():
        fp = TrackInfo(title=title, artist=artist, source="shazam")
        row = describe(fp, resolver.resolve(fp))
        row.update(set="journal", heard=heard,
                   kind="owned" if row["owned"] else ("stranger" if row["album"] else "none"))
        rows.append(row)
    return rows


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def play_side(resolver, rel, indices: list[int]) -> dict:
    """One side through the real resolver and session: a hit every HIT_EVERY seconds with an
    exact offset, the clock ticking every 2 s, silence at the end."""
    clock = _Clock()
    session = ScrobbleSession(clock=clock)
    t0 = clock.t
    scrobbled: list[tuple[str, str]] = []
    locks: list[str] = []

    def emit(pairs) -> None:
        scrobbled.extend((t.title, t.album or "") for t, _ in pairs)

    def ctx() -> ResolveContext:
        return ResolveContext(locked_album_id=session.locked_album_id,
                              locked_artist=session.locked_artist,
                              locked_release_id=session.locked_release_id)

    start = t0
    for i in indices:
        track = rel.tracks[i]
        artist, title = fingerprint_artist(rel, track), track.get("title", "")
        first = resolver.resolve(TrackInfo(title=title, artist=artist, source="shazam"), ctx())
        length = first.track.duration or track.get("duration") or UNKNOWN_LENGTH
        end, at, tick = start + length, start + HIT_EVERY, start
        while tick < end:
            clock.t = tick
            emit(session.check_advance())
            if abs(tick - at) < 1.0 and at < end - 1:
                hit = TrackInfo(title=title, artist=artist, source="shazam",
                                offset=(at - 6) - start)
                res = resolver.resolve(hit, ctx())
                if res.has_album or session.mode is not None:
                    emit(session.on_identified(res, t0, at=at))
                at += HIT_EVERY
                al = session.album_state
                if al and (not locks or locks[-1] != al.tracklist.album_name):
                    locks.append(al.tracklist.album_name)
            tick += 2
        start = end
    clock.t = start + 8
    al = session.album_state
    info = {"lock": locks, "owned": bool(al and al.tracklist.owned),
            "rid": al.tracklist.discogs_release_id if al else None}
    emit(session.on_silence())
    info["scrobbled"] = scrobbled
    return info


def run_sides(col, http) -> list[dict]:
    rows = []
    seen: set = set()
    for rel in sorted(col.all_releases(), key=lambda r: (r.artist, r.title, r.id)):
        key = (rel.artist, rel.title.strip())
        if key in seen or not rel.tracks:
            continue
        seen.add(key)
        resolver = Resolver(Catalogue(None, DeezerClient(session=http)), col)   # a fresh start
        sides: dict = collections.OrderedDict()
        for i, t in enumerate(rel.tracks):
            sides.setdefault(side_of(t.get("position", "")) or "?", []).append(i)
        if list(sides) == ["?"]:               # no side letters at all: play it in runs of 6
            idx = sides["?"]
            sides = collections.OrderedDict(
                (f"?{k // 6 + 1}", idx[k:k + 6]) for k in range(0, len(idx), 6))
        for side, indices in sides.items():
            want = [rel.tracks[i].get("title", "") for i in indices]
            info = play_side(resolver, rel, indices)
            got = [title for title, _ in info["scrobbled"]]
            # Last.fm takes no track under 30 s: a skit that short is not missing
            short = {rel.tracks[i].get("title", "") for i in indices
                     if (rel.tracks[i].get("duration") or 99) < 30}
            missing = [w for w in want
                       if w not in short and not any(titles_match(w, g) for g in got)]
            phantom = [g for g in got if not any(titles_match(w, g) for w in want)]
            dupes = len(got) - len(set(got))
            kind = "ok"
            if phantom:
                kind = "PHANTOM"
            elif dupes:
                kind = "DUPE"
            elif missing:
                kind = "MISSING"
            if not info["owned"] or (info["rid"] != rel.id
                                     and not _same_album(col, info["rid"], rel)):
                kind = "NOT-YOUR-PRESSING" if kind == "ok" else kind
            rows.append({"set": "sides", "rel": rel.title.strip(), "rel_artist": rel.artist,
                         "rel_id": rel.id, "q_artist": rel.artist, "q_title": f"side {side}",
                         "want_pos": side, "kind": kind, "want": want, "got": got,
                         "missing": missing, "phantom": phantom, "lock": info["lock"],
                         "albums": sorted({album for _, album in info["scrobbled"]})})
    return rows


def _key(row: dict) -> tuple:
    return (row["set"], row.get("rel_id"), row["q_artist"], row["q_title"], row.get("want_pos"))


def summary(rows: list[dict]) -> str:
    counts = collections.Counter((r["set"], r["kind"]) for r in rows)
    return "  ".join(f"{s}:{k}={n}" for (s, k), n in sorted(counts.items()))


def diff(a_path: Path, b_path: Path) -> None:
    a, b = json.loads(a_path.read_text()), json.loads(b_path.read_text())
    after = {_key(r): r for r in b}
    fields = ("kind", "owned", "album", "src", "rid", "pos", "name", "by", "missing", "phantom",
              "lock", "albums")
    changed = 0
    for row in a:
        other = after.get(_key(row))
        if other is None:
            print("only in A:", _key(row))
            continue
        delta = {f: (row.get(f), other.get(f)) for f in fields if row.get(f) != other.get(f)}
        if delta:
            changed += 1
            where = f" (on {row['rel']} {row['want_pos']})" if row.get("rel") else ""
            print(f"[{row['set']}] {row['q_artist']} - {row['q_title']}{where}")
            for f, (x, y) in delta.items():
                print(f"      {f}: {x!r} -> {y!r}")
    print(f"\n{changed} of {len(a)} rows differ\nA  {summary(a)}\nB  {summary(b)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("mode", choices=("lookups", "sides", "diff"))
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache/spindle/discogs")
    ap.add_argument("--user", default="", help="Discogs user (default: the one cache in the dir)")
    ap.add_argument("--fixtures", type=Path, default=Path.home() / ".cache/spindle/replay")
    ap.add_argument("--journal", type=Path)
    args = ap.parse_args()
    if args.mode == "diff":
        if len(args.files) != 2:
            ap.error("diff takes two files")
        diff(*args.files)
        return
    logging.basicConfig(level=logging.ERROR)
    user = args.user
    if not user:
        caches = sorted(args.cache_dir.glob("collection-*.json"))
        if len(caches) != 1:
            ap.error(f"{len(caches)} collection caches in {args.cache_dir}: name one with --user")
        user = caches[0].stem.removeprefix("collection-")
    col = DiscogsCollection("-", user, cache_dir=args.cache_dir)   # the cache only: never started
    if not col.ready:
        ap.error(f"no collection cache for {user} in {args.cache_dir}")
    http = Recorded(args.fixtures)
    rows = run_lookups(col, http, args.journal) if args.mode == "lookups" else run_sides(col, http)
    args.files[0].write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"{len(rows)} rows -> {args.files[0]}   (catalogue: {http.live} live calls, "
          f"{http.replayed} replayed)\n{summary(rows)}")
    for row in rows:
        if row["kind"] not in ("ok", "owned", "stranger", "none"):
            print(f"  {row['kind']:<16} {row.get('rel', '')[:34]:<34} {row['q_title'][:40]}")


if __name__ == "__main__":
    main()

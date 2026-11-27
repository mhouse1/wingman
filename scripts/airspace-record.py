"""Design 017 Phase 1: build each arena's free-airspace record from the session logs.

Reads the ``TERRAIN:`` fixes (Design 020's shadow position fix) and the HUD altitudes logged
around them, keeps the samples Design 017's rules accept, and writes one record per arena
(``wingman/airspace.py``) beside Design 020's data, as ``<arena>-airspace.npz`` in the folder
that holds the arena's height map. Then prints Phase 1's checks: coverage by height band, flown
floors against the mesh, crash samples against the ground, and the fixes logged while the
operator flew (Phase 0's open item).

Log-side rules (the rest are in wingman/airspace.py):

- a sample is a ``TERRAIN:`` line with a fix, not a carried position;
- its altitude is the higher of the two HUD readings logged either side of it, each within
  ``--bracket-s`` and each clean: a number (the parser's signed rule of 2026-11-08 is applied
  before the PADLOCK line), not rejected by the plausibility filter. The higher one, so a
  climb read late never lowers the floor;
- not in the last 8 s before a death, nor in the death camera after it (a death is the
  ``RespawnLatency`` OCR edge minus its ``no_digits_for``). GAME_BATTLE_EJECT counts: it is
  "missiles empty", and the resupply run is flown in it;
- a life ends at each death, each MATCH line and each entry into a battle state: two samples
  are one pass only within one life.

Crash samples come from deaths whose DEATH CARD found no kill card (``wingman/kill_card.py``):
the last fix within 4 s before the death, carried to the death along the last step, at the
last clean altitude carried at its rate. Re-arm points are the last fix 3 to 6 s before
``RESUPPLY: confirmed ammo``, at the last clean altitude read before it (Design 020's method).

Usage:
    uv run --active python scripts/airspace-record.py [LOG ...] [--since YYYY-MM-DD]
        [--config wingman/config.yaml] [--no-write] [--json REPORT.json]

With no LOG, every ``logs/*.log`` and every log kept in the cache. Each rotated log's extraction is
cached in ``test_screenshots/terrain/airspace-samples/``, so its flights still count after wingman
prunes the log (the rotated logs keep about eight days). The live ``wingman.log`` is read only when
named, and never cached.
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import glob
import json
import math
import os
import re
import sys
import time

import numpy as np
import yaml

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from wingman.airspace import (Crash, HeightMap, Rearm, Sample, build_record,  # noqa: E402
                              mesh_check)

PRE_DEATH_S = 8.0
BRACKET_S = 2.0
CRASH_FIX_MAX_AGE_S = 4.0
REARM_FIX_WINDOW_S = 6.0
REARM_LEAD_S = 3.0
REARM_ALT_WINDOW_S = 3.0       # a re-arm's altitude: the nearest clean reading this close
MAX_MISSILES = 12              # a missile count above this is a misread ('75' seen in manual flight)
_RE_MISSILES = re.compile(r"\bmissiles=(\d+|None)")
REJECT_PAIR_S = 0.05          # the filter's warning and its PADLOCK line share a millisecond
BATTLE = {"GAME_BATTLE", "GAME_BATTLE_MANUAL", "GAME_BATTLE_EJECT"}

_RE_TERRAIN = re.compile(r"TERRAIN: map=(.+?) fix=(?:(-?\d+),(-?\d+)|none)")
_RE_ALT = re.compile(r"PADLOCK: \w+ \| Altitude: (-?\d+|None)")
_RE_EDGE = re.compile(r"no_digits_for=(\d+(?:\.\d+)?)s")
_RE_STATE = re.compile(r"Entering state (\w+)\.")

_epoch_memo: dict = {}


def _epoch(line: str) -> float:
    key = line[:19]
    base = _epoch_memo.get(key)
    if base is None:
        base = dt.datetime.strptime(key, "%Y-%m-%d %H:%M:%S").timestamp()
        if len(_epoch_memo) > 200_000:
            _epoch_memo.clear()
        _epoch_memo[key] = base
    return base + int(line[20:23]) / 1000.0


def _is_line(line: str) -> bool:
    return len(line) > 24 and line[4] == "-" and line[:4].isdigit() and line[19] == ","


class LogEvents:
    """What one log says, in time order per kind."""

    def __init__(self, path: str):
        self.path = path
        self.fixes: list = []          # (t, map_name, x, z)
        self.terrain_lines: list = []  # (t, map_name, has_fix)
        self.readings: list = []       # (t, alt or None, clean)
        self.deaths: list = []         # [t_death, t_edge, crash flag or None]
        self.cards: list = []          # (t, crash)
        self.states: list = []         # (t, state)
        self.matches: list = []        # t
        self.rearms: list = []         # t
        self.missiles: list = []       # (t, count or None) from the BT[active] tick line
        self.first_t: "float | None" = None


def parse_log(path: str) -> LogEvents:
    ev = LogEvents(path)
    reject_t = None
    with open(path, errors="replace") as fh:
        for line in fh:
            if not _is_line(line):
                continue
            if "TERRAIN: map=" in line:
                m = _RE_TERRAIN.search(line)
                if not m:
                    continue
                t = _epoch(line)
                has = m.group(2) is not None
                ev.terrain_lines.append((t, m.group(1), has))
                if has:
                    ev.fixes.append((t, m.group(1), float(m.group(2)), float(m.group(3))))
            elif "PADLOCK: " in line:
                m = _RE_ALT.search(line)
                if not m:
                    continue
                t = _epoch(line)
                value = None if m.group(1) == "None" else float(m.group(1))
                clean = value is not None and not (reject_t is not None and t - reject_t <= REJECT_PAIR_S)
                reject_t = None
                ev.readings.append((t, value, clean))
            elif "plausibility filter rejected" in line:
                reject_t = _epoch(line)
            elif "Entering state " in line:
                m = _RE_STATE.search(line)
                if m:
                    ev.states.append((_epoch(line), m.group(1)))
            elif "RespawnLatency: OCR edge" in line:
                t = _epoch(line)
                m = _RE_EDGE.search(line)
                gone = float(m.group(1)) if m else 0.0
                ev.deaths.append([t - gone, t, None])
            elif "DEATH CARD:" in line and "DEATH CARDS" not in line:
                ev.cards.append((_epoch(line), "no kill card" in line))
            elif "MATCH: " in line:
                ev.matches.append(_epoch(line))
            elif "BT[active]: selected=" in line:
                m = _RE_MISSILES.search(line)
                if m:
                    ev.missiles.append((_epoch(line), None if m.group(1) == "None" else int(m.group(1))))
            elif "RESUPPLY: confirmed ammo" in line:
                ev.rearms.append(_epoch(line))
            if ev.first_t is None:
                ev.first_t = _epoch(line)
    # Each card closes the latest death whose edge came before it.
    edges = [d[1] for d in ev.deaths]
    for tc, crash in ev.cards:
        k = bisect.bisect_right(edges, tc) - 1
        if k >= 0 and tc - edges[k] <= 30.0 and ev.deaths[k][2] is None:
            ev.deaths[k][2] = crash
    return ev


class Timeline:
    """Lives, game states and excluded spans of one log."""

    def __init__(self, ev: LogEvents, life_base: int):
        bounds = [d[0] for d in ev.deaths] + list(ev.matches)
        prev = None
        for t, state in ev.states:
            if state in BATTLE and (prev is None or prev not in BATTLE):
                bounds.append(t)
            prev = state
        self.bounds = sorted(bounds)
        self.life_base = life_base
        self.state_t = [t for t, _ in ev.states]
        self.state_v = [s for _, s in ev.states]
        self.excluded = sorted((d[0] - PRE_DEATH_S, d[1]) for d in ev.deaths)
        self._ex_start = [a for a, _ in self.excluded]

    def life(self, t: float) -> int:
        return self.life_base + bisect.bisect_right(self.bounds, t)

    @property
    def lives(self) -> int:
        return len(self.bounds) + 1

    def state(self, t: float) -> "str | None":
        k = bisect.bisect_right(self.state_t, t) - 1
        return self.state_v[k] if k >= 0 else None

    def excluded_at(self, t: float) -> bool:
        k = bisect.bisect_right(self._ex_start, t) - 1
        # spans can overlap; check back while they could still cover t
        while k >= 0:
            a, b = self.excluded[k]
            if a <= t <= b:
                return True
            if b < t - 60.0:
                break
            k -= 1
        return False


def bracket_altitude(readings_t, readings, t: float, tl: Timeline, window: float = BRACKET_S):
    """(altitude, reason): the higher of the clean readings either side of t within `window`,
    both in t's life; or (None, why not)."""
    k = bisect.bisect_right(readings_t, t) - 1
    if k < 0 or k + 1 >= len(readings):
        return None, "no-alt"
    (tp, vp, cp), (tn, vn, cn) = readings[k], readings[k + 1]
    if t - tp > window or tn - t > window:
        return None, "no-alt"
    if not (cp and cn):
        return None, "unclean"
    life = tl.life(t)
    if tl.life(tp) != life or tl.life(tn) != life:
        return None, "life-split"
    return max(vp, vn), None


def _nearest_clean(readings_t, readings, t: float, window: float):
    """The clean reading nearest t, before or after it, within `window`; None if there is none.
    A re-arm is one point: the operator's level pass over a Stormbreak stack on 2026-11-26 had
    both readings before its fix rejected for a speed misread (9 kph, then 809), and was dropped."""
    k = bisect.bisect_right(readings_t, t)
    best = None
    for j in range(max(0, k - 6), min(len(readings), k + 6)):
        tj, _, clean = readings[j]
        if clean and abs(tj - t) <= window and (best is None or abs(tj - t) < abs(readings[best][0] - t)):
            best = j
    return best


def _last_clean(readings_t, readings, t: float, max_age: float):
    k = bisect.bisect_right(readings_t, t) - 1
    while k >= 0 and t - readings[k][0] <= max_age:
        if readings[k][2]:
            return k
        k -= 1
    return None


def extract(ev: LogEvents, arena_of: dict, life_base: int):
    """Samples, crashes and re-arms per arena from one log, and the rule counts."""
    tl = Timeline(ev, life_base)
    rt = [r[0] for r in ev.readings]
    out = collections.defaultdict(lambda: {"samples": [], "crashes": [], "rearms": []})
    counts = collections.Counter()
    manual = collections.Counter()
    for t, _name, has in ev.terrain_lines:
        if tl.state(t) == "GAME_BATTLE_MANUAL":
            manual["lines"] += 1
            manual["fixes"] += int(has)
    for t, name, x, z in ev.fixes:
        arena = arena_of.get(name)
        if arena is None:
            counts["unknown-map"] += 1
            continue
        # GAME_BATTLE_EJECT is "missiles empty" (state.py), not a death: the aircraft flies
        # its resupply in it, so it is kept. The death camera is the excluded span.
        state = tl.state(t)
        if tl.excluded_at(t):
            counts["pre-death"] += 1
            continue
        alt, why = bracket_altitude(rt, ev.readings, t, tl)
        if alt is None:
            counts[why] += 1
            continue
        is_manual = state == "GAME_BATTLE_MANUAL"
        manual["samples"] += int(is_manual)
        counts["accepted"] += 1
        out[arena]["samples"].append(Sample(t, x, z, alt, tl.life(t), is_manual))

    fix_t = [f[0] for f in ev.fixes]
    for t_death, _t_edge, crash in ev.deaths:
        if not crash:
            continue
        counts["crash-deaths"] += 1
        k = bisect.bisect_right(fix_t, t_death + 0.5) - 1
        if k < 0 or t_death - ev.fixes[k][0] > CRASH_FIX_MAX_AGE_S:
            counts["crash-no-fix"] += 1
            continue
        tf, name, x, z = ev.fixes[k]
        arena = arena_of.get(name)
        if arena is None:
            continue
        age = max(0.0, t_death - tf)
        if k > 0 and tl.life(ev.fixes[k - 1][0]) == tl.life(tf):
            tq, _, xq, zq = ev.fixes[k - 1]
            if 0 < tf - tq <= BRACKET_S:
                vx, vz = (x - xq) / (tf - tq), (z - zq) / (tf - tq)
                if math.hypot(vx, vz) <= 700.0:
                    x, z = x + vx * age, z + vz * age
        r = _last_clean(rt, ev.readings, t_death, CRASH_FIX_MAX_AGE_S)
        if r is None:
            counts["crash-no-alt"] += 1
            continue
        tr, alt = ev.readings[r][0], ev.readings[r][1]
        q = _last_clean(rt, ev.readings, tr - 0.01, BRACKET_S + 0.5)
        if q is not None and tr - ev.readings[q][0] > 0:
            rate = (alt - ev.readings[q][1]) / (tr - ev.readings[q][0])
            alt = alt + rate * max(0.0, t_death - tr)
        out[arena]["crashes"].append(Crash(t_death, x, z, alt, age))
        counts["crash-samples"] += 1

    # Design 020's ring sites: the last fix and altitude at least REARM_LEAD_S before the
    # confirmation, which lands on the climb-out. Read after the fix instead, the 11-18 to 11-26
    # sites sat about 350 m above Design 020's at the same places.
    for t, by_hand in [(t, False) for t in ev.rearms] + [(t, True) for t in manual_rearms(ev, tl)]:
        k = bisect.bisect_right(fix_t, t - REARM_LEAD_S) - 1
        if k < 0 or t - ev.fixes[k][0] > REARM_FIX_WINDOW_S:
            continue
        tf, name, x, z = ev.fixes[k]
        arena = arena_of.get(name)
        r = _nearest_clean(rt, ev.readings, tf, REARM_ALT_WINDOW_S)
        if arena is None or r is None:
            continue
        out[arena]["rearms"].append(Rearm(tf, x, z, ev.readings[r][1], by_hand))
    return out, counts, manual, tl.lives


def manual_rearms(ev: LogEvents, tl: Timeline) -> list:
    """Times of re-arms flown by hand. Wingman logs `RESUPPLY: confirmed ammo` only from its own
    pursuit, so a ring the operator takes in GAME_BATTLE_MANUAL left no re-arm: 1 of 318 re-arms on
    record by 2026-11-26 was flown by hand. The tick line's missile count is still read then, noisily
    (it flickers to None, and once read 75), so a re-arm is a stable count (two equal reads running)
    rising to a stable higher one, both at most MAX_MISSILES, within one life and in manual flight."""
    out = []
    stable = None                  # (count, life) of the last stable reading
    last = None
    for t, n in ev.missiles:
        if n is None or n > MAX_MISSILES or tl.state(t) != "GAME_BATTLE_MANUAL":
            last = None
            if tl.state(t) != "GAME_BATTLE_MANUAL":
                stable = None
            continue
        life = tl.life(t)
        if last is not None and last[0] == n and last[1] == life:
            if stable is not None and stable[1] == life and n > stable[0]:
                out.append(last[2])            # the first read of the new count
            stable = (n, life)
        last = (n, life, t)
    return out


def arenas_from_config(path: str) -> "tuple[dict, dict, dict]":
    """(map name -> arena key, arena key -> height map path, arena key -> minimap texture).
    The key is the height map's file name without "-height-16m.npz": lighting variants that
    share a height map share a record, and Icebound's snow variant, with its own, gets its own."""
    with open(path) as fh:
        cfg = yaml.safe_load(fh)
    maps = ((cfg.get("terrain_service") or {}).get("maps")) or {}
    arena_of, height_of, minimap_of = {}, {}, {}
    for name, entry in maps.items():
        height = (entry or {}).get("height")
        if not height:
            continue
        key = re.sub(r"-height-\d+m\.npz$", "", os.path.basename(height))
        arena_of[name] = key
        height_of[key] = height
        minimap_of[key] = (entry or {}).get("minimap")
    return arena_of, height_of, minimap_of


def _fmt_q(q: dict, unit: str = "m") -> str:
    if not q.get("n"):
        return "none"
    return f"median {q['median']:+.0f} {unit} (p10 {q['p10']:+.0f}, p90 {q['p90']:+.0f}), n={q['n']}"


# ---- the per-log cache -------------------------------------------------------------------
# Wingman deletes its oldest rotated logs at each start (capture_budget rotated_logs, 2,048 MB:
# about eight days of looped sessions), so a record rebuilt from logs/*.log alone forgets every
# older flight. Each rotated log's extraction is kept here, keyed by its name, size and mtime,
# and still counts after the log is gone. Bump CACHE_VERSION when an extraction rule changes:
# logs on hand are then read again; a log already deleted keeps the extraction it had.
CACHE_DIR = "test_screenshots/terrain/airspace-samples"
CACHE_VERSION = 4               # 2: hand-flown re-arms; 3: re-arms carry `manual`; 4: nearest clean altitude
_COLUMNS = {"samples": 6, "crashes": 5, "rearms": 5}     # t,x,z,alt,life,manual | t,x,z,alt,age | t,x,z,alt,manual


def extract_log(path: str, arena_of: dict) -> "dict | None":
    """One log's extraction, its lives numbered from 0, as arrays; None when it holds no fix."""
    ev = parse_log(path)
    if not ev.fixes or ev.first_t is None:
        return None
    out, counts, manual, lives = extract(ev, arena_of, 0)
    arenas = {}
    for arena, d in out.items():
        arenas[arena] = {
            "samples": np.array([(s.t, s.x, s.z, s.alt, s.life, float(s.manual)) for s in d["samples"]],
                                dtype=np.float64).reshape(-1, 6),
            "crashes": np.array([(c.t, c.x, c.z, c.alt, c.fix_age_s) for c in d["crashes"]],
                                dtype=np.float64).reshape(-1, 5),
            "rearms": np.array([(r.t, r.x, r.z, r.alt, float(r.manual)) for r in d["rearms"]],
                               dtype=np.float64).reshape(-1, 5)}
    return {"first_t": ev.first_t, "span": [ev.fixes[0][0], ev.fixes[-1][0]], "lives": lives,
            "counts": dict(counts), "manual": dict(manual), "arenas": arenas}


def _save_cache(path: str, entry: "dict | None", st) -> None:
    meta = {"version": CACHE_VERSION, "size": st.st_size, "mtime": st.st_mtime, "empty": entry is None}
    arrays = {}
    if entry is not None:
        meta.update({k: entry[k] for k in ("first_t", "span", "lives", "counts", "manual")})
        meta["arenas"] = sorted(entry["arenas"])
        for arena, d in entry["arenas"].items():
            for kind, a in d.items():
                arrays[f"{kind}:{arena}"] = a
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp.npz"
    np.savez_compressed(tmp, meta=json.dumps(meta), **arrays)
    os.replace(tmp, path)


def _width(a: np.ndarray, n: int) -> np.ndarray:
    """An array of `n` columns. A log cached under an older version and since deleted keeps its
    narrower rows (version 1 re-arms had no `manual` column); the missing columns read 0."""
    a = np.asarray(a, dtype=np.float64)
    a = a.reshape(len(a), -1) if a.size else np.zeros((0, n))
    if a.shape[1] < n:
        a = np.hstack([a, np.zeros((len(a), n - a.shape[1]))])
    return a


def _load_cache(path: str) -> "tuple[dict, dict | None]":
    z = np.load(path)
    meta = json.loads(str(z["meta"]))
    if meta.get("empty"):
        return meta, None
    entry = {k: meta[k] for k in ("first_t", "span", "lives", "counts", "manual")}
    entry["arenas"] = {a: {kind: _width(z[f"{kind}:{a}"], n) for kind, n in _COLUMNS.items()}
                       for a in meta["arenas"]}
    return meta, entry


def collect(logs, arena_of: dict, cache_dir: "str | None" = CACHE_DIR, include_cached: bool = True):
    """[(log name, extraction)] for `logs`, read from the cache when it is current, and, with
    `include_cached`, every cached log no longer on disk. Notes count what came from where."""
    entries, notes, seen = [], collections.Counter(), set()
    for log in logs:
        name = os.path.basename(log)
        try:
            st = os.stat(log)
        except FileNotFoundError:
            notes["vanished"] += 1                 # pruned between the listing and now
            continue
        # The live log grows and its name is reused by the next session: never cached.
        cpath = os.path.join(cache_dir, name[:-4] + ".npz") if cache_dir and name != "wingman.log" else None
        entry, hit = None, False
        if cpath and os.path.exists(cpath):
            meta, cached = _load_cache(cpath)
            if (meta.get("version") == CACHE_VERSION and meta.get("size") == st.st_size
                    and meta.get("mtime") == st.st_mtime):
                entry, hit = cached, True
                notes["cached"] += 1
        if not hit:
            try:
                entry = extract_log(log, arena_of)
            except FileNotFoundError:
                notes["vanished"] += 1
                continue
            notes["parsed"] += 1
            if cpath:
                _save_cache(cpath, entry, st)
        seen.add(name)
        if entry is not None:
            entries.append((name, entry))
    if include_cached and cache_dir and os.path.isdir(cache_dir):
        for cpath in sorted(glob.glob(os.path.join(cache_dir, "*.npz"))):
            name = os.path.basename(cpath)[:-4] + ".log"
            if name in seen:
                continue
            _, entry = _load_cache(cpath)
            notes["kept"] += 1                     # its log is gone; its flights still count
            if entry is not None:
                entries.append((name, entry))
    entries.sort(key=lambda e: e[1]["first_t"])
    return entries, notes


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("logs", nargs="*")
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--since", help="only logs whose first line is on or after YYYY-MM-DD")
    ap.add_argument("--no-write", action="store_true", help="report only; write no record")
    ap.add_argument("--json", help="also write the report as JSON here")
    ap.add_argument("--cache-dir", default=CACHE_DIR,
                    help="per-log extractions, kept after the log is pruned ('' for none)")
    ap.add_argument("--with-live", action="store_true",
                    help="with no LOG named, also read wingman.log (never cached): the session that just "
                         "ended stays there until the next start rotates it")
    args = ap.parse_args(argv)
    logs = args.logs or sorted(glob.glob("logs/*.log"))
    if args.with_live and not args.logs and os.path.exists("wingman.log"):
        logs = logs + ["wingman.log"]
    arena_of, height_of, minimap_of = arenas_from_config(args.config)
    started = time.monotonic()
    # Named logs build from those alone; with none named, every cached log counts too.
    entries, notes = collect(logs, arena_of, args.cache_dir or None, include_cached=not args.logs)
    per_arena = collections.defaultdict(lambda: {"samples": [], "crashes": [], "rearms": []})
    counts, manual = collections.Counter(), collections.Counter()
    used, life_base, span = [], 0, [math.inf, -math.inf]
    for name, entry in entries:
        if args.since and dt.datetime.fromtimestamp(entry["first_t"]).strftime("%Y-%m-%d") < args.since:
            continue
        counts.update(entry["counts"])
        manual.update(entry["manual"])
        used.append(name)
        span = [min(span[0], entry["span"][0]), max(span[1], entry["span"][1])]
        for arena, d in entry["arenas"].items():
            dest = per_arena[arena]
            dest["samples"] += [Sample(r[0], r[1], r[2], r[3], int(r[4]) + life_base, bool(r[5]))
                                for r in d["samples"]]
            dest["crashes"] += [Crash(*r) for r in d["crashes"]]
            dest["rearms"] += [Rearm(r[0], r[1], r[2], r[3], bool(r[4])) for r in d["rearms"]]
        life_base += int(entry["lives"])
    print("logs: " + ", ".join(f"{k} {notes[k]}" for k in ("parsed", "cached", "kept", "vanished") if notes[k])
          + " (kept: from the cache, the log since deleted)")
    if not used:
        print("no log with TERRAIN fixes")
        return 1
    when = lambda t: dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")   # noqa: E731
    print(f"{len(used)} logs with fixes, {when(span[0])} to {when(span[1])}, "
          f"parsed in {time.monotonic() - started:.0f} s")
    fixes = sum(v for k, v in counts.items() if k not in ("crash-deaths", "crash-no-fix",
                                                          "crash-no-alt", "crash-samples"))
    print(f"fixes {fixes}: " + ", ".join(f"{k} {counts[k]} ({counts[k] / max(fixes, 1):.1%})"
                                       for k in ("accepted", "pre-death", "no-alt", "unclean",
                                                 "life-split", "unknown-map") if counts[k]))
    print(f"crash deaths {counts['crash-deaths']}: samples {counts['crash-samples']}, "
          f"no fix within {CRASH_FIX_MAX_AGE_S:.0f} s {counts['crash-no-fix']}, "
          f"no altitude {counts['crash-no-alt']}")
    print(f"manual flight (Phase 0): {manual['lines']} TERRAIN lines, {manual['fixes']} with a fix, "
          f"{manual['samples']} accepted samples")
    report = {"logs": used, "counts": dict(counts), "manual": dict(manual), "arenas": {}}
    for arena in sorted(per_arena):
        d = per_arena[arena]
        height_path = height_of[arena]
        height = HeightMap.load(height_path)
        meta = {"arena": arena, "height": height_path, "minimap": minimap_of.get(arena),
                "content_build": os.path.basename(os.path.dirname(os.path.dirname(height_path))),
                "logs": len(used), "from": when(span[0]), "to": when(span[1]),
                "built": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
        rec, stats = build_record(d["samples"], d["crashes"], d["rearms"], height=height, meta=meta)
        chk = mesh_check(rec, height)
        manual_n = sum(1 for s in d["samples"] if s.manual)
        out_path = os.path.join(os.path.dirname(height_path), f"{arena}-airspace.npz")
        if not args.no_write:
            rec.save(out_path)
        print(f"\n== {arena}: {stats['samples']} samples ({manual_n} manual), {chk['crashes']} crash samples, "
              f"{len(d['rearms'])} re-arm points" + ("" if args.no_write else f" -> {out_path}"))
        print(f"  dropped: spikes {stats['spikes']}, samples inside the ground {stats['samples_inside']}; "
              f"fill {stats['fill_points']} points over {stats['segments']} steps, "
              f"{stats['fill_inside']} inside the ground ({stats['fill_inside'] / max(stats['fill_points'], 1):.1%}), "
              f"{stats['segments_too_fast']} steps too fast to fill")
        print(f"  coverage: flown {chk['flown_km2']:.0f} of {chk['arena_km2']:.0f} sq km "
              f"({chk['flown_km2'] / chk['arena_km2']:.0%}), trusted (two passes) {chk['trusted_km2']:.0f} sq km "
              f"({chk['trusted_km2'] / chk['arena_km2']:.0%})")
        bands = "; ".join(
            f"{'below' if b['hi'] == 0 else ('%g+' % b['lo'] if math.isinf(b['hi']) else '%g-%g' % (b['lo'], b['hi']))}"
            f" {b['cells']} ({b['trusted']} trusted)" for b in chk["bands"])
        print(f"  floor over the cell's highest ground, m: {bands}")
        print(f"  mesh: floors under all the ground in their cell {chk['floor_under_all_ground']}; "
              f"rock above the floor somewhere in the cell {chk['rock_above_floor']} "
              f"({chk['rock_above_floor'] / max(chk['flown_cells'], 1):.0%} of flown cells, by "
              f"{_fmt_q(chk['rock_above_floor_m'])}), above a trusted floor {chk['rock_above_trusted_floor']}")
        print(f"  crash samples over the ground: {_fmt_q(chk['crash_over_ground_m'])}; "
              f"within 100 m {chk['crashes_within_100m']} of {chk['crashes']}")
        report["arenas"][arena] = {"stats": stats, "check": chk, "manual_samples": manual_n,
                                   "rearms": len(d["rearms"]), "record": out_path}
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(report, fh, indent=1, default=float)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Design 017 phase 2: score the planner's query offline, on recorded flights.

For each day of logs on hand, the record is built from every other day (the cache of pruned logs
included), so a flight never vouches for its own path. Every battle tick of the held-out day with
a fix and a clean altitude then asks the query (wingman/airspace.py, AirspaceQuery) about the
straight path ahead: --horizon seconds on the track and climb of the step from the tick before.
Reported for each setting of cell size, side margin and up margin:

- crash approaches: deaths with no kill card. The share whose last 8 s held a tick whose path
  answered not free; how many of those answered ground (the mesh) and how many only not known
  (the record); and how far ahead of the death the first one came.
- killed approaches: deaths to an enemy's kill card, as a control. A query that marks these as
  often as the crashes is reading the flying, not the terrain.
- ordinary flight: ticks with no death in the next 10 s. The share answering not free, ground
  and not known. Every not-free answer here is a path the planner could not prove.

Usage:
    uv run --active python scripts/airspace-score.py [--cells 32,64,128] [--side 0,100,200]
        [--up 0,50,100] [--horizon 3] [--json OUT.json]
"""

from __future__ import annotations

import argparse
import bisect
import collections
import datetime as dt
import glob
import importlib.util
import json
import math
import os
import sys
import time

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

from wingman.airspace import (FREE, GROUND, NOT_KNOWN, AirspaceQuery, Crash, HeightMap,  # noqa: E402
                              Rearm, Sample, build_record)

_spec = importlib.util.spec_from_file_location("airspace_record", os.path.join(_HERE, "airspace-record.py"))
ar = importlib.util.module_from_spec(_spec)
sys.modules["airspace_record"] = ar
_spec.loader.exec_module(ar)

APPROACH_S = 8.0          # a death's approach: its last 8 s (the record leaves them out)
ORDINARY_CLEAR_S = 10.0   # an ordinary tick has no death in the next 10 s
STEP_S = 0.25             # path points this far apart
MAX_STEP_S = 2.0          # a tick's track comes from the tick before it within this
MAX_SPEED = 700.0


def day_of(t: float) -> str:
    return dt.datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def resupply_runs(path: str) -> list:
    """[(start, end)] of each resupply run, cut as resupply_runs.py cuts them: from 'missiles
    exhausted' or 'urgency overtook' to the confirmed re-arm, a death card or the next MATCH."""
    runs, start = [], None
    with open(path, errors="replace") as fh:
        for line in fh:
            if not ar._is_line(line):
                continue
            if "RESUPPLY: missiles exhausted" in line or "RESUPPLY: urgency overtook" in line:
                if start is None:
                    start = ar._epoch(line)
            elif start is not None and ("RESUPPLY: confirmed ammo" in line or "DEATH CARD:" in line
                                        or "MATCH: " in line):
                runs.append((start, ar._epoch(line)))
                start = None
    return runs


def ticks_of(path: str, arena_of: dict):
    """(ticks, deaths) of one log. A tick: arena, t, x, z, alt, life, and vx, vz, vy from the
    tick before it (NaN when there is none). The last 8 s before a death are kept: they are what
    a crash approach is scored on."""
    ev = ar.parse_log(path)
    if not ev.fixes:
        return [], []
    tl = ar.Timeline(ev, 0)
    rt = [r[0] for r in ev.readings]
    rows = []
    for t, name, x, z in ev.fixes:
        arena = arena_of.get(name)
        if arena is None:
            continue
        alt, _ = ar.bracket_altitude(rt, ev.readings, t, tl)
        if alt is None:
            continue
        rows.append([arena, t, x, z, alt, tl.life(t), tl.state(t)])
    runs = resupply_runs(path)
    run_start = [a for a, _ in runs]
    out = []
    for k, (arena, t, x, z, alt, life, state) in enumerate(rows):
        j = bisect.bisect_right(run_start, t) - 1
        if j >= 0 and t <= runs[j][1]:
            state = "resupply run"
        vx = vz = vy = math.nan
        if k:
            pa, pt, px, pz, palt, plife, _ = rows[k - 1]
            dtk = t - pt
            if plife == life and pa == arena and 0 < dtk <= MAX_STEP_S:
                sx, sz = (x - px) / dtk, (z - pz) / dtk
                if math.hypot(sx, sz) <= MAX_SPEED:
                    vx, vz, vy = sx, sz, (alt - palt) / dtk
        out.append((arena, t, x, z, alt, life, vx, vz, vy, state))
    deaths = [(d[0], bool(d[2]), tl.life(d[0] - 0.01)) for d in ev.deaths if d[2] is not None]
    return out, deaths


def path_points(ticks: list, horizon: float):
    """For the ticks with a track: their index, and the path points (k, m) of x, z, alt."""
    have = [k for k, tk in enumerate(ticks) if not math.isnan(tk[6])]
    if not have:
        return np.zeros(0, dtype=np.int64), np.zeros((0, 0)), np.zeros((0, 0)), np.zeros((0, 0))
    a = np.array([ticks[k][2:9] for k in have], dtype=np.float64)    # x, z, alt, life, vx, vz, vy
    s = np.arange(1, int(round(horizon / STEP_S)) + 1) * STEP_S
    x = a[:, 0:1] + a[:, 4:5] * s
    z = a[:, 1:2] + a[:, 5:6] * s
    alt = a[:, 2:3] + a[:, 6:7] * s
    return np.array(have), x, z, alt


def build_records(entries, heights: dict, cell: float) -> dict:
    """One record per arena from cached extractions (lives offset so no two logs share one)."""
    per = collections.defaultdict(lambda: ([], [], []))
    base = 0
    for _, entry in entries:
        for arena, d in entry["arenas"].items():
            s, c, r = per[arena]
            s += [Sample(v[0], v[1], v[2], v[3], int(v[4]) + base, bool(v[5])) for v in d["samples"]]
            c += [Crash(*v) for v in d["crashes"]]
            r += [Rearm(v[0], v[1], v[2], v[3], bool(v[4]) if len(v) > 4 else False) for v in d["rearms"]]
        base += int(entry["lives"])
    return {arena: build_record(s, c, r, height=heights.get(arena), cell=cell)[0]
            for arena, (s, c, r) in per.items()}


def score_day(ticks, deaths, codes):
    """Per tick answers (worst over its path; -1 without a path) -> crash and killed rows (each
    with its arena) and ordinary counts keyed (arena, what)."""
    t = np.array([tk[1] for tk in ticks])
    life = np.array([tk[5] for tk in ticks])
    arena = [tk[0] for tk in ticks]
    tally = {"crash": [], "killed": [], "ordinary": collections.Counter()}
    death_t = sorted(d[0] for d in deaths)
    for k in range(len(ticks)):
        if codes[k] < 0:
            continue
        j = bisect.bisect_left(death_t, t[k])
        if j < len(death_t) and death_t[j] - t[k] <= ORDINARY_CLEAR_S:
            continue
        tally["ordinary"][(arena[k], "ticks")] += 1
        tally["ordinary"][(arena[k], "not_free")] += int(codes[k] != FREE)
        tally["ordinary"][(arena[k], "ground")] += int(codes[k] == GROUND)
        state = ticks[k][9] or "?"
        tally["ordinary"][(arena[k], "ticks@" + state)] += 1
        tally["ordinary"][(arena[k], "not_free@" + state)] += int(codes[k] != FREE)
    for td, crash, dlife in deaths:
        sel = np.flatnonzero((life == dlife) & (t >= td - APPROACH_S) & (t < td) & (codes >= 0))
        if not len(sel):
            continue
        flagged = sel[codes[sel] != FREE]
        row = {"arena": arena[sel[-1]], "ground": bool((codes[sel] == GROUND).any()),
               "flagged": bool(len(flagged)), "lead": float(td - t[flagged[0]]) if len(flagged) else None}
        tally["crash" if crash else "killed"].append(row)
    return tally


def summarise(rows: list) -> dict:
    n = len(rows)
    flagged = [r for r in rows if r["flagged"]]
    ground = sum(r["ground"] for r in rows)
    leads = [r["lead"] for r in flagged if r["lead"] is not None]
    return {"n": n, "flagged": len(flagged), "ground": ground, "not_known_only": len(flagged) - ground,
            "lead_median": float(np.median(leads)) if leads else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cells", default="32,64,128")
    ap.add_argument("--side", default="0,100,200")
    ap.add_argument("--up", default="0,50,100")
    ap.add_argument("--horizon", type=float, default=3.0)
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--json")
    ap.add_argument("--one-pass", action="store_true", help="trust a one-pass floor (the two-pass rule off)")
    ap.add_argument("--train-days", type=int, default=0,
                    help="build each day's record from only the N days before it (0: every other day)")
    ap.add_argument("--test-days", type=int, default=0, help="hold out only the last N days (0: all)")
    ap.add_argument("--default-cell", type=float, default=64.0)
    ap.add_argument("--default-side", type=float, default=0.0)
    ap.add_argument("--default-up", type=float, default=50.0)
    args = ap.parse_args(argv)
    cells = [float(v) for v in args.cells.split(",")]
    sides = [float(v) for v in args.side.split(",")]
    ups = [float(v) for v in args.up.split(",")]
    started = time.monotonic()
    arena_of, height_of, _ = ar.arenas_from_config(args.config)
    heights = {a: HeightMap.load(p) for a, p in height_of.items()}
    entries, _ = ar.collect(sorted(glob.glob("logs/*.log")), arena_of)
    by_day = collections.defaultdict(list)
    for log in sorted(glob.glob("logs/*.log")):
        ticks, deaths = ticks_of(log, arena_of)
        if ticks:
            by_day[day_of(ticks[0][1])].append((ticks, deaths))
    print(f"{sum(len(v) for v in by_day.values())} logs on {len(by_day)} days held out in turn; "
          f"{len(entries)} logs in the record pool; read in {time.monotonic() - started:.0f} s")
    results = collections.defaultdict(lambda: {"crash": [], "killed": [], "ordinary": collections.Counter()})
    default = (args.default_cell, args.default_side, args.default_up)
    days = sorted(by_day)
    test_days = days[-args.test_days:] if args.test_days else days
    for day in test_days:
        logs = by_day[day]
        if args.train_days:
            prior = [d for d in sorted({day_of(e[1]["first_t"]) for e in entries}) if d < day][-args.train_days:]
            train = [e for e in entries if day_of(e[1]["first_t"]) in prior]
        else:
            train = [e for e in entries if day_of(e[1]["first_t"]) != day]
        for cell in cells:
            records = build_records(train, heights, cell)
            queries = {(side, up): {a: AirspaceQuery(r, heights.get(a), side, up, two_pass=not args.one_pass)
                                    for a, r in records.items()}
                       for side in sides for up in ups}
            for ticks, deaths in logs:
                have, x, z, alt = path_points(ticks, args.horizon)
                arenas = np.array([ticks[k][0] for k in have])
                for (side, up), q in queries.items():
                    codes = np.full(len(ticks), -1, dtype=np.int64)
                    for arena in set(arenas.tolist()):
                        rows = np.flatnonzero(arenas == arena)
                        if arena not in q:                     # no record yet: nothing is proven
                            codes[have[rows]] = NOT_KNOWN
                            if heights.get(arena) is not None:
                                g = heights[arena].inside(x[rows], z[rows], alt[rows], tol=0.0).any(axis=1)
                                codes[have[rows[g]]] = GROUND
                            continue
                        codes[have[rows]] = q[arena].points(x[rows], z[rows], alt[rows]).max(axis=1)
                    tally = score_day(ticks, deaths, codes)
                    key = (cell, side, up)
                    results[key]["crash"] += tally["crash"]
                    results[key]["killed"] += tally["killed"]
                    results[key]["ordinary"].update(tally["ordinary"])
        print(f"  {day} scored ({time.monotonic() - started:.0f} s)")

    def only(r, arena=None):
        def keep(row):
            return arena is None or row["arena"] == arena
        o = collections.Counter()
        for (a, what), v in r["ordinary"].items():
            if arena is None or a == arena:
                o[what] += v
        return {"crash": [x for x in r["crash"] if keep(x)], "killed": [x for x in r["killed"] if keep(x)],
                "ordinary": o}

    def line(key, r):
        c, k, o = summarise(r["crash"]), summarise(r["killed"]), r["ordinary"]
        pct = lambda a, b: f"{a / b:.0%}" if b else "-"      # noqa: E731
        lead = "-" if c["lead_median"] is None else f"{c['lead_median']:.1f} s"
        return (f"{key:<16} crash {pct(c['flagged'], c['n']):>4} of {c['n']:<4} (ground {pct(c['ground'], c['n']):>4}, "
                f"not known only {pct(c['not_known_only'], c['n']):>4}, lead {lead:>6}) | "
                f"killed {pct(k['flagged'], k['n']):>4} of {k['n']:<4} (ground {pct(k['ground'], k['n']):>4}) | ordinary not free "
                f"{pct(o['not_free'], o['ticks']):>4} (ground {pct(o['ground'], o['ticks'])}) of {o['ticks']}")

    print(f"\nnot free on the {args.horizon:.0f} s path ahead; cell/side/up in metres")
    for key in sorted(results):
        print(line("%g/%g/%g" % key, only(results[key])))
    if default in results:
        o = only(results[default])["ordinary"]
        states = sorted(k[6:] for k in o if k.startswith("ticks@"))
        print("\nordinary flight by state at %g/%g/%g: " % default + "; ".join(
            f"{st} not free {o['not_free@' + st] / o['ticks@' + st]:.0%} of {o['ticks@' + st]}" for st in states))
        print("\nper arena at %g/%g/%g:" % default)
        arenas = sorted({a for a, _ in results[default]["ordinary"]})
        for arena in arenas:
            print(line(arena, only(results[default], arena)))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump({"%g/%g/%g" % k: {"crash": summarise(only(v)["crash"]), "killed": summarise(only(v)["killed"]),
                                        "ordinary": dict(only(v)["ordinary"])} for k, v in results.items()},
                      fh, indent=1)
    return 0


if __name__ == "__main__":
    sys.exit(main())

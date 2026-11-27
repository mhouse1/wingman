"""Design 017 phase 4: golden-path candidates from the re-arms on record, and the operator's accept.

A candidate is one life's flight through a ring site: its accepted samples from 30 s before the
re-arm point to 15 s after. The re-arm point is the last fix 3 to 6 s before ``RESUPPLY: confirmed
ammo`` (airspace-record.py), so the ring lies about there. Wingman's own clean re-arms count, as
the operator's do (Design 017, "How one is recorded"). A candidate is kept when:

- no crash (a death with no kill card) came within 30 s after the re-arm;
- every waypoint leaves a way out: from it, Design 020's climb (a 22 deg/s pull to 50 deg after
  0.5 s, terrain_service.fly_candidate), flown at the waypoint's speed and heading, reaches the
  arena's transit altitude without meeting the mesh;
- a re-arm flown by hand is found from the missile count, which a rack switch also moves: away from
  any ring site wingman confirmed (1,200 m), it is kept but marked `unconfirmed`, for the operator to
  check in the replay before accepting it;
- it is a path to follow: at least MIN_WAYPOINTS waypoints and no gap over MAX_GAP_S (4.5 s, two
  missed ticks).

Not applied yet: "no terrain warning within 30 s" (the record's cache does not hold warnings).
The transit altitude is the arena's highest mesh ground within the 10 km ring plus 50 m. The
design also puts it above the highest flown floor; that is left out, since one cell flown only at
5,000 m would set it.

Reads the record's cache (pruned logs included). Writes ``<arena>-golden-candidates.json`` beside
the record, every cut with its verdict. ``--accept ARENA:ID`` copies a candidate into
``<arena>-golden.json``, the accepted golden paths: "the operator accepts it from the list the tool
prints". export_scene.py draws both as layers, so a candidate can be looked over in the 3D scene
first.

Usage:
    uv run --active python scripts/airspace-golden.py
    uv run --active python scripts/airspace-golden.py --accept crimson-canyon:3 stoneforest:12
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import glob
import importlib.util
import json
import math
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

from wingman.airspace import HeightMap  # noqa: E402
from wingman.terrain_service import WAY_OUT_CANDIDATES, fly_candidate  # noqa: E402
from wingman.config_schema import schema_default  # noqa: E402

_spec = importlib.util.spec_from_file_location("airspace_record", os.path.join(_HERE, "airspace-record.py"))
ar = importlib.util.module_from_spec(_spec)
sys.modules["airspace_record"] = ar
_spec.loader.exec_module(ar)

BEFORE_S, AFTER_S = 30.0, 15.0     # the cut around the re-arm point
CRASH_AFTER_S = 30.0               # no crash this soon after
MIN_WAYPOINTS = 8
MAX_GAP_S = 4.5                    # two missed ticks; a follower bridges that
TRANSIT_MARGIN_M = 50.0
CLIMB_PULL_DEG_S, CLIMB_MAX_DEG, CLIMB_REACTION_S = 22.0, 50.0, 0.5   # Design 020's "climb" way out
SITE_WITHIN_M = 1200.0             # re-arms this close are one ring site (resupply_runs.py)


def transit_altitude(height: HeightMap, radius_m: float = 10000.0) -> float:
    c = height.origin + (np.arange(height.n) + 0.5) * height.cell
    xx, zz = np.meshgrid(c, c)
    inside = np.hypot(xx, zz) <= radius_m
    return float(height.top[inside].max()) + TRANSIT_MARGIN_M


def ring_sites(rearms: list) -> list:
    """[(x, z, alt, count)] clustered within SITE_WITHIN_M, two re-arms at least."""
    clusters = []
    for r in rearms:
        for c in clusters:
            if math.hypot(r[1] - c[0][1], r[2] - c[0][2]) < SITE_WITHIN_M:
                c.append(r)
                break
        else:
            clusters.append([r])
    return [(float(np.median([r[1] for r in c])), float(np.median([r[2] for r in c])),
             float(np.median([r[3] for r in c])), len(c)) for c in clusters if len(c) >= 2]


def way_out(height: HeightMap, wp: np.ndarray, transit: float) -> "list[int]":
    """Indexes of the waypoints whose climb meets the mesh before the transit altitude."""
    bad = []
    for k in range(len(wp)):
        a, b = max(k - 1, 0), min(k + 1, len(wp) - 1)
        dt_ = wp[b, 0] - wp[a, 0]
        if dt_ <= 0:
            continue
        vx, vz = (wp[b, 1] - wp[a, 1]) / dt_, (wp[b, 2] - wp[a, 2]) / dt_
        vy = (wp[b, 3] - wp[a, 3]) / dt_
        speed = max(math.hypot(vx, vz, vy), 60.0)
        if math.hypot(vx, vz) < 1.0:
            continue
        climb_needed = max(transit - wp[k, 3], 0.0)
        horizon = min(30.0, CLIMB_REACTION_S + 2.5 + climb_needed / (speed * math.sin(math.radians(CLIMB_MAX_DEG))))
        hit = fly_candidate(height, wp[k, 1], wp[k, 2], vx, vz, wp[k, 3], vy, 0.0, CLIMB_PULL_DEG_S, horizon,
                            reaction_s=CLIMB_REACTION_S, max_path_deg=CLIMB_MAX_DEG)
        if hit is not None:
            bad.append(k)
    return bad


def way_out_any(height: HeightMap, wp: np.ndarray, horizon: "float | None" = None) -> "list[int]":
    """Indexes of the waypoints from which none of Design 020's way-out manoeuvres (climb, a turn
    either way, or a turn with a climb: WAY_OUT_CANDIDATES) stays off the mesh for `horizon`
    (terrain_service.way_out_horizon_s, 5 s). Low in a canyon the way out is often a turn; the
    climb-only rule rejects such a pass however it was flown."""
    horizon = float(schema_default("terrain_service.way_out_horizon_s")) if horizon is None else horizon
    bad = []
    for k in range(len(wp)):
        a, b = max(k - 1, 0), min(k + 1, len(wp) - 1)
        dt_ = wp[b, 0] - wp[a, 0]
        if dt_ <= 0:
            continue
        vx, vz = (wp[b, 1] - wp[a, 1]) / dt_, (wp[b, 2] - wp[a, 2]) / dt_
        vy = (wp[b, 3] - wp[a, 3]) / dt_
        if math.hypot(vx, vz) < 1.0:
            continue
        clear = any(fly_candidate(height, wp[k, 1], wp[k, 2], vx, vz, wp[k, 3], vy, turn, pull, horizon,
                                  reaction_s=react, level_deg_s=level) is None
                    for _, turn, pull, level, react in WAY_OUT_CANDIDATES)
        if not clear:
            bad.append(k)
    return bad


def cut(d: dict, rearm: np.ndarray):
    """The waypoints (t, x, z, alt) of the re-arm's life around it, or None without a life."""
    s = d["samples"]
    if not len(s):
        return None
    near = np.argmin(np.abs(s[:, 0] - rearm[0]))
    if abs(s[near, 0] - rearm[0]) > 2.0:
        return None
    life = s[near, 4]
    sel = (s[:, 4] == life) & (s[:, 0] >= rearm[0] - BEFORE_S) & (s[:, 0] <= rearm[0] + AFTER_S)
    wp = s[sel][:, :4]
    return wp[np.argsort(wp[:, 0])], bool(s[sel][:, 5].any())


def candidates_for(arena: str, entries: list, height: HeightMap, rule: str = "climb") -> "tuple[list, float, list]":
    transit = transit_altitude(height)
    # Ring sites from re-arms wingman confirmed itself. A re-arm found by hand from the missile
    # count can be a rack switch, so it counts only at a site those mark (rings sit at fixed sites).
    all_rearms = [r for _, e in entries for r in e["arenas"].get(arena, {}).get("rearms", [])]
    sites = ring_sites([r for r in all_rearms if len(r) < 5 or not r[4]])
    out = []
    for name, e in entries:
        d = e["arenas"].get(arena)
        if not d:
            continue
        for r in d["rearms"]:
            got = cut(d, r)
            row = {"log": name, "t": float(r[0]), "when": dt.datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M:%S"),
                   "rearm": [float(r[1]), float(r[2]), float(r[3])]}
            if sites:
                site = min(range(len(sites)), key=lambda i: math.hypot(sites[i][0] - r[1], sites[i][1] - r[2]))
                row["site"] = site
                row["site_m"] = round(math.hypot(sites[site][0] - r[1], sites[site][1] - r[2]))
            reasons = []          # (category, detail)
            by_hand = len(r) > 4 and bool(r[4])
            # A re-arm found by hand from the missile count can be a rack switch. Away from any site
            # wingman confirmed, it stays a candidate but is marked: the operator accepts every
            # golden path, and checks the replay first. On a map with no confirmed site yet the
            # operator's pass is the only way to learn the ring (Stormbreak, 2026-11-26 21:20: the
            # operator's level pass across the stack re-armed where wingman's dive met the wall).
            row["unconfirmed"] = bool(by_hand and (not sites or row.get("site_m", math.inf) > SITE_WITHIN_M))
            if got is None:
                reasons.append(("no life", "no accepted sample within 2 s of the re-arm"))
                wp, manual = np.zeros((0, 4)), False
            else:
                wp, manual = got
                # The record's own rule: a waypoint more than 30 m inside the mesh is a fault in the
                # fix or the altitude, not flown air.
                if len(wp):
                    inside = height.inside(wp[:, 1], wp[:, 2], wp[:, 3])
                    row["dropped_inside"] = int(inside.sum())
                    wp = wp[~inside]
            row["manual"] = manual or by_hand
            crashes = d["crashes"]
            if len(crashes) and np.any((crashes[:, 0] > r[0]) & (crashes[:, 0] <= r[0] + CRASH_AFTER_S)):
                reasons.append(("crash", "a crash within 30 s"))
            if got is not None and len(wp) < MIN_WAYPOINTS:
                reasons.append(("too short", f"{len(wp)} waypoints"))
            elif len(wp) >= 2 and np.diff(wp[:, 0]).max() > MAX_GAP_S:
                reasons.append(("gap", f"a {np.diff(wp[:, 0]).max():.1f} s gap"))
            if len(wp) >= 2:
                bad = way_out(height, wp, transit) if rule == "climb" else way_out_any(height, wp)
                if bad:
                    reasons.append(("no way out", f"no way out from {len(bad)} of {len(wp)} waypoints"))
                over = wp[:, 3] - height.ground(wp[:, 1], wp[:, 2])
                row["lowest_over_ground_m"] = round(float(over.min()))
                row["lowest_alt_m"] = round(float(wp[:, 3].min()))
            row["waypoints"] = [[round(float(v), 2) for v in w] for w in wp]
            row["verdict"] = "candidate" if not reasons else "rejected"
            row["reasons"] = [detail for _, detail in reasons]
            row["reject"] = [category for category, _ in reasons]
            out.append(row)
    out.sort(key=lambda r: r["t"])
    for i, row in enumerate(out):
        row["id"] = i
    return out, transit, sites


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--accept", nargs="*", default=[], help="ARENA:ID of candidates to accept as golden paths")
    ap.add_argument("--show", type=int, default=12, help="candidates to list per arena")
    args = ap.parse_args(argv)
    arena_of, height_of, _ = ar.arenas_from_config(args.config)
    entries, _ = ar.collect(sorted(glob.glob("logs/*.log")) + (["wingman.log"] if os.path.exists("wingman.log") else []),
                            arena_of)
    accept = collections.defaultdict(set)
    for item in args.accept:
        arena, _, ident = item.partition(":")
        accept[arena].add(int(ident))
    for arena in sorted(height_of):
        height = HeightMap.load(height_of[arena])
        rows, transit, sites = candidates_for(arena, entries, height)
        folder = os.path.dirname(height_of[arena])
        with open(os.path.join(folder, f"{arena}-golden-candidates.json"), "w") as fh:
            json.dump({"arena": arena, "transit_alt_m": transit, "sites": sites, "paths": rows}, fh)
        kept = [r for r in rows if r["verdict"] == "candidate"]
        why = collections.Counter(c for r in rows for c in r["reject"])
        unconfirmed = sum(1 for r in kept if r.get("unconfirmed"))
        print(f"== {arena}: {len(rows)} re-arms, {len(kept)} candidates, transit altitude {transit:.0f} m, "
              f"{len(sites)} ring sites" + (f", {unconfirmed} of them unconfirmed hand-flown" if unconfirmed else "")
              + (f"; rejected for: {dict(why)}" if why else ""))
        for r in kept[:args.show]:
            site = f"site {r['site']} ({r['site_m']} m off)" if "site" in r else "no site"
            print(f"  {arena}:{r['id']:<4} {r['when']}  {site}, {len(r['waypoints'])} waypoints, lowest "
                  f"{r['lowest_alt_m']} m ({r['lowest_over_ground_m']} m over the ground)"
                  + (", flown by hand" if r["manual"] else "") + (", UNCONFIRMED: check the replay" if r.get("unconfirmed") else ""))
        if accept.get(arena):
            for line in accept_paths(os.path.join(folder, f"{arena}-golden.json"), arena, rows, accept[arena]):
                print("  " + line)
    return 0


def accept_paths(path: str, arena: str, rows: list, ids) -> list:
    """Copy the candidates named by `ids` into the golden-path file at `path`, once each (a
    candidate is known by its log and re-arm time, since ids renumber as logs are added).
    Returns what happened to each id."""
    golden = {"arena": arena, "paths": []}
    if os.path.exists(path):
        with open(path) as fh:
            golden = json.load(fh)
    have = {(p["log"], p["t"]) for p in golden["paths"]}
    said = []
    for r in rows:
        if r["id"] not in ids:
            continue
        if r["verdict"] != "candidate":
            said.append(f"{arena}:{r['id']} not accepted: {', '.join(r['reasons'])}")
        elif (r["log"], r["t"]) in have:
            said.append(f"{arena}:{r['id']} already a golden path")
        else:
            golden["paths"].append(dict(r, accepted=dt.datetime.now().strftime("%Y-%m-%d %H:%M")))
            have.add((r["log"], r["t"]))
            said.append(f"{arena}:{r['id']} accepted as a golden path")
    with open(path, "w") as fh:
        json.dump(golden, fh)
    return said


if __name__ == "__main__":
    sys.exit(main())

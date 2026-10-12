"""Carry the position between fixes, and say how far it can be trusted.

Between two fixes the position is carried forward in a straight line at the
speed of the last fixes. How wrong that gets is measured on the fixes
themselves: predict each one from a fix some seconds earlier, as if the ones
between had been lost, and compare.

usage: mm_coast.py HELD_OUT_RUN [HELD_OUT_RUN ...]   (needs mm/rotfix_run<run>.json from mm_rotfix.py)
"""

import json
import sys
from pathlib import Path

import numpy as np

FRAME_S = 0.506  # seconds between recorded frames
M_PER_PX = 64.0
HORIZONS = (2, 4, 8, 12, 20, 30)  # frames carried: about 1, 2, 4, 6, 10 and 15 s
LIMIT_M = 500.0  # a carried position is trusted while its error, 9 times in 10, is under this

rows = json.loads(Path("mm/scan2.json").read_text())
runs = json.loads(Path("mm/runs.json").read_text())


def load(run):
    fixes = json.loads(Path("mm/rotfix_run%d.json" % run).read_text())
    fixes = {int(k): np.array(v[:2], float) for k, v in fixes.items()}
    a, b = runs[run]
    alive = [k for k in range(a, b + 1) if rows[k]["own"]]
    return fixes, alive


def velocity(fixes, k):
    """Pixels a frame at fix k, from the fixes of the two seconds before it."""
    back = [j for j in range(k - 4, k) if j in fixes]
    if not back:
        return None
    j = back[0]
    return (fixes[k] - fixes[j]) / (k - j)


def unbroken(alive_set, a, b):
    """No death or respawn between two frames: every frame between is an alive one."""
    return all(j in alive_set for j in range(a, b + 1))


errors = {h: [] for h in HORIZONS}
loaded = {}
for run in (int(v) for v in sys.argv[1:]):
    fixes, alive = load(run)
    loaded[run] = (fixes, alive)
    alive_set = set(alive)
    for k in fixes:
        for h in HORIZONS:
            j = k - h
            if j not in fixes or not unbroken(alive_set, j, k):
                continue
            v = velocity(fixes, j)
            if v is None:
                continue
            errors[h].append(float(np.hypot(*(fixes[j] + v * h - fixes[k]))) * M_PER_PX)

print("error of a carried position, against the fix it should have reached:")
limit_frames = 0
for h in HORIZONS:
    e = np.array(errors[h])
    if len(e) < 20:
        print("  carried %4.1f s: too few cases (%d)" % (h * FRAME_S, len(e)))
        continue
    p50, p90 = np.percentile(e, [50, 90])
    print(
        "  carried %4.1f s: n=%4d | error m, median %4.0f, 9 in 10 under %5.0f"
        % (h * FRAME_S, len(e), p50, p90)
    )
    if (
        p90 <= LIMIT_M
        and limit_frames == HORIZONS[max(HORIZONS.index(h) - 1, 0)]
        or (p90 <= LIMIT_M and limit_frames == 0)
    ):
        limit_frames = h
print(
    "a carried position is trusted for %.1f s (9 in 10 within %d m); past that it is lost"
    % (limit_frames * FRAME_S, LIMIT_M)
)

print("coverage, share of alive frames:")
for run, (fixes, alive) in loaded.items():
    alive_set = set(alive)
    carried = 0
    for k in alive:
        if k in fixes:
            continue
        last = max((j for j in range(k - limit_frames, k) if j in fixes), default=None)
        if last is not None and velocity(fixes, last) is not None and unbroken(alive_set, last, k):
            carried += 1
    n = len(alive)
    fixed = sum(k in fixes for k in alive)
    print(
        "  run %d: alive %3d | fixed %3d (%.0f%%) | carried %3d (%.0f%%) | not lost %.0f%% | lost %3d frames"
        % (
            run,
            n,
            fixed,
            100 * fixed / n,
            carried,
            100 * carried / n,
            100 * (fixed + carried) / n,
            n - fixed - carried,
        )
    )

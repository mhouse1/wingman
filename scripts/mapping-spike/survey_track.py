"""Where the survey flew, from the SURVEY POS lines of wingman logs: a picture and a coverage figure.

    survey_track.py OUT.png LOG [LOG ...]

Coverage is the share of the arena disc that lies within SWATH radii of a flown
stretch: two fixes on the same pass of the same mission start, joined by a
straight line. Positions are in arena radii, as `wingman/full_map.py` reads them.
"""

import math
import re
import sys
import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

SWATH = 0.15  # radii either side of the track counted as seen: about 1.4 km on a 9.4 km arena
ANSI = re.compile(r"\x1b\[[0-9;]*m")
POS = re.compile(
    r"(\d\d):(\d\d):(\d\d),\d+ .*SURVEY POS: east=([+-][\d.]+) north=([+-][\d.]+) r=([\d.]+) "
    r"cone=(\S+) hdg=(\S+) pass=(\d+) target=(\d+)"
)
out, logs = sys.argv[1], sys.argv[2:]

tracks = []  # (key, [(seconds, east, north), ...]) for each pass of each mission start
fixes = 0
misses = 0
cone_err = []
for path in logs:
    mission = 0
    current = None
    with open(path, errors="replace") as handle:
        lines = handle.readlines()
    for raw in lines:
        line = ANSI.sub("", raw)
        if "mission_survey - passes on" in line:
            mission += 1
            current = None
        if "SURVEY POS: n/a" in line:
            misses += 1
        m = POS.search(line)
        if not m:
            continue
        fixes += 1
        seconds = int(m[1]) * 3600 + int(m[2]) * 60 + int(m[3])
        key = (path, mission, int(m[9]), int(m[10]))
        if m[7] != "n/a" and m[8] != "n/a":
            cone_err.append((float(m[7]) - float(m[8]) + 180) % 360 - 180)
        if current is None or current[0] != key:
            current = (key, [])
            tracks.append(current)
        current[1].append((seconds, float(m[4]), float(m[5])))

grid = np.linspace(-1, 1, 161)
X, Y = np.meshgrid(grid, grid)
inside = X * X + Y * Y <= 1.0
seen = np.zeros_like(inside)


def near_segment(ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    u = np.clip(((X - ax) * dx + (Y - ay) * dy) / length2, 0, 1) if length2 > 0 else 0
    return np.hypot(X - (ax + u * dx), Y - (ay + u * dy)) <= SWATH


fig, axes = plt.subplots(figsize=(7, 7))
axes.add_patch(plt.Circle((0, 0), 1.0, fill=False, lw=1.5))
flown = 0.0
for _key, points in tracks:
    xs, ys = [p[1] for p in points], [p[2] for p in points]
    axes.plot(xs, ys, ".-", lw=1.2, ms=4)
    for (t0, a, b), (t1, c, d) in zip(points, points[1:], strict=False):
        if t1 - t0 <= 40:
            seen |= near_segment(a, b, c, d)
            flown += math.hypot(c - a, d - b)
    if len(points) == 1:
        seen |= np.hypot(X - xs[0], Y - ys[0]) <= SWATH
coverage = (seen & inside).sum() / inside.sum()
axes.contourf(X, Y, (seen & inside).astype(float), levels=[0.5, 1.5], alpha=0.15)
axes.set_xlim(-1.1, 1.1)
axes.set_ylim(-1.1, 1.1)
axes.set_aspect("equal")
axes.set_xlabel("east, arena radii")
axes.set_ylabel("north, arena radii")
axes.set_title(
    "Survey fixes: %d on %d stretches; within %.2f radii of a flown stretch: %.0f%% of the arena"
    % (fixes, len(tracks), SWATH, 100 * coverage)
)
fig.savefig(out, dpi=110, bbox_inches="tight")

radii = [math.hypot(p[1], p[2]) for _key, points in tracks for p in points]
print(
    "fixes %d, looks with no fix %d, stretches %d, flown length %.1f radii"
    % (fixes, misses, len(tracks), flown)
)
if radii:
    print(
        "distance from the centre: min %.2f, median %.2f, max %.2f radii"
        % (min(radii), float(np.median(radii)), max(radii))
    )
    print(
        "coverage within %.2f radii of a flown stretch: %.0f%% of the arena" % (SWATH, 100 * coverage)
    )
if cone_err:
    apart = np.abs(cone_err)
    print(
        "map cone against compass heading: median %.0f deg apart, 9 in 10 within %.0f (%d pairs)"
        % (np.median(apart), np.percentile(apart, 90), len(apart))
    )

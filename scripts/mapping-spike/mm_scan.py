"""Whole recording: is the minimap there, is N read, does the terrain's movement agree with the compass."""

import sys
from pathlib import Path
import math
import json
import cv2
import numpy as np
import mm_lib as L

video, out = sys.argv[1], sys.argv[2]
cap = cv2.VideoCapture(video)
rows = []
prev = None
k = 0
while True:
    ok, f = cap.read()
    if not ok:
        break
    c = L.crop(f)
    pres = L.present(c)
    own = bool(pres and L.own_icon(c))
    phi = L.north_angle(c) if own else None
    row = dict(k=k, present=bool(pres), own=own, phi=phi)
    cur = None
    if phi is not None:
        g, m = L.north_up(c, phi)
        cur = (g, m, phi)
        if prev is not None:
            dx, dy, sc = L.shift(prev[0], prev[1], g, m, max_shift=24)
            row.update(dx=-dx, dy=-dy, score=sc)  # aircraft movement, north-up, y down
    prev = cur
    rows.append(row)
    k += 1
Path(out).write_text(json.dumps(rows))
P = [r for r in rows if r["present"]]
O = [r for r in P if r["own"]]
N = [r for r in O if r["phi"] is not None]
print("own icon at centre: %d (%.0f%% of present)" % (len(O), 100 * len(O) / max(len(P), 1)))
M = [r for r in N if "score" in r]
print(
    "frames %d | minimap present %d (%.0f%%) | N read %d (%.1f%% of own-icon frames) | pairs %d"
    % (len(rows), len(P), 100 * len(P) / len(rows), len(N), 100 * len(N) / max(len(O), 1), len(M))
)
sc = np.array([r["score"] for r in M])
print(
    "score pct 10 50 90:",
    np.round(np.percentile(sc, [10, 50, 90]), 2),
    "| share over 0.6: %.2f" % (sc > 0.6).mean(),
)
good = [r for r in M if r["score"] > 0.6 and math.hypot(r["dx"], r["dy"]) > 1.0]
d = np.array(
    [((math.degrees(math.atan2(r["dx"], -r["dy"])) + r["phi"]) + 180) % 360 - 180 for r in good]
)
print(
    "moving pairs %d | track minus heading: median %+.1f, abs pct 50 90: %s"
    % (len(good), np.median(d), np.round(np.percentile(np.abs(d), [50, 90]), 1))
)
sp = np.array([math.hypot(r["dx"], r["dy"]) / 0.506 for r in M if r["score"] > 0.6])
print("px per s pct 5 50 95:", np.round(np.percentile(sp, [5, 50, 95]), 2))
# runs of present frames = battles
runs = []
s = None
for r in rows:
    if r["present"] and s is None:
        s = r["k"]
    if not r["present"] and s is not None:
        if r["k"] - s > 60:
            runs.append((s, r["k"] - 1))
        s = None
if s is not None:
    runs.append((s, rows[-1]["k"]))
print("runs:", runs)

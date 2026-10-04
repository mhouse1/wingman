"""Match each frame against the one GAP frames later and compare with compass heading and HUD speed."""

import sys
from pathlib import Path
import os
import json
import math
import cv2
import numpy as np
import mm_lib as L

video, gap = sys.argv[1], int(sys.argv[2])
rows = json.loads(Path("mm/scan2.json").read_text())
speed = {}
if os.path.exists("mm/speed.json"):
    for k, v in json.loads(Path("mm/speed.json").read_text()).items():
        if (
            len(v) == 2
            and v[0][1] >= 0.6
            and 3 <= len(v[0][0]) <= 4
            and 200 <= int(v[0][0]) <= 3000
        ):
            speed[int(k)] = int(v[0][0]) / 3.6
cap = cv2.VideoCapture(video)
cache = {}


def nu(k):
    if k not in cache:
        cap.set(cv2.CAP_PROP_POS_FRAMES, k)
        c = L.crop(cap.read()[1])
        cache[k] = L.north_up(c, rows[k]["phi"])
        if len(cache) > 40:
            cache.pop(next(iter(cache)))
    return cache[k]


out = []
for k in range(0, len(rows) - gap, gap):
    if any(rows[j]["phi"] is None for j in range(k, k + gap + 1)):
        continue
    phis = [rows[j]["phi"] for j in range(k, k + gap + 1)]
    turn = max(abs((p - phis[0] + 180) % 360 - 180) for p in phis)
    fa, ma = nu(k)
    fb, mb = nu(k + gap)
    dx, dy, sc = L.shift(fa, ma, fb, mb, max_shift=60, min_overlap=0.25)
    mx, my = -dx, -dy
    hdg = math.radians((-np.mean(phis)) % 360)
    sp = [speed[j] for j in range(k, k + gap + 1) if j in speed]
    out.append(
        dict(
            k=k,
            turn=turn,
            px=math.hypot(mx, my),
            score=sc,
            masked=float(ma.mean()),
            diff=(math.degrees(math.atan2(mx, -my)) - math.degrees(hdg) + 180) % 360 - 180,
            metres=(np.mean(sp) * gap * 0.506) if len(sp) >= gap else None,
        )
    )
Path("mm/eval_gap%d.json" % gap).write_text(json.dumps(out))


def rep(name, sel):
    if not sel:
        print(name, "none")
        return
    d = np.abs([r["diff"] for r in sel])
    print(
        "%-34s n=%4d | heading error pct 50 90: %5.1f %5.1f | within 10 deg: %.0f%%"
        % (name, len(sel), *np.percentile(d, [50, 90]), 100 * (d < 10).mean())
    )


print("pairs", len(out), "| gap %.1f s" % (gap * 0.506))
st = [r for r in out if r["turn"] < 10]
rep("straight (turn under 10 deg)", st)
rep("straight, score over 0.6", [r for r in st if r["score"] > 0.6])
rep("straight, score over 0.6, moved 3 px", [r for r in st if r["score"] > 0.6 and r["px"] > 3])
rep("turning", [r for r in out if r["turn"] >= 10])
print(
    "score pct 10 50 90:",
    np.round(np.percentile([r["score"] for r in out], [10, 50, 90]), 2),
    "| terrain share of crop pct 10 50:",
    np.round(np.percentile([r["masked"] for r in out], [10, 50]), 2),
)
sc = [r for r in st if r["score"] > 0.6 and r["px"] > 3 and r["metres"] and abs(r["diff"]) < 10]
if sc:
    mpp = np.array([r["metres"] / r["px"] for r in sc])
    print(
        "with HUD speed: n=%d | metres per pixel pct 10 50 90: %s | spread (iqr over median): %.2f"
        % (
            len(sc),
            np.round(np.percentile(mpp, [10, 50, 90]), 1),
            (np.percentile(mpp, 75) - np.percentile(mpp, 25)) / np.median(mpp),
        )
    )
    for a, b in ((0, 1400), (1400, 2700), (2700, 3700), (3700, 5300)):
        m = np.array([r["metres"] / r["px"] for r in sc if a <= r["k"] < b])
        if len(m):
            print("   frames %4d to %4d: n=%3d median %.1f m per px" % (a, b, len(m), np.median(m)))

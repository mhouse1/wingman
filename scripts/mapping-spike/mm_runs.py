"""Split the recording into battles and show one minimap from each, to group them by arena."""

import sys
from pathlib import Path
import json
import cv2
import numpy as np
import mm_lib as L

rows = json.loads(Path("mm/scan2.json").read_text())
cap = cv2.VideoCapture(sys.argv[1])
runs = []
s = None
last = None
for r in rows:
    if r["present"]:
        if s is None:
            s = r["k"]
        last = r["k"]
    elif s is not None and r["k"] - last > 40:  # 20 s without a minimap ends a battle
        runs.append((s, last))
        s = None
if s is not None:
    runs.append((s, last))
runs = [(a, b) for a, b in runs if b - a > 120]
tiles = []
for i, (a, b) in enumerate(runs):
    ks = [r["k"] for r in rows[a : b + 1] if r["phi"] is not None]
    k = ks[len(ks) // 2]
    cap.set(cv2.CAP_PROP_POS_FRAMES, k)
    c = L.crop(cap.read()[1])
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    m = L._rr < 55
    print(
        "run %2d frames %4d-%4d (%3d s) heading reads %3d | disc hue %3d sat %3d val %3d"
        % (i, a, b, (b - a) // 2, len(ks), *np.median(hsv[m], axis=0))
    )
    t = cv2.resize(c, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    cv2.putText(t, "run %d" % i, (4, 20), 0, 0.7, (255, 255, 255), 2)
    tiles.append(t)
Path("mm/runs.json").write_text(json.dumps(runs))
while len(tiles) % 5:
    tiles.append(np.zeros_like(tiles[0]))
cv2.imwrite(
    "mm/runs.jpg", np.vstack([np.hstack(tiles[i : i + 5]) for i in range(0, len(tiles), 5)])
)

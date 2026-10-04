"""Find steady, forward-looking stretches in a session recording and write them out.

Gate, from the frames themselves: consecutive frames that track well, with a
small rotation and a moderate slide between them. Menus and kill-cams fail the
tracking; camera swings and hard turns fail the rotation.
"""

import sys
import json
import math
import logging
from pathlib import Path
import cv2
import numpy as np

logging.disable(logging.CRITICAL)

video, out = sys.argv[1], Path(sys.argv[2])
cap = cv2.VideoCapture(video)
total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
LK = dict(
    winSize=(31, 31),
    maxLevel=4,
    criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
)

# Static mask for the whole session: HUD strokes are edges at the same pixel in
# most frames; the own aircraft and HUD panels barely change at all.
acc = None
mean = None
sq = None
used = 0
for k in np.linspace(0, total - 1, 300).astype(int):
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(k))
    ok, f = cap.read()
    if not ok:
        continue
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
    e = (cv2.Canny(g.astype(np.uint8), 60, 160) > 0).astype(np.float32)
    acc = e if acc is None else acc + e
    mean = g if mean is None else mean + g
    sq = g * g if sq is None else sq + g * g
    used += 1
static_edges = cv2.dilate(((acc / used) > 0.35).astype(np.uint8), np.ones((9, 9), np.uint8)) > 0
std = np.sqrt(np.maximum(sq / used - (mean / used) ** 2, 0))
h, w = std.shape
mask = np.full((h, w), 255, np.uint8)
mask[static_edges] = 0
mask[: int(h * 0.09)] = 0  # scoreboard strip
mask[int(h * 0.90) :] = 0  # bottom strip: health, weapons, fps text
cv2.circle(mask, (int(w * 0.915), int(h * 0.13)), int(h * 0.14), 0, -1)  # minimap
print(
    "frames",
    total,
    "| masked share %.2f" % (1 - mask.mean() / 255),
    "| low-variance share %.2f" % (std < 12).mean(),
)

cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
prev = None
ok_flags = []
angs = []
for _ in range(total):
    ok, f = cap.read()
    if not ok:
        break
    g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
    good = False
    if prev is not None and not np.array_equal(prev, g):
        pts = cv2.goodFeaturesToTrack(prev, 400, 0.01, 7, mask=mask)
        if pts is not None and len(pts) >= 80:
            nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, g, pts, None, **LK)
            back, st2, _ = cv2.calcOpticalFlowPyrLK(g, prev, nxt, None, **LK)
            keep = (
                (st.ravel() == 1)
                & (st2.ravel() == 1)
                & (np.linalg.norm((pts - back).reshape(-1, 2), axis=1) < 1.5)
            )
            a, b = pts[keep].reshape(-1, 2), nxt[keep].reshape(-1, 2)
            if len(a) >= 80:
                m, inl = cv2.estimateAffinePartial2D(
                    a, b, method=cv2.RANSAC, ransacReprojThreshold=3.0
                )
                if m is not None and inl is not None and inl.sum() >= 80:
                    ang = abs(math.degrees(math.atan2(m[1, 0], m[0, 0])))
                    slide = float(np.hypot(m[0, 2], m[1, 2]))
                    moved = float(np.median(np.linalg.norm(b - a, axis=1)))
                    # A hangar or a menu is steady because nothing moves. Flight
                    # moves the whole picture by a few pixels every frame.
                    good = ang < 8.0 and slide < 200.0 and moved >= 2.5
                    angs.append(ang)
    ok_flags.append(good)
    prev = g
runs = []
start = None
for i, v in enumerate(ok_flags + [False]):
    if v and start is None:
        start = i
    if not v and start is not None:
        if i - start >= 16:
            runs.append((start, i))  # 8 s or more
        start = None
runs.sort(key=lambda r: r[0] - r[1])
print(
    "steady pairs %d of %d (%.0f%%); stretches of 8 s or more: %d, totalling %.1f min; longest %.0f s"
    % (
        sum(ok_flags),
        len(ok_flags),
        100 * sum(ok_flags) / len(ok_flags),
        len(runs),
        sum(b - a for a, b in runs) * 0.506 / 60,
        (runs[0][1] - runs[0][0]) * 0.506 if runs else 0,
    )
)
out.mkdir(parents=True, exist_ok=True)
cv2.imwrite(str(out / "mask.png"), mask)
meta = []
for n, (a, b) in enumerate(runs[:6]):
    d = out / f"stretch_{n}" / "images"
    d.mkdir(parents=True, exist_ok=True)
    md = out / f"stretch_{n}" / "masks"
    md.mkdir(parents=True, exist_ok=True)
    cap.set(cv2.CAP_PROP_POS_FRAMES, a - 1)
    for i in range(a - 1, b):
        ok, f = cap.read()
        if not ok:
            break
        name = f"f{i:06d}.jpg"
        cv2.imwrite(str(d / name), f, [cv2.IMWRITE_JPEG_QUALITY, 96])
        cv2.imwrite(str(md / (name + ".png")), mask)
    meta.append(
        dict(stretch=n, first=a - 1, last=b - 1, frames=b - a + 1, t0_s=round((a - 1) * 0.506, 1))
    )
    print(
        "  stretch %d: frames %d..%d (%d), t=%.0f s" % (n, a - 1, b - 1, b - a + 1, (a - 1) * 0.506)
    )
(out / "stretches.json").write_text(json.dumps(meta, indent=1))

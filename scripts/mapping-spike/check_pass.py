"""Check a reconstructed stretch against HUD numbers the model was not given.

A reconstruction that places every frame can still be wrong (Design 017, the
survey footage check of 2026-10-04). Two readings off the HUD test it:

- Altitude. One direction and one scale should turn the cameras' positions into
  the HUD altitudes: alt = c + position . w. The length of w is metres per model
  unit. Only meaningful when the path is not a straight line: on a steady
  descent any w along the path fits.
- Speed. The metres flown between frames, from HUD speed, against the model's
  step. Their ratio is metres per model unit again.

On a sound model the two scales agree and the steps follow the speed.

    check_pass.py <stretch dir> <hud.json> [seconds per recorded frame]

hud.json: {"alt": {"<frame>": metres, ...}, "kph": {"<frame>": kph, ...}}
"""

import sys
import json
from pathlib import Path
import numpy as np
import pycolmap

stretch = Path(sys.argv[1])
hud = json.loads(Path(sys.argv[2]).read_text())
dt = float(sys.argv[3]) if len(sys.argv) > 3 else 0.506
alt = {int(k): v for k, v in hud["alt"].items()}
kph = {int(k): v for k, v in hud["kph"].items()}

recs = [pycolmap.Reconstruction(str(d)) for d in sorted((stretch / "work" / "sparse").iterdir())]
rec = max(recs, key=lambda r: r.num_reg_images())
ims = sorted([im for im in rec.images.values() if im.has_pose], key=lambda im: im.name)
idx = np.array([int(im.name[1:7]) for im in ims])
C = np.array([im.projection_center() for im in ims])
steps = np.linalg.norm(np.diff(C, axis=0), axis=1)
gaps = np.diff(idx)
print(
    "frames placed %d, %d..%d | steps, model units: median %.3f, 5th pct %.3f, 95th pct %.3f, max %.3f"
    % (len(ims), idx[0], idx[-1], np.median(steps), *np.percentile(steps, [5, 95]), steps.max())
)

# Altitude: alt = c + C . w, on the frames with a HUD altitude.
ref = [k for k in sorted(alt) if k in set(idx)]
A = np.array([np.append(C[np.where(idx == k)[0][0]], 1.0) for k in ref])
b = np.array([alt[k] for k in ref], float)
sol, *_ = np.linalg.lstsq(A, b, rcond=None)
w, c = sol[:3], sol[3]
res = A @ sol - b
print(
    "altitude fit on %d frames: %.0f m per model unit, residual rms %.0f m, worst %.0f m (HUD altitude spans %d m)"
    % (len(ref), np.linalg.norm(w), np.sqrt((res**2).mean()), np.abs(res).max(), b.max() - b.min())
)
up = w / np.linalg.norm(w)
cam_up = np.array([-im.cam_from_world().rotation.matrix()[1] for im in ims]).mean(0)
cam_up /= np.linalg.norm(cam_up)
print(
    "up from the altitude fit against up from the cameras: %.1f degrees apart"
    % np.degrees(np.arccos(np.clip(up @ cam_up, -1, 1)))
)
left_out = []
for j in range(len(ref)):
    others = np.arange(len(ref)) != j
    s, *_ = np.linalg.lstsq(A[others], b[others], rcond=None)
    left_out.append(A[j] @ s - b[j])
print(
    "each HUD altitude predicted from the others: median error %.0f m, worst %.0f m"
    % (np.median(np.abs(left_out)), np.abs(left_out).max())
)

# Speed: metres flown per step against the model's step.
frames = sorted(kph)
speed = np.interp(idx, frames, [kph[k] / 3.6 for k in frames])
metres = (speed[:-1] + speed[1:]) / 2 * gaps * dt
moving = steps > 0.25 * np.median(steps)
ratio = metres[moving] / steps[moving]
print(
    "speed: %.0f m per model unit (median over %d steps; middle half %.0f to %.0f)"
    % (np.median(ratio), moving.sum(), *np.percentile(ratio, [25, 75]))
)
print("step length against speed, correlation: %.2f" % np.corrcoef(steps[moving], speed[:-1][moving])[0, 1])

# Terrain heights with the altitude fit. Sound terrain lies below the aircraft.
P = np.array([p.xyz for p in rec.points3D.values()])
elev = P @ w + c
print(
    "points: %d, elevation 5th pct %.0f, median %.0f, 95th pct %.0f m; above the highest HUD altitude: %.1f%%"
    % (len(P), *np.percentile(elev, [5, 50, 95]), 100 * (elev > max(alt.values())).mean())
)

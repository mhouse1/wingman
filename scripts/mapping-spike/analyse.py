import sys
from pathlib import Path
import numpy as np
import pycolmap
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

stretch = Path(sys.argv[1])
kph0, kph1, alt_m = float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
dt = 0.506
recs = [pycolmap.Reconstruction(str(d)) for d in sorted((stretch / "work" / "sparse").iterdir())]
rec = max(recs, key=lambda r: r.num_reg_images())
ims = sorted([im for im in rec.images.values() if im.has_pose], key=lambda im: im.name)
C = np.array([im.projection_center() for im in ims])
steps = np.linalg.norm(np.diff(C, axis=0), axis=1)
# Scale: metres per model unit, from the mean HUD speed over the stretch.
mean_speed = (kph0 + kph1) / 2 / 3.6
scale = mean_speed * dt / steps.mean()
print("frames %d | camera steps, model units: %s" % (len(ims), " ".join("%.2f" % s for s in steps)))
half = len(steps) // 2
print(
    "step growth, last half over first half: %.2f | HUD speed, end over start: %.2f"
    % (steps[half:].mean() / steps[:half].mean(), kph1 / kph0)
)
print("scale: 1 model unit = %.0f m (mean HUD speed %.0f m/s)" % (scale, mean_speed))
# Up: the mean of the cameras' own up directions (the flight is close to level).
ups = []
fwd = []
for im in ims:
    R = im.cam_from_world().rotation.matrix()
    ups.append(-R[1])
    fwd.append(R[2])
up = np.mean(ups, axis=0)
up /= np.linalg.norm(up)
f = np.mean(fwd, axis=0)
f -= f.dot(up) * up
f /= np.linalg.norm(f)
right = np.cross(f, up)
P = np.array([p.xyz for p in rec.points3D.values()])
col = np.array([p.color for p in rec.points3D.values()]) / 255.0
rel = (P - C[0]) * scale
x, y, z = rel @ right, rel @ f, rel @ up
cz = ((C - C[0]) * scale) @ up
cx = ((C - C[0]) * scale) @ right
cy = ((C - C[0]) * scale) @ f
elev = alt_m + z  # metres above the HUD's zero, if up is right
keep = (y > 0) & (y < 12000) & (np.abs(x) < 8000)
print("points kept %d of %d (within 12 km ahead)" % (keep.sum(), len(P)))
print("camera climb over the stretch, model: %+.0f m (HUD said about -38 m)" % (cz[-1] - cz[0]))
q = np.percentile(elev[keep], [2, 25, 50, 75, 98])
print(
    "terrain elevation, m: 2nd pct %.0f, quartiles %.0f / %.0f / %.0f, 98th pct %.0f | aircraft at %.0f m"
    % (*q, alt_m)
)
print(
    "share of terrain points above the aircraft's altitude: %.1f%%"
    % (100 * (elev[keep] > alt_m).mean())
)
near = keep & (y < 3000)
if near.sum() > 50:
    print(
        "within 3 km ahead: %d points, elevation median %.0f m, highest (98th pct) %.0f m"
        % (near.sum(), np.median(elev[near]), np.percentile(elev[near], 98))
    )
fig, ax = plt.subplots(1, 3, figsize=(18, 6))
s0 = ax[0].scatter(x[keep], y[keep], c=elev[keep], s=3, cmap="terrain", vmin=q[0], vmax=q[4])
ax[0].plot(cx, cy, "r.-", lw=2)
ax[0].set_title("From above: terrain points coloured by elevation (m); red = flight path")
ax[0].set_xlabel("right (m)")
ax[0].set_ylabel("ahead (m)")
ax[0].set_aspect("equal")
plt.colorbar(s0, ax=ax[0])
ax[1].scatter(y[keep], elev[keep], c=col[keep], s=3)
ax[1].plot(cy, alt_m + cz, "r.-", lw=2)
ax[1].set_title("From the side: elevation against distance ahead (true colours)")
ax[1].set_xlabel("ahead (m)")
ax[1].set_ylabel("elevation (m)")
ax[2].scatter(x[keep], elev[keep], c=col[keep], s=3)
ax[2].axhline(alt_m, color="r")
ax[2].set_title("From behind: elevation against left-right")
ax[2].set_xlabel("right (m)")
ax[2].set_ylabel("elevation (m)")
plt.tight_layout()
plt.savefig(stretch / "model.png", dpi=70)

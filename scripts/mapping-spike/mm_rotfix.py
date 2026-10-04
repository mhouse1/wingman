"""Place a battle's frames on a stitched arena without reading the N letter.

The frame's rotation is found by trying every rotation against the map and
keeping the one that fits. Where the N letter was read, the two are compared.

usage: mm_rotfix.py VIDEO HELD_OUT_RUN      (needs mm/mosaic_wo<run>.npz from mm_stitch.py)
"""

import json
import sys
from pathlib import Path

import cv2
import numpy as np

import mm_lib as L

L.UP = 1
W = 2 * L.HALF
COARSE = 6  # degrees between rotations tried
ACCEPT, MARGIN = 0.7, 0.15  # a fix: this score, and this far above the next-best

video, held = sys.argv[1], int(sys.argv[2])
rows = json.loads(Path("mm/scan2.json").read_text())
runs = json.loads(Path("mm/runs.json").read_text())
saved = np.load("mm/mosaic_wo%d.npz" % held)
cnt_full = saved["cnt"]
ys, xs = np.nonzero(cnt_full)
pad = W
y0, x0 = max(ys.min() - pad, 0), max(xs.min() - pad, 0)
N = 1 << int(np.ceil(np.log2(max(ys.max() - y0, xs.max() - x0) + 2 * pad)))
cnt = np.zeros((N, N), np.float32)
tot = np.zeros((N, N), np.float32)
part = cnt_full[y0 : y0 + N, x0 : x0 + N]
cnt[: part.shape[0], : part.shape[1]] = part
tot[: part.shape[0], : part.shape[1]] = saved["sum"][y0 : y0 + N, x0 : x0 + N]
known = (cnt > 0).astype(np.float32)
mean = tot / np.maximum(cnt, 1) * known
F_known, F_mean, F_sq = (np.fft.rfft2(a) for a in (known, mean, mean * mean))
print("map %d px across in a %d px working square" % (max(np.ptp(ys), np.ptp(xs)), N))


def corr(a, b):
    return np.fft.irfft2(a * b, (N, N))


def locate(g, m):
    """Best place for a window anywhere on the map: centre, score, runner-up score."""
    gm = np.zeros((N, N), np.float32)
    mm = np.zeros((N, N), np.float32)
    mm[:W, :W] = m
    gm[:W, :W] = g * m
    Gm, Gg, Ggg = (np.conj(np.fft.rfft2(a)) for a in (mm, gm, gm * gm))
    n = corr(Gm, F_known)
    ok = n > 0.6 * m.sum()
    n = np.where(ok, n, 1.0)
    sg, sf = corr(Gg, F_known), corr(Gm, F_mean)
    num = corr(Gg, F_mean) - sg * sf / n
    den = np.sqrt(
        np.maximum(corr(Ggg, F_known) - sg * sg / n, 1e-6)
        * np.maximum(corr(Gm, F_sq) - sf * sf / n, 1e-6)
    )
    ncc = np.where(ok, num / den, -1.0)
    y, x = np.unravel_index(np.argmax(ncc), ncc.shape)
    best = float(ncc[y, x])
    cv2.circle(ncc, (int(x), int(y)), 14, -1.0, -1)
    return (int(x) + L.HALF, int(y) + L.HALF), best, float(ncc.max())


def near(p, g, m):
    """The same, within 24 px of a known place: centre, score."""
    x, y = int(round(p[0])) - L.HALF, int(round(p[1])) - L.HALF
    if x < 0 or y < 0 or x + W > N or y + W > N:
        return None, -1.0
    f, mf = mean[y : y + W, x : x + W], known[y : y + W, x : x + W] > 0
    if mf.sum() < 0.3 * m.sum():
        return None, -1.0
    dx, dy, sc = L.shift(f, mf, g, m, max_shift=24, min_overlap=0.3)
    return (round(p[0]) - dx, round(p[1]) - dy), sc


def angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def fix(c, prior):
    """(position, rotation, score, how) for one minimap crop, or None."""
    trials = []
    for phi in range(-180, 180, COARSE):
        g, m = L.north_up(c, float(phi))
        if m.sum() < 800:
            return None
        if prior is not None:
            pos, sc = near(prior, g, m)
            trials.append((sc, phi, pos, -1.0))
        else:
            pos, sc, second = locate(g, m)
            trials.append((sc, phi, pos, second))
    sc, phi, pos, second = max(trials, key=lambda t: t[0])
    other = max((t[0] for t in trials if angle_diff(t[1], phi) > 20), default=-1.0)
    if pos is None or sc < ACCEPT or sc - max(other, second) < MARGIN:
        return None
    # Refine the rotation to a degree, at the place just found.
    best = (sc, float(phi), pos)
    for fine in range(phi - COARSE + 1, phi + COARSE):
        g, m = L.north_up(c, float(fine))
        p2, s2 = near(pos, g, m)
        if p2 is not None and s2 > best[0]:
            best = (s2, float(fine), p2)
    return best[2], best[1], best[0], "near" if prior is not None else "anywhere"


cap = cv2.VideoCapture(video)
a, b = runs[held]
cap.set(cv2.CAP_PROP_POS_FRAMES, a)
alive = 0
out = {}
prior, prior_k = None, None
for k in range(a, b + 1):
    ok, frame = cap.read()
    if not ok:
        break
    if not rows[k]["own"]:
        continue
    alive += 1
    c = L.crop(frame)
    use_prior = prior if prior is not None and k - prior_k <= 6 else None
    got = fix(c, use_prior)
    if got is None and use_prior is not None:
        got = fix(c, None)  # lost the thread: look everywhere
    if got is None:
        continue
    pos, phi, sc, how = got
    out[k] = (float(pos[0]) + x0, float(pos[1]) + y0, phi, sc, how)
    prior, prior_k = pos, k
Path("mm/rotfix_run%d.json" % held).write_text(json.dumps(out))

print(
    "held-out run %d: alive frames %d | placed without the N letter: %d (%.0f%%) | found near the last fix %d, found anywhere %d"
    % (
        held,
        alive,
        len(out),
        100 * len(out) / alive,
        sum(v[4] == "near" for v in out.values()),
        sum(v[4] == "anywhere" for v in out.values()),
    )
)
both = [(k, v) for k, v in out.items() if rows[k]["phi"] is not None]
if both:
    d = np.array([angle_diff(v[2], rows[k]["phi"]) for k, v in both])
    print(
        "  rotation against the N letter, where both exist: n=%d | apart by pct 50 90 99: %s deg | within 5 deg: %.0f%%"
        % (len(d), np.round(np.percentile(d, [50, 90, 99]), 1), 100 * (d <= 5).mean())
    )
old = Path("mm/fixes_run%d.json" % held)
if old.exists():
    letter = {int(k): v for k, v in json.loads(old.read_text()).items()}
    pairs = [
        np.hypot(out[k][0] - v[0][0], out[k][1] - v[0][1])
        for k, v in letter.items()
        if k in out and v[1] > ACCEPT and v[1] - v[2] > MARGIN
    ]
    if pairs:
        e = np.array(pairs)
        print(
            "  position against the N-letter fix, where both exist: n=%d | apart by pct 50 90 99: %s px | within 2 px: %.0f%%"
            % (len(e), np.round(np.percentile(e, [50, 90, 99]), 1), 100 * (e <= 2).mean())
        )
ks = sorted(out)
gaps = [k2 - k1 for k1, k2 in zip(ks, ks[1:], strict=False)]
steps = [
    np.hypot(out[k2][0] - out[k1][0], out[k2][1] - out[k1][1])
    for k1, k2 in zip(ks, ks[1:], strict=False)
    if k2 - k1 == 1
]
if steps:
    s = np.array(steps)
    print(
        "  step between consecutive fixes: pct 50 90 99: %s px | over 12 px (a jump): %d of %d"
        % (np.round(np.percentile(s, [50, 90, 99]), 1), int((s > 12).sum()), len(s))
    )
print("  longest run of alive frames with no fix: %d frames" % (max(gaps) - 1 if gaps else 0))

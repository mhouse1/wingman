"""Stitch an arena from some battles' minimaps, then place another battle's frames on it.

usage: mm_stitch.py VIDEO HELD_OUT_RUN BUILD_RUN [BUILD_RUN ...]
"""

import sys
from pathlib import Path
import json
import cv2
import numpy as np
import mm_lib as L

L.UP = 1
N = 2048
W = 2 * L.HALF
video, held, build = sys.argv[1], int(sys.argv[2]), [int(v) for v in sys.argv[3:]]
rows = json.loads(Path("mm/scan2.json").read_text())
runs = json.loads(Path("mm/runs.json").read_text())
cap = cv2.VideoCapture(video)


def frames(run):
    a, b = runs[run]
    cap.set(cv2.CAP_PROP_POS_FRAMES, a)
    for k in range(a, b + 1):
        ok, f = cap.read()
        if not ok:
            return
        if rows[k]["phi"] is not None:
            yield (k, *L.north_up(L.crop(f), rows[k]["phi"]))


class Mosaic:
    def __init__(self):
        self.sum = np.zeros((N, N), np.float32)
        self.cnt = np.zeros((N, N), np.float32)
        self._fft = None

    def mean(self):
        return self.sum / np.maximum(self.cnt, 1), self.cnt > 0

    def view(self, p):
        x, y = int(round(p[0])) - L.HALF, int(round(p[1])) - L.HALF
        f, mf = self.mean()
        return f[y : y + W, x : x + W], mf[y : y + W, x : x + W]

    def paste(self, p, g, m):
        x, y = int(round(p[0])) - L.HALF, int(round(p[1])) - L.HALF
        self.sum[y : y + W, x : x + W] += g * m
        self.cnt[y : y + W, x : x + W] += m
        self._fft = None

    def locate(self, g, m, min_overlap=0.6):
        """Best place for a window anywhere on the mosaic: centre, score, runner-up score."""
        if self._fft is None:
            f, mf = self.mean()
            mf = mf.astype(np.float32)
            f = f * mf
            self._fft = tuple(np.fft.rfft2(a) for a in (mf, f, f * f))
        Fm, Ff, Fff = self._fft
        gm = np.zeros((N, N), np.float32)
        mm = np.zeros((N, N), np.float32)
        mm[:W, :W] = m
        gm[:W, :W] = g * m
        Gm, Gg, Ggg = (np.conj(np.fft.rfft2(a)) for a in (mm, gm, gm * gm))

        def c(A, B):
            return np.fft.irfft2(A * B, (N, N))

        n = c(Gm, Fm)
        ok = n > min_overlap * m.sum()
        n = np.where(ok, n, 1.0)
        sg, sf = c(Gg, Fm), c(Gm, Ff)
        num = c(Gg, Ff) - sg * sf / n
        den = np.sqrt(
            np.maximum(c(Ggg, Fm) - sg * sg / n, 1e-6) * np.maximum(c(Gm, Fff) - sf * sf / n, 1e-6)
        )
        ncc = np.where(ok, num / den, -1.0)
        y, x = np.unravel_index(np.argmax(ncc), ncc.shape)
        best = float(ncc[y, x])
        rest = ncc.copy()
        cv2.circle(rest, (int(x), int(y)), 14, -1.0, -1)
        return (int(x) + L.HALF, int(y) + L.HALF), best, float(rest.max())


def track(mos, run, start=None, paste=True):
    """Follow one battle across the mosaic. Returns {frame: (x, y, how)}."""
    p = start
    prev = None
    vel = np.zeros(2)
    out = {}
    last_k = None
    for k, g, m in frames(run):
        if m.sum() < 800:
            continue
        if p is None:
            p = np.array([N / 2, N / 2], float)
        if last_k is not None and k - last_k > 6:
            # A break in the battle (a death, a respawn somewhere else): the old
            # position means nothing. Wait for a frame the map can place outright.
            pos, best, second = mos.locate(g, m) if m.sum() >= 3000 else (None, -1.0, -1.0)
            if not (best > 0.7 and best - second > 0.15):
                continue
            p = np.array(pos, float)
            vel = np.zeros(2)
            prev = None
            last_k = None
            out[k] = (float(p[0]), float(p[1]), "refix")
        if last_k is not None:
            gap = k - last_k
            guess = p + vel * gap
            how = None
            f, mf = mos.view(guess)
            if mf.sum() > 0.3 * m.sum():
                dx, dy, sc = L.shift(f, mf, g, m, max_shift=20, min_overlap=0.3)
                if sc > 0.6:
                    new = np.round(guess) - (dx, dy)
                    how = "map"
            if how is None and prev is not None and gap <= 6:
                dx, dy, sc = L.shift(prev[0], prev[1], g, m, max_shift=24, min_overlap=0.3)
                if sc > 0.6:
                    new = p - (dx, dy)
                    how = "step"
            if how is None:
                # Unreadable against both: carry on at the last speed, paste nothing,
                # and let the next frame step from this one.
                p = guess
                prev = (g, m)
                last_k = k
                out[k] = (float(p[0]), float(p[1]), "coast")
                continue
            vel = 0.5 * vel + 0.5 * (new - p) / gap
            p = new
            out[k] = (float(p[0]), float(p[1]), how)
        if paste:
            mos.paste(p, g, m)
        prev = (g, m)
        last_k = k
    return out


def tally(tr):
    c = {h: sum(v[2] == h for v in tr.values()) for h in ("map", "step", "coast", "refix")}
    return "%d frames: %d against the map, %d by step, %d coasted, %d re-fixed after a break" % (
        len(tr),
        c["map"],
        c["step"],
        c["coast"],
        c["refix"],
    )


mos = Mosaic()
first = True
for run in build:
    if first:
        tr = track(mos, run)
        first = False
        print("run %d stitched |" % run, tally(tr))
    else:
        start = None
        joined_at = None
        for k, g, m in frames(run):
            if m.sum() < 3000:
                continue
            pos, best, second = mos.locate(g, m)
            if best > 0.7 and best - second > 0.15:
                start = np.array(pos, float)
                joined_at = k
                break
        if start is None:
            print("run %d: no confident starting fix, left out" % run)
            continue
        tr = track(mos, run, start=start)
        print(
            "run %d joined at frame %d (score %.2f, runner-up %.2f) |"
            % (run, joined_at, best, second),
            tally(tr),
        )
f, mf = mos.mean()
ys, xs = np.nonzero(mf)
print(
    "mosaic covers %d px (%.0f sq km at 64 m per px), extent %d by %d px"
    % (mf.sum(), mf.sum() * 0.064**2, xs.max() - xs.min(), ys.max() - ys.min())
)
cv2.imwrite(
    "mm/mosaic_wo%d.png" % held, f[ys.min() : ys.max(), xs.min() : xs.max()].astype(np.uint8)
)

np.savez_compressed("mm/mosaic_wo%d.npz" % held, sum=mos.sum, cnt=mos.cnt)

# Held-out battle: a fix for every frame, with no knowledge of where the last one was.
a, b = runs[held]
alive = sum(rows[k]["own"] for k in range(a, b + 1))
fixes = {}
prev = None
odo = {}
for k, g, m in frames(held):
    pos, best, second = mos.locate(g, m) if m.sum() >= 800 else ((0, 0), -1.0, -1.0)
    fixes[k] = (pos, best, second, float(m.sum()))
    if prev is not None and k - prev[0] <= 2 and m.sum() >= 800:
        dx, dy, sc = L.shift(prev[1], prev[2], g, m, max_shift=20, min_overlap=0.3)
        if sc > 0.6:
            odo[k] = (prev[0], -dx, -dy)
    prev = (k, g, m)
Path("mm/fixes_run%d.json" % held).write_text(json.dumps({str(k): v for k, v in fixes.items()}))
good = {k: v for k, v in fixes.items() if v[1] > 0.7 and v[1] - v[2] > 0.15}
print(
    "held-out run %d: alive frames %d | heading read %d | confident fix %d (%.0f%% of alive, %.0f%% of heading-read)"
    % (held, alive, len(fixes), len(good), 100 * len(good) / alive, 100 * len(good) / len(fixes))
)
sc = np.array([v[1] for v in fixes.values()])
print(
    "  best score pct 10 50 90:",
    np.round(np.percentile(sc, [10, 50, 90]), 2),
    "| margin over runner-up pct 10 50 90:",
    np.round(np.percentile([v[1] - v[2] for v in fixes.values()], [10, 50, 90]), 2),
)
err = []
for k, (pk, dx, dy) in odo.items():
    if k in good and pk in good:
        fx = np.subtract(good[k][0], good[pk][0])
        err.append((float(np.hypot(*(fx - (dx, dy)))), float(np.hypot(dx, dy))))
if err:
    e = np.array(err)
    print(
        "  consecutive fixes against the step between them: n=%d | disagreement px pct 50 90 99: %s | within 2 px: %.0f%% | over 10 px: %.0f%%"
        % (
            len(e),
            np.round(np.percentile(e[:, 0], [50, 90, 99]), 1),
            100 * (e[:, 0] <= 2).mean(),
            100 * (e[:, 0] > 10).mean(),
        )
    )

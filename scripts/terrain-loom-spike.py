#!/usr/bin/env python3
"""Offline spike for HLDD 001 Phase 2: terrain looming from frame-to-frame motion.

Reads a recorded session (logs/session_*.mp4, 960x600 at about 2 fps) and its
behaviour-tree trace (logs/bt_trace_*.jsonl), and for every pair of consecutive
frames:

- tracks corner points in the forward view (HUD and own aircraft masked out),
- fits one similarity transform to them (RANSAC): a zoom and a slide,
- turns the zoom into a time to contact, tau = dt / (scale - 1),
- finds the point the picture expands from (the transform's fixed point) and
  calls the pair "on course" when that point is inside the flight-path box.

A warning is `confirm` consecutive on-course pairs with tau under `tau_s`. The
report counts warnings against the deaths in the trace (a tactic change to
RespawnWait). It decides nothing and touches no live code.

    uv run --active python scripts/terrain-loom-spike.py logs/session_X.mp4
    uv run --active python scripts/terrain-loom-spike.py logs/session_X.mp4 --dump out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

# Fractions of the frame. The forward view is what the nose flies into; the own
# aircraft fills the bottom centre of the chase camera, so the view stops above it.
VIEW = (0.20, 0.12, 0.80, 0.60)
# The flight-path box, around the screen centre: an expansion point inside it
# means the aircraft is flying at what it is looking at.
PATH_BOX = (0.38, 0.30, 0.62, 0.62)


def hud_mask(video: str, samples: int = 120) -> np.ndarray:
    """255 where the picture is free to move, 0 on HUD strokes and text.

    HUD elements are edges that sit at the same pixel in most frames; terrain,
    sky and aircraft are not. Sampled across the whole session."""
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    acc = None
    used = 0
    for k in np.linspace(0, max(0, total - 1), samples).astype(int):
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(k))
        ok, frame = cap.read()
        if not ok:
            continue
        edges = cv2.Canny(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), 60, 160) > 0
        acc = edges.astype(np.float32) if acc is None else acc + edges
        used += 1
    cap.release()
    if acc is None:
        raise SystemExit(f"no frames readable in {video}")
    static = (acc / max(1, used)) > 0.35
    static = cv2.dilate(static.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
    return np.where(static, 0, 255).astype(np.uint8)


def view_mask(shape, hud: np.ndarray) -> np.ndarray:
    h, w = shape[:2]
    mask = np.zeros((h, w), np.uint8)
    x1, y1, x2, y2 = VIEW
    mask[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)] = 255
    return cv2.bitwise_and(mask, hud)


def pair_motion(prev_gray, gray, mask, min_points: int):
    """(scale, fixed_point_xy or None, inliers, tracked) for one frame pair, or None."""
    pts = cv2.goodFeaturesToTrack(prev_gray, maxCorners=400, qualityLevel=0.01,
                                  minDistance=7, mask=mask)
    if pts is None or len(pts) < min_points:
        return None
    lk = dict(winSize=(31, 31), maxLevel=4,
              criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
    nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev_gray, gray, pts, None, **lk)
    back, st2, _ = cv2.calcOpticalFlowPyrLK(gray, prev_gray, nxt, None, **lk)
    good = (st.ravel() == 1) & (st2.ravel() == 1) & (
        np.linalg.norm((pts - back).reshape(-1, 2), axis=1) < 1.5)
    a, b = pts[good].reshape(-1, 2), nxt[good].reshape(-1, 2)
    if len(a) < min_points:
        return None
    m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC,
                                         ransacReprojThreshold=3.0)
    if m is None or inl is None:
        return None
    inliers = int(inl.sum())
    if inliers < min_points:
        return None
    scale = float(np.hypot(m[0, 0], m[1, 0]))
    # Fixed point of x' = A x + t: the point the picture expands from.
    lhs = m[:, :2] - np.eye(2)
    fixed = None
    if abs(np.linalg.det(lhs)) > 1e-6:
        fixed = np.linalg.solve(lhs, -m[:, 2])
    return scale, fixed, inliers, len(a)


def in_box(point, shape) -> bool:
    if point is None:
        return False
    h, w = shape[:2]
    x1, y1, x2, y2 = PATH_BOX
    return x1 * w <= point[0] <= x2 * w and y1 * h <= point[1] <= y2 * h


def trace_deaths(trace: Path) -> tuple[list[float], list[float], float]:
    rows = [json.loads(line) for line in trace.read_text().splitlines() if line.strip()]
    deaths = [r["t"] for r in rows if r["to"] == "RespawnWait"]
    after_climb = [r["t"] for r in rows if r["to"] == "RespawnWait" and r["from"] == "Climb"]
    return deaths, after_climb, float(rows[-1]["t"]) if rows else 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("video")
    ap.add_argument("--trace", help="bt_trace jsonl (default: matched by name)")
    ap.add_argument("--tau-s", type=float, default=6.0, help="warn below this time to contact")
    ap.add_argument("--confirm", type=int, default=2, help="consecutive pairs to warn")
    ap.add_argument("--min-points", type=int, default=25)
    ap.add_argument("--lead-s", type=float, default=8.0,
                    help="a death counts as warned if a warning fell this close before it")
    ap.add_argument("--dump", help="directory for annotated frames at each warning")
    ap.add_argument("--no-box", action="store_true",
                    help="warn on tau alone, ignoring where the expansion point is")
    ap.add_argument("--deaths", action="store_true",
                    help="print what the tracker saw in the lead window before each death")
    args = ap.parse_args()

    video = Path(args.video)
    trace = Path(args.trace) if args.trace else video.with_name(
        video.name.replace("session_", "bt_trace_").replace(".mp4", ".jsonl"))
    deaths, after_climb, last_t = trace_deaths(trace) if trace.exists() else ([], [], 0.0)

    cap = cv2.VideoCapture(str(video))
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    # The recorder sleeps 1/fps between grabs, so a frame is a little over 0.5 s;
    # the trace's own clock calibrates it.
    dt = (last_t / total) if last_t and total else 1.0 / max(cap.get(cv2.CAP_PROP_FPS), 1.0)
    hud = hud_mask(str(video))
    dump = Path(args.dump) if args.dump else None
    if dump:
        dump.mkdir(parents=True, exist_ok=True)

    prev_gray = None
    mask = None
    streak = 0
    warnings: list[float] = []     # times, one per episode
    pairs_log: list[tuple[float, float | None, bool, int]] = []   # t, tau, on course, inliers
    readable = unreadable = expanding = 0
    last_warn = -1e9
    k = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if mask is None:
            mask = view_mask(frame.shape, hud)
        if prev_gray is not None:
            res = pair_motion(prev_gray, gray, mask, args.min_points)
            t = k * dt
            if res is None:
                unreadable += 1
                streak = 0
                pairs_log.append((t, None, False, 0))
            else:
                readable += 1
                scale, fixed, inliers, tracked = res
                tau = dt / (scale - 1.0) if scale > 1.0 + 1e-3 else None
                on_course = tau is not None and in_box(fixed, frame.shape)
                if tau is not None:
                    expanding += 1
                pairs_log.append((t, tau, on_course, inliers))
                hit = tau is not None and tau < args.tau_s and (on_course or args.no_box)
                streak = streak + 1 if hit else 0
                if streak >= args.confirm and t - last_warn > 10.0:
                    warnings.append(t)
                    last_warn = t
                    if dump is not None:
                        out = frame.copy()
                        h, w = out.shape[:2]
                        x1, y1, x2, y2 = PATH_BOX
                        cv2.rectangle(out, (int(x1 * w), int(y1 * h)),
                                      (int(x2 * w), int(y2 * h)), (0, 0, 255), 2)
                        if fixed is not None:
                            cv2.circle(out, (int(fixed[0]), int(fixed[1])), 8, (0, 255, 255), 2)
                        cv2.putText(out, f"t={t:.0f}s tau={tau:.1f}s scale={scale:.3f} "
                                         f"pts={inliers}/{tracked}", (20, h - 20),
                                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                        cv2.imwrite(str(dump / f"warn_{int(t):05d}.png"), out)
        prev_gray = gray
        k += 1
    cap.release()

    def warned(d: float) -> bool:
        return any(0.0 <= d - wt <= args.lead_s for wt in warnings)

    followed = sum(1 for wt in warnings if any(0.0 <= d - wt <= 15.0 for d in deaths))
    pairs = readable + unreadable
    print(f"{video.name}: {total} frames, {dt:.3f} s per frame, {total * dt / 60:.1f} min")
    print(f"  frame pairs: {pairs}, readable {readable} "
          f"({100.0 * readable / max(1, pairs):.0f}%), expanding {expanding}")
    print(f"  warnings (tau < {args.tau_s:g} s, {args.confirm} pairs, "
          f"{'any direction' if args.no_box else 'on course'}): "
          f"{len(warnings)}, followed by a death within 15 s: {followed}")
    print(f"  deaths: {len(deaths)}, warned within {args.lead_s:g} s before: "
          f"{sum(1 for d in deaths if warned(d))}")
    print(f"  deaths straight from Climb: {len(after_climb)}, warned: "
          f"{sum(1 for d in after_climb if warned(d))}")
    if args.deaths:
        for d in deaths:
            win = [p for p in pairs_log if 0.0 <= d - p[0] <= args.lead_s]
            cells = " ".join(
                "--" if n == 0 else ("%s%.0f" % ("*" if oc else "", tau) if tau is not None
                                     and tau < 99 else "..")
                for _, tau, oc, n in win)
            print(f"  death t={d:7.1f}s {'(from Climb) ' if d in after_climb else ''}"
                  f"{'WARNED ' if warned(d) else ''}tau per pair: {cells}")
        print("  legend: number = tau in s, * = on course, .. = not expanding, -- = unreadable")
    return 0


if __name__ == "__main__":
    sys.exit(main())

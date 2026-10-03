"""Terrain looming from frame-to-frame motion (HLDD 001 Phase 2, shadow only).

Two frames a short time apart. Corner points on whatever fills the forward view
are tracked from one to the other and one zoom-and-slide is fitted to them:

- the zoom gives a time to contact, ``tau = dt / (zoom - 1)``. It needs neither
  distance nor speed and is in seconds, like ADR 086's time to ground;
- the slide gives the point the picture expands from. Inside the flight-path
  box, the aircraft is flying at what it is looking at; outside, the structure
  passes to that side.

No trackable texture (sky, water, a dark night) gives no reading, never
"terrain ahead". Nothing here actuates: the reading is logged and drawn on the
live HUD so its false-alarm rate can be counted before anything acts on it.

The method and its parameters are those of ``scripts/terrain-loom-spike.py``,
which was measured against a recorded session. Standard library, numpy and
OpenCV only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config_schema import schema_default

logger = logging.getLogger(__name__)

# The spike's tracking parameters were tuned on 960 px wide frames, so the view
# is brought to that scale before tracking.
_REFERENCE_WIDTH = 960
# A HUD stroke is an edge at the same pixel in most frames; terrain is not.
_STATIC_EDGE_FRAC = 0.35
# Pairs to see before the HUD mask is trusted. Until then there is no reading:
# untracked HUD strokes do not move, and would vote for "not closing".
_WARMUP_PAIRS = 10
# The mask's memory, in pairs. Bounded so a long static screen fades out again.
_MASK_MEMORY_PAIRS = 400
# A zoom this close to 1 is not an expansion (the spike's cut-off).
_MIN_EXPANSION = 1e-3

_LK = dict(winSize=(31, 31), maxLevel=4,
           criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))


def _d(key: str):
    return schema_default(f"terrain_avoidance.loom.{key}")


@dataclass(frozen=True)
class LoomReading:
    """One frame pair. Pixel positions are in the full frame."""

    status: str                 # "ok", or why there is no measurement
    dt: float = 0.0
    scale: "float | None" = None
    tau: "float | None" = None  # seconds to contact; None when not expanding
    fixed: "tuple[float, float] | None" = None   # the expansion point
    on_course: bool = False     # expansion point inside the flight-path box
    inliers: int = 0
    tracked: int = 0
    points: np.ndarray = field(default_factory=lambda: np.empty((0, 2), np.float32))
    # What the colour mask left out of the view, for the HUD overlay: a 0/255
    # mask at the tracking scale, the view's origin in the frame, and that scale.
    hud_mask: "np.ndarray | None" = None
    hud_origin: "tuple[int, int]" = (0, 0)
    hud_scale: float = 1.0
    hud_frac: float = 0.0       # share of the view the colour mask removed

    @property
    def readable(self) -> bool:
        return self.status == "ok"


class TerrainLoom:
    """Measures frame pairs and keeps the confirm streak. One per session."""

    def __init__(self, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", _d("enabled")))
        self.pair_interval_s = float(cfg.get("pair_interval_s", _d("pair_interval_s")))
        self.same_frame_wait_s = float(
            cfg.get("same_frame_wait_s", _d("same_frame_wait_s")))
        self.pairs_per_tick = int(cfg.get("pairs_per_tick", _d("pairs_per_tick")))
        self._min_points = int(cfg.get("min_points", _d("min_points")))
        self.tau_warn_s = float(cfg.get("tau_warn_s", _d("tau_warn_s")))
        self._confirm_pairs = int(cfg.get("confirm_pairs", _d("confirm_pairs")))
        self.view_pct = tuple(float(v) for v in cfg.get("view_pct", _d("view_pct")))
        self.path_box_pct = tuple(
            float(v) for v in cfg.get("path_box_pct", _d("path_box_pct")))
        self.path_open_below = bool(cfg.get("path_open_below", _d("path_open_below")))
        hud = cfg.get("hud_mask") or {}

        def _h(key):
            return np.array(hud.get(key, _d(f"hud_mask.{key}")), dtype=np.uint8)

        self._hud_enabled = bool(hud.get("enabled", _d("hud_mask.enabled")))
        self._hud_margin_px = int(hud.get("margin_px", _d("hud_mask.margin_px")))
        self._hud_ranges = [(_h("red_lower"), _h("red_upper")),
                            (_h("red_wrap_lower"), _h("red_wrap_upper")),
                            (_h("green_lower"), _h("green_upper"))]
        self._edge_sum: "np.ndarray | None" = None
        self._edge_n = 0
        self._streak = 0
        self.warn = False

    # ------------------------------------------------------------------
    def _view(self, frame: np.ndarray) -> "tuple[np.ndarray, int, int, float]":
        """Grey forward view at the reference scale, its origin, and the scale."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.view_pct
        ox, oy = int(w * x1), int(h * y1)
        crop = frame[oy:int(h * y2), ox:int(w * x2)]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.ndim == 3 else crop
        s = _REFERENCE_WIDTH / float(w)
        if abs(s - 1.0) > 1e-3:
            gray = cv2.resize(gray, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        return gray, ox, oy, s

    def same_view(self, frame_a: np.ndarray, frame_b: np.ndarray) -> bool:
        """Whether two frames show the identical forward view.

        The game sometimes holds one picture for longer than the pair
        interval. Such a pair has no motion to measure.
        """
        if frame_a.shape != frame_b.shape:
            return False
        h, w = frame_a.shape[:2]
        x1, y1, x2, y2 = self.view_pct
        rows, cols = slice(int(h * y1), int(h * y2)), slice(int(w * x1), int(w * x2))
        return np.array_equal(frame_a[rows, cols], frame_b[rows, cols])

    def hud_colour_mask(self, frame: np.ndarray, shape) -> np.ndarray:
        """255 where the forward view shows a HUD colour, at the tracking scale.

        Nameplates, target markers and the lock circle move across the view,
        so the learned static-edge mask cannot catch them, and they move
        independently of the ground. Tested at full resolution, because
        shrinking first blends a thin stroke into what is behind it, then
        widened by a margin: a corner forms where a stroke meets terrain.
        `shape` is the tracking view's (height, width).
        """
        if not self._hud_enabled or frame.ndim != 3:
            return np.zeros(shape, np.uint8)
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = self.view_pct
        hsv = cv2.cvtColor(frame[int(h * y1):int(h * y2), int(w * x1):int(w * x2)],
                           cv2.COLOR_BGR2HSV)
        mask = np.zeros(hsv.shape[:2], np.uint8)
        for lower, upper in self._hud_ranges:
            mask |= cv2.inRange(hsv, lower, upper)
        if mask.shape != tuple(shape):
            # INTER_AREA keeps any coverage as a non-zero value.
            mask = cv2.resize(mask, (shape[1], shape[0]), interpolation=cv2.INTER_AREA)
        mask = np.where(mask > 0, 255, 0).astype(np.uint8)
        if self._hud_margin_px > 0:
            k = 2 * self._hud_margin_px + 1
            mask = cv2.dilate(mask, np.ones((k, k), np.uint8))
        return mask

    def _learn_mask(self, gray: np.ndarray) -> "np.ndarray | None":
        """Fold this view into the static-edge mask; the mask, once warmed up."""
        edges = (cv2.Canny(gray, 60, 160) > 0).astype(np.float32)
        if self._edge_sum is None or self._edge_sum.shape != edges.shape:
            self._edge_sum = np.zeros_like(edges)
            self._edge_n = 0
        if self._edge_n >= _MASK_MEMORY_PAIRS:
            keep = (_MASK_MEMORY_PAIRS - 1) / _MASK_MEMORY_PAIRS
            self._edge_sum *= keep
            self._edge_n = _MASK_MEMORY_PAIRS - 1
        self._edge_sum += edges
        self._edge_n += 1
        if self._edge_n < _WARMUP_PAIRS:
            return None
        static = (self._edge_sum / self._edge_n) > _STATIC_EDGE_FRAC
        static = cv2.dilate(static.astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
        return np.where(static, 0, 255).astype(np.uint8)

    def in_path_box(self, point, frame_shape) -> bool:
        """Whether the expansion point says the aircraft is flying at the view.

        The box is around the screen centre. With `path_open_below` it has no
        bottom edge: in a descent the flight path is below where the camera
        points, so the picture expands from below the box, often from below
        the frame itself. A closed box classed those readings as passing
        while the aircraft flew at the ground (HLDD 001, 2026-10-03 05:36).
        """
        if point is None:
            return False
        h, w = frame_shape[:2]
        x1, y1, x2, y2 = self.path_box_pct
        if not (x1 * w <= point[0] <= x2 * w and y1 * h <= point[1]):
            return False
        return self.path_open_below or point[1] <= y2 * h

    def measure(self, frame_a: np.ndarray, frame_b: np.ndarray, dt: float) -> LoomReading:
        """Motion between two frames `dt` seconds apart. Does not touch the streak."""
        if dt <= 0.0:
            return LoomReading(status="no-interval")
        prev, ox, oy, s = self._view(frame_a)
        cur, _, _, _ = self._view(frame_b)
        mask = self._learn_mask(cur)
        if mask is None:
            return LoomReading(status="warming", dt=dt)
        if prev.shape != cur.shape:
            return LoomReading(status="size-changed", dt=dt)
        hud_a = self.hud_colour_mask(frame_a, prev.shape)
        hud_b = self.hud_colour_mask(frame_b, cur.shape)
        mask = cv2.bitwise_and(mask, cv2.bitwise_not(hud_a))
        seen = dict(hud_mask=hud_b, hud_origin=(ox, oy), hud_scale=s,
                    hud_frac=float(np.count_nonzero(hud_b)) / hud_b.size)
        if np.array_equal(prev, cur):
            # The second grab returned the same picture: no motion to measure,
            # and a zoom of exactly 1 would read as "not closing".
            return LoomReading(status="same-frame", dt=dt, **seen)
        pts = cv2.goodFeaturesToTrack(prev, maxCorners=400, qualityLevel=0.01,
                                      minDistance=7, mask=mask)
        if pts is None or len(pts) < self._min_points:
            return LoomReading(status="few-points", dt=dt,
                               tracked=0 if pts is None else len(pts), **seen)
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, **_LK)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(cur, prev, nxt, None, **_LK)
        good = (st.ravel() == 1) & (st2.ravel() == 1) & (
            np.linalg.norm((pts - back).reshape(-1, 2), axis=1) < 1.5)
        # A point that ends under a HUD colour was overrun by a moving stroke.
        end = np.rint(nxt.reshape(-1, 2)).astype(int)
        inside = ((end[:, 0] >= 0) & (end[:, 0] < cur.shape[1])
                  & (end[:, 1] >= 0) & (end[:, 1] < cur.shape[0]))
        on_hud = np.zeros(len(end), bool)
        on_hud[inside] = hud_b[end[inside, 1], end[inside, 0]] > 0
        good &= ~on_hud
        a, b = pts[good].reshape(-1, 2), nxt[good].reshape(-1, 2)
        if len(a) < self._min_points:
            return LoomReading(status="few-points", dt=dt, tracked=len(a), **seen)
        m, inl = cv2.estimateAffinePartial2D(a, b, method=cv2.RANSAC,
                                             ransacReprojThreshold=3.0)
        if m is None or inl is None:
            return LoomReading(status="no-fit", dt=dt, tracked=len(a), **seen)
        inl = inl.ravel().astype(bool)
        inliers = int(inl.sum())
        if inliers < self._min_points:
            return LoomReading(status="few-agree", dt=dt, tracked=len(a), inliers=inliers,
                               **seen)
        scale = float(np.hypot(m[0, 0], m[1, 0]))
        # Fixed point of x' = A x + t: the point the picture expands from.
        lhs = m[:, :2] - np.eye(2)
        fixed = None
        if abs(np.linalg.det(lhs)) > 1e-6:
            fx, fy = np.linalg.solve(lhs, -m[:, 2])
            fixed = (float(fx) / s + ox, float(fy) / s + oy)
        tau = dt / (scale - 1.0) if scale > 1.0 + _MIN_EXPANSION else None
        points = b[inl] / s + np.array([ox, oy], np.float32)
        return LoomReading(
            status="ok", dt=dt, scale=scale, tau=tau, fixed=fixed,
            on_course=tau is not None and self.in_path_box(fixed, frame_b.shape),
            inliers=inliers, tracked=len(a), points=points.astype(np.float32), **seen)

    def update(self, reading: "LoomReading | None") -> bool:
        """Advance the confirm streak with this tick's reading; the shadow verdict.

        Closing means a time to contact under `tau_warn_s` with the expansion
        point in the flight-path box. A tick with no reading resets the streak,
        like Phase 1's sky test: a gap is not evidence of danger.
        """
        closing = (reading is not None and reading.readable
                   and reading.tau is not None and reading.tau < self.tau_warn_s
                   and reading.on_course)
        self._streak = self._streak + 1 if closing else 0
        warn = self._streak >= max(1, self._confirm_pairs)
        if warn and not self.warn:
            logger.warning(
                "LOOM[shadow]: terrain closing — time to contact %.1fs on course for "
                "%d pairs (HLDD 001 phase 2) [SHADOW - not actuating]",
                reading.tau, self._streak)
        self.warn = warn
        return warn


def fmt_taus(readings) -> str:
    """This tick's readings for the BT log line, in order, comma-separated."""
    return ",".join(fmt_tau(r) for r in readings) if readings else "n/a"


def fmt_tau(reading: "LoomReading | None") -> str:
    """Time to contact for the BT log line: seconds, `inf` when the view is not
    expanding, or `n/a` with the reason when the pair gave no measurement."""
    if reading is None:
        return "n/a"
    if not reading.readable:
        return f"n/a({reading.status})"
    if reading.tau is None:
        return "inf"
    return f"{reading.tau:.1f}s{'' if reading.on_course else '/off'}"

"""Terrain as shapes: outlines of what is on the screen (HLDD 001 Phase 2, shadow only).

The operator's design, 2026-10-03, in two steps. First: group the looming dots
and draw an outline round each group. Then: find the outlines of the objects
themselves, because a loose line round whichever dots survived is not the
object's edge, and steering round something needs its edge.

- **Outline**: where the picture has texture (rock, ground, buildings) against
  where it is smooth (sky, haze, water). The border of a textured region is the
  object's silhouette. No colour, and no frame-to-frame tracking, is needed to
  find it.
- **Growth**: from the dots inside the outline, moving as one body, as in
  `terrain_loom`. The outline's own area was tried as the measure and is not
  used: its border is uncertain by a few pixels, which over 0.4 s is as large
  as the growth. On a test object 6.0 s from contact the dots read 6.2 to
  6.3 s and the outline's area read 31 s and 1.3 s. It is still logged.
- **Threat**: an outline that covers the middle of the screen and grows.
- **Way out**: the nearest smooth gap, left or right of the middle, or above it.

Cloud has texture and as many trackable corners as rock (measured: 4.0 against
4.1 per 1,000 px), so nothing here tells cloud from terrain in one frame. The
growth test does it: distant cloud does not get bigger. Nothing here actuates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import cv2
import numpy as np

from .config_schema import schema_default

logger = logging.getLogger(__name__)

_LK = dict(winSize=(31, 31), maxLevel=4,
           criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01))
# A growth this close to none is not an approach (same cut-off as the pairs).
_MIN_GROWTH = 1e-3
# Share of a strip that may be textured for the strip still to count as open.
_OPEN_STRIP_FRAC = 0.2


def _d(key: str):
    return schema_default(f"terrain_avoidance.loom.shapes.{key}")


@dataclass(frozen=True)
class Shape:
    """One object: its outline. Pixels are in the full frame."""

    outline: np.ndarray             # N x 2, the silhouette in the last frame
    dots: int                       # tracked dots inside it
    area_first: float               # of the matching outline in the first frame
    area_last: float                # (both in full-frame px)
    tau: "float | None"             # seconds to contact; None when not growing or unknown
    centred: bool                   # covers the middle of the screen
    clipped: "tuple[bool, bool, bool, bool]" = (False, False, False, False)  # left, right, top, bottom
    tau_outline: "float | None" = None   # from the outline's own area; diagnostic only
    tau_dots: "float | None" = None      # from the dots inside it; this is `tau`
    measured: bool = True           # False when there were too few dots to take it


@dataclass(frozen=True)
class ShapeReading:
    """One tick. `threat` is the centred, growing shape with the least time left."""

    status: str                     # "ok", or why there are no shapes
    span_s: float = 0.0
    frames: int = 0
    shapes: "tuple[Shape, ...]" = ()
    threat: "Shape | None" = None
    avoid: "str | None" = None      # "left", "right" or "up" for the threat
    tracked: int = 0
    points: np.ndarray = field(default_factory=lambda: np.empty((0, 2), np.float32))
    blind: bool = False             # something covers the middle and its growth is unknown
    lost: bool = False              # a threat was there and the view has since gone unreadable

    @property
    def readable(self) -> bool:
        return self.status == "ok"

    @property
    def middle(self) -> "Shape | None":
        """The largest shape covering the middle, threat or not (for the log)."""
        centred = [sh for sh in self.shapes if sh.centred]
        return max(centred, key=lambda sh: sh.area_last) if centred else None


class TerrainShapes:
    """Finds the tick's outlines and keeps the cross-tick confirm."""

    def __init__(self, loom, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}
        self._loom = loom
        self.enabled = bool(cfg.get("enabled", _d("enabled")))
        self._texture_energy = float(cfg.get("texture_energy", _d("texture_energy")))
        self._texture_blur_px = int(cfg.get("texture_blur_px", _d("texture_blur_px"))) | 1
        self._min_area_frac = float(cfg.get("min_area_frac", _d("min_area_frac")))
        self._min_dots = int(cfg.get("min_dots", _d("min_dots")))
        self._centre_cover = float(cfg.get("centre_cover", _d("centre_cover")))
        self.tau_warn_s = float(cfg.get("tau_warn_s", _d("tau_warn_s")))
        self._confirm_ticks = int(cfg.get("confirm_ticks", _d("confirm_ticks")))
        self._up_bias = float(cfg.get("up_bias", _d("up_bias")))
        self._edge_margin_px = int(cfg.get("edge_margin_px", _d("edge_margin_px")))
        self._gap_min_px = int(cfg.get("gap_min_px", _d("gap_min_px")))
        self._lost_dots = int(cfg.get("lost_dots", _d("lost_dots")))
        self._after_threat = False
        self._streak = 0
        self._last_threat: "Shape | None" = None
        self.warn = False

    # ------------------------------------------------------------------
    def texture_mask(self, gray: np.ndarray, excluded: np.ndarray) -> np.ndarray:
        """255 where the view is textured. `excluded` is 255 on HUD.

        Edge strength, averaged over a neighbourhood. HUD strokes are strong
        edges, so their strength is removed before the averaging and they
        leave no region of their own.
        """
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        strength = cv2.magnitude(gx, gy)
        strength[excluded > 0] = 0.0
        k = self._texture_blur_px
        energy = cv2.blur(strength, (k, k))
        mask = np.where(energy > self._texture_energy, 255, 0).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((k, k), np.uint8))
        return cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))

    def _track_pair(self, prev, cur, mask):
        """Dots found in one view and followed to the next: (before, after)."""
        empty = np.empty((0, 2), np.float32)
        pts = cv2.goodFeaturesToTrack(prev, maxCorners=400, qualityLevel=0.01,
                                      minDistance=7, mask=mask)
        if pts is None:
            return empty, empty
        nxt, st, _ = cv2.calcOpticalFlowPyrLK(prev, cur, pts, None, **_LK)
        back, st2, _ = cv2.calcOpticalFlowPyrLK(cur, prev, nxt, None, **_LK)
        good = (st.ravel() == 1) & (st2.ravel() == 1) & (
            np.linalg.norm((pts - back).reshape(-1, 2), axis=1) < 1.5)
        return pts.reshape(-1, 2)[good], nxt.reshape(-1, 2)[good]

    def _way_out(self, solid: np.ndarray, centre):
        """"left", "right" or "up": the nearest open gap from the middle.

        Open means smooth and joined to the top of the view, where the sky
        is: a smooth patch inside an object, or haze under it, is not a way
        through. A gap also has to be `gap_min_px` wide, so a notch in an
        outline is not taken for one. An object that runs off the side of the
        view offers no gap on that side.

        Up is what the aircraft already does and it fails against rising
        ground, so it is chosen only when the gap above is clearly the
        nearest, or when there is no gap to either side.
        """
        h, w = solid.shape
        smooth = np.where(solid > 0, 0, 255).astype(np.uint8)
        _, labels = cv2.connectedComponents(smooth, connectivity=4)
        sky_labels = np.unique(labels[0][smooth[0] > 0])
        sky = np.isin(labels, sky_labels) & (smooth > 0)
        cx = int(np.clip(centre[0], 0, w - 1))
        cy = int(np.clip(centre[1], 0, h - 1))
        band_y = max(4, h // 10)
        band_x = max(4, w // 10)
        open_cols = sky[max(0, cy - band_y):cy + band_y].mean(axis=0) > 1.0 - _OPEN_STRIP_FRAC
        open_rows = sky[:, max(0, cx - band_x):cx + band_x].mean(axis=1) > 1.0 - _OPEN_STRIP_FRAC

        def nearest(flags):
            """Steps to the nearest run of `gap_min_px` open strips, or inf."""
            run = 0
            for step, is_open in enumerate(flags):
                run = run + 1 if is_open else 0
                if run >= self._gap_min_px:
                    return float(step - run + 1)
            return float("inf")

        left = nearest(open_cols[:cx][::-1])
        right = nearest(open_cols[cx:])
        up = nearest(open_rows[:cy][::-1])
        side = min(left, right)
        if side == float("inf") or up < self._up_bias * side:
            return "up"
        return "left" if left <= right else "right"

    def measure(self, frames, span_s: float, last_dt: "float | None" = None) -> ShapeReading:
        """Shapes from a tick's consecutive frames.

        `span_s` is first frame to last, for the outlines' growth. `last_dt`
        is between the last two, for the dots: they are tracked over that one
        interval only, because following them through every frame keeps the
        far, slow ones and loses the near, fast ones.
        """
        n = len(frames)
        if n < 2 or span_s <= 0.0:
            return ShapeReading(status="no-frames", frames=n)
        static = self._loom.static_mask
        if static is None:
            return ShapeReading(status="warming", span_s=span_s, frames=n)
        last_dt = span_s / (n - 1) if last_dt is None else last_dt
        first_view = self._loom._view(frames[0])
        prev_view = self._loom._view(frames[-2])
        last_view = self._loom._view(frames[-1])
        g_first, g_prev, g_last = first_view[0], prev_view[0], last_view[0]
        _, ox, oy, s = last_view
        if not (g_first.shape == g_prev.shape == g_last.shape == static.shape):
            return ShapeReading(status="size-changed", span_s=span_s, frames=n)
        if np.array_equal(g_first, g_last):
            return ShapeReading(status="same-frame", span_s=span_s, frames=n)
        not_static = cv2.bitwise_not(static)
        hud_first = cv2.bitwise_or(self._loom.hud_colour_mask(frames[0], g_first.shape), not_static)
        hud_prev = cv2.bitwise_or(self._loom.hud_colour_mask(frames[-2], g_prev.shape), not_static)
        hud_last = cv2.bitwise_or(self._loom.hud_colour_mask(frames[-1], g_last.shape), not_static)
        tex_first = self.texture_mask(g_first, hud_first)
        tex_last = self.texture_mask(g_last, hud_last)
        h, w = g_last.shape
        before, after = self._track_pair(g_prev, g_last, cv2.bitwise_not(hud_prev))
        if len(after):
            end = np.rint(after).astype(int)
            keep = ((end[:, 0] >= 0) & (end[:, 0] < w) & (end[:, 1] >= 0) & (end[:, 1] < h))
            keep[keep] = hud_last[end[keep, 1], end[keep, 0]] == 0
            before, after = before[keep], after[keep]

        def to_frame(p):
            return (np.asarray(p, np.float32) / s + np.array([ox, oy], np.float32)).astype(np.float32)

        fh, fw = frames[-1].shape[:2]
        bx1, by1, bx2, by2 = self._loom.path_box_pct
        box = [int(round(v)) for v in ((bx1 * fw - ox) * s, (by1 * fh - oy) * s,
                                       (bx2 * fw - ox) * s, (by2 * fh - oy) * s)]
        box = [int(np.clip(box[0], 0, w)), int(np.clip(box[1], 0, h)),
               int(np.clip(box[2], 0, w)), int(np.clip(box[3], 0, h))]
        centre = ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)
        n_first, labels_first, stats_first, _ = cv2.connectedComponentsWithStats(tex_first)
        contours, _ = cv2.findContours(tex_last, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        m = self._edge_margin_px
        dots_at = np.rint(after).astype(int) if len(after) else np.empty((0, 2), int)
        # Every outline filled in. A smooth patch inside an object is a hole
        # in the textured mask, not a gap to fly through.
        solid = np.zeros((h, w), np.uint8)
        shapes = []
        for contour in contours:
            area_last = float(cv2.contourArea(contour))
            if area_last < self._min_area_frac * tex_last.size:
                continue
            region = np.zeros((h, w), np.uint8)
            cv2.drawContours(region, [contour], -1, 255, -1)
            solid |= region
            x, y, bw, bh = cv2.boundingRect(contour)
            clipped = (x <= m, x + bw >= w - m, y <= m, y + bh >= h - m)
            # The same object in the first frame: the textured region there
            # that this one overlaps most.
            tau_outline = None
            area_first = 0.0
            under = labels_first[region > 0]
            under = under[under > 0]
            if len(under):
                label = int(np.bincount(under).argmax())
                area_first = float(stats_first[label, cv2.CC_STAT_AREA])
                growth = (area_last / area_first) ** 0.5 if area_first > 0 else 1.0
                if growth > 1.0 + _MIN_GROWTH:
                    tau_outline = span_s / (growth - 1.0)
            # The dots inside it, as one body.
            tau_dots = None
            dots = 0
            dots_measured = False
            if len(dots_at):
                inside = region[dots_at[:, 1], dots_at[:, 0]] > 0
                dots = int(inside.sum())
                if dots >= self._min_dots:
                    fit, inl = cv2.estimateAffinePartial2D(
                        before[inside], after[inside], method=cv2.RANSAC,
                        ransacReprojThreshold=3.0)
                    if fit is not None and inl is not None and int(inl.sum()) >= self._min_dots:
                        dots_measured = True
                        scale = float(np.hypot(fit[0, 0], fit[1, 0]))
                        if scale > 1.0 + _MIN_GROWTH:
                            tau_dots = last_dt / (scale - 1.0)
            # The dots are the measure of growth; the outline's own area is
            # too noisy at its border (module docstring) and is kept for the
            # log only. No dots inside means the growth is not known.
            tau = tau_dots
            measured = dots_measured
            covered = (region[box[1]:box[3], box[0]:box[2]] > 0).mean() if (
                box[3] > box[1] and box[2] > box[0]) else 0.0
            centred = bool(covered >= self._centre_cover
                           or region[centre[1], centre[0]] > 0)
            simple = cv2.approxPolyDP(contour, 2.0, True).reshape(-1, 2)
            shapes.append(Shape(
                outline=to_frame(simple), dots=dots,
                area_first=area_first / (s * s), area_last=area_last / (s * s),
                tau=tau, centred=centred, clipped=clipped,
                tau_outline=tau_outline, tau_dots=tau_dots, measured=measured))
        threats = [sh for sh in shapes
                   if sh.centred and sh.tau is not None and sh.tau < self.tau_warn_s]
        threat = min(threats, key=lambda sh: sh.tau) if threats else None
        blind = threat is None and any(sh.centred and not sh.measured for sh in shapes)
        # A threat does not vanish between ticks. Nothing in the middle and
        # almost nothing to follow, straight after one, is a view too smeared
        # to read (the ground is close), not open sky.
        lost = (self._after_threat and threat is None
                and not any(sh.centred for sh in shapes) and len(after) < self._lost_dots)
        blind = blind or lost
        return ShapeReading(
            status="ok", span_s=span_s, frames=n, shapes=tuple(shapes), threat=threat,
            avoid=None if threat is None else self._way_out(solid, centre),
            tracked=len(after), points=to_frame(after) if len(after) else
            np.empty((0, 2), np.float32), blind=blind, lost=lost)

    def update(self, reading: "ShapeReading | None") -> bool:
        """Advance the cross-tick confirm; the shadow verdict.

        "Stays in the middle of the screen and grows": the threat has to be
        there on `confirm_ticks` ticks running, and each tick's outline has to
        overlap the one before, so it is the same object. A tick with no
        reading, or with no threat, starts the count again.
        """
        threat = reading.threat if reading is not None and reading.readable else None
        if threat is not None:
            self._after_threat = True
        elif reading is not None and reading.readable and not reading.lost:
            self._after_threat = False      # the view was read and the middle is open
        if threat is None:
            self._streak = 0
        elif self._last_threat is not None and _overlap(threat.outline,
                                                        self._last_threat.outline):
            self._streak += 1
        else:
            self._streak = 1
        self._last_threat = threat
        warn = self._streak >= max(1, self._confirm_ticks)
        if warn and not self.warn:
            logger.warning(
                "SHAPE[shadow]: a shape in the middle of the screen is growing — time to "
                "contact %.1fs, %d dots, way out %s (HLDD 001 phase 2) "
                "[SHADOW - not actuating]",
                threat.tau, threat.dots, reading.avoid.upper())
        self.warn = warn
        return warn


def _overlap(a: np.ndarray, b: np.ndarray) -> bool:
    """Whether two outlines share any area (by their convex hulls)."""
    if len(a) < 3 or len(b) < 3:
        return False
    ha = cv2.convexHull(a.astype(np.float32))
    hb = cv2.convexHull(b.astype(np.float32))
    area, _ = cv2.intersectConvexConvex(ha, hb)
    return area > 0.0


def _fmt_s(tau: "float | None") -> str:
    return "-" if tau is None else f"{tau:.1f}s"


def fmt_middle(reading: "ShapeReading | None") -> str:
    """What covers the middle, threat or not, for the SHAPE log line.

    `outline 6.1s dots 5.4s n=42 clipped=LR` gives both growth figures for the
    largest centred shape, so a `clear` verdict can be told apart from a
    growth that was measured slow, measured from the wrong source, or not
    measured at all.
    """
    middle = None if reading is None or not reading.readable else reading.middle
    if middle is None:
        return "none"
    sides = "".join(c for c, on in zip("LRTB", middle.clipped, strict=True) if on) or "none"
    return (f"outline {_fmt_s(middle.tau_outline)} dots {_fmt_s(middle.tau_dots)} "
            f"n={middle.dots} clipped={sides}")


def fmt_shape(reading: "ShapeReading | None") -> str:
    """This tick's shape verdict for the BT log line.

    `4.2s:left` is a threat and its way out. `clear` is nothing growing in the
    middle. `blind` is something in the middle whose growth could not be
    measured, or a view that went unreadable straight after a threat: not
    clear, and not a threat. `n/a(reason)` is no reading.
    """
    if reading is None:
        return "n/a"
    if not reading.readable:
        return f"n/a({reading.status})"
    if reading.threat is None:
        return "blind" if reading.blind else "clear"
    return f"{reading.threat.tau:.1f}s:{reading.avoid}"

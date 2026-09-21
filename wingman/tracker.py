"""Screen-space target tracking for MetalStorm J-20 combat.

Detects enemy target-marker bars in the HUD via HSV contour filtering,
maintains a scan-mode state machine (ACQUIRING → TRACKING → LOST_GRACE),
and returns a per-frame observation dict consumed by the main loop.

Call update(frame) once per main-loop tick while in GAME_BATTLE.
Call reset() when leaving GAME_BATTLE.
Pass observation["error_norm"] to Controller.orient_nose_to_target().
"""

import logging
import time
from enum import Enum, auto

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class TrackMode(Enum):
    SEARCHING = auto()    # not in battle / between missions
    ACQUIRING = auto()    # scanning global acquisition region for first lock
    TRACKING = auto()     # locked; using local ROI each tick
    LOST_GRACE = auto()   # target lost; brief hold before reacquire or fallback


class TargetTracker:
    """Detect target-marker bars and compute proportional roll-error signal.

    Target bars are tall, narrow, bright-green (non-locked) or red (locked)
    vertical elements in the MetalStorm HUD.  The lock-on reticle (dashed
    circle) is rejected by the aspect-ratio filter (height/width < 2.5).

    Thread-safety: not thread-safe; call only from the main loop thread.
    """

    def __init__(self, config: dict) -> None:
        cfg = config.get("tracking", {})
        self._enabled = bool(cfg.get("enabled", False))

        acq = cfg.get("acquisition_region_pct", [0.20, 0.18, 0.80, 0.68])
        self._acq_x1 = float(acq[0])
        self._acq_y1 = float(acq[1])
        self._acq_x2 = float(acq[2])
        self._acq_y2 = float(acq[3])

        self._lost_timeout = float(cfg.get("lost_timeout_sec", 0.70))
        self._prefer_red = bool(cfg.get("prefer_red_lock", True))

        self._local_roi_enabled = bool(cfg.get("local_roi_enabled", True))
        self._roi_scale = float(cfg.get("local_roi_scale", 0.22))
        roi_min = cfg.get("local_roi_min_px", [140, 90])
        self._roi_min_w = int(roi_min[0])
        self._roi_min_h = int(roi_min[1])
        self._roi_expand = float(cfg.get("local_roi_expand_factor", 1.25))
        self._roi_max_scale = float(cfg.get("local_roi_max_scale", 0.45))
        self._roi_reacquire_cycles = int(cfg.get("local_roi_reacquire_cycles", 3))

        hsv = config.get("tracking_hsv", {})
        self._red_lower = np.array(hsv.get("red_lower", [0, 150, 150]), dtype=np.uint8)
        self._red_upper = np.array(hsv.get("red_upper", [10, 255, 255]), dtype=np.uint8)
        self._green_lower = np.array(hsv.get("green_lower", [45, 150, 150]), dtype=np.uint8)
        self._green_upper = np.array(hsv.get("green_upper", [75, 255, 255]), dtype=np.uint8)
        self._min_area = float(hsv.get("min_contour_area", 12))
        self._min_aspect = float(hsv.get("min_aspect_ratio", 2.5))

        # ADR 136: padlock-off indicator — a dashed green ring centered on
        # screen when padlock is disengaged (calibrated against a live
        # capture 2026-09-09: ~40 short dash segments, H 40-75/S 30-200/
        # V 100-255, spanning roughly [0.42,0.39]-[0.67,0.67] of the frame —
        # not a single filled dot, an earlier draft of this detector assumed
        # a dot and never found the ring at all). Own config block, own HSV
        # bounds — deliberately not reusing tracking_hsv/acquisition_region
        # so calibrating one detector can't drift the other.
        pli = config.get("padlock_indicator", {})
        self._padlock_region_pct = [float(v) for v in pli.get(
            "region_pct", [0.42, 0.39, 0.67, 0.67])]
        self._padlock_green_lower = np.array(
            pli.get("green_lower", [40, 30, 100]), dtype=np.uint8)
        self._padlock_green_upper = np.array(
            pli.get("green_upper", [75, 200, 255]), dtype=np.uint8)
        self._padlock_min_area = float(pli.get("min_contour_area", 3))
        self._padlock_max_area = float(pli.get("max_contour_area", 30))
        self._padlock_min_dashes = int(pli.get("min_dashes", 6))

        # ADR 140: a small solid green dot fixed at exact screen center —
        # a different element from the dashed ring above, with its own
        # config block deliberately kept separate so calibrating one never
        # drifts the other. Measured 2026-09-17 against 8 real padlock-off
        # captures: OpenCV HSV (55, 76, 224), byte-identical across all 8
        # regardless of roll/pitch/target state, unlike the ring (which
        # moved to a different screen position in every one of those same
        # frames). Padlock-ON comparison not yet done — see ADR 140 Open
        # Question 1.
        pci = config.get("padlock_center_indicator", {})
        self._padlock_center_region_pct = [float(v) for v in pci.get(
            "region_pct", [0.485, 0.47, 0.515, 0.53])]
        self._padlock_center_green_lower = np.array(
            pci.get("green_lower", [50, 60, 200]), dtype=np.uint8)
        self._padlock_center_green_upper = np.array(
            pci.get("green_upper", [60, 90, 255]), dtype=np.uint8)
        self._padlock_center_min_pixels = int(pci.get("min_pixels", 3))

        self._mode = TrackMode.SEARCHING
        self._last_x: "float | None" = None
        self._last_y: "float | None" = None
        self._last_seen_ts: float = 0.0
        self._current_roi_scale: float = 0.0
        self._roi_rect: "tuple[int, int, int, int] | None" = None
        self._roi_miss_count: int = 0
        # HLDD 005 Selection Hardening (2026-09-21): rate-limited rationale
        # logging counter, same shape as every other shadow counter in this
        # codebase (ADR 117 D9 / HLDD 013 Phase 1) — 1st/10th/100th, then
        # every 500th. Pure logging; never changes what gets selected.
        self._selection_shadow_count: int = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def mode(self) -> TrackMode:
        return self._mode

    def reset(self) -> None:
        """Clear all tracking state. Call when leaving GAME_BATTLE."""
        self._mode = TrackMode.SEARCHING
        self._last_x = None
        self._last_y = None
        self._last_seen_ts = 0.0
        self._current_roi_scale = 0.0
        self._roi_rect = None
        self._roi_miss_count = 0
        logger.debug("TargetTracker: reset to SEARCHING")

    def update(self, frame: np.ndarray, ts: "float | None" = None) -> dict:
        """Process one frame; update state machine; return observation dict.

        Returns:
            mode        — TrackMode name string
            visible     — True if a target was detected this tick
            centroid_x  — frame-absolute x of selected target (or last known)
            centroid_y  — frame-absolute y of selected target (or last known)
            error_norm  — horizontal error in [-1, 1]; positive = target right of center
                          (this is "error_norm_x" in HLDD 005's 2026-09-21 revision;
                          the dict key is kept as-is so existing callers are unaffected)
            error_norm_y — vertical error in [-1, 1]; positive = target below center.
                          New (HLDD 005 Two-Axis Rollout Phase 1) — sensing only, no
                          consumer presses a key from this yet.
            n_detections — raw contour count found in scan region
            roi_rect    — active local ROI as (x, y, w, h) in frame coords, or None
        """
        if ts is None:
            ts = time.time()
        h, w = frame.shape[:2]

        if self._mode == TrackMode.SEARCHING:
            self._mode = TrackMode.ACQUIRING

        # LOST_GRACE keeps scanning the (progressively expanded, see _handle_miss)
        # local ROI around the last-known position instead of falling back to a
        # full acquisition-region scan — that expansion has no effect otherwise.
        use_roi = (
            self._local_roi_enabled
            and self._mode in (TrackMode.TRACKING, TrackMode.LOST_GRACE)
            and self._roi_rect is not None
        )
        if use_roi:
            rx, ry, rw, rh = self._roi_rect
            crop = frame[ry:ry + rh, rx:rx + rw]
            ox, oy = rx, ry
        else:
            ax1 = int(w * self._acq_x1)
            ay1 = int(h * self._acq_y1)
            ax2 = int(w * self._acq_x2)
            ay2 = int(h * self._acq_y2)
            crop = frame[ay1:ay2, ax1:ax2]
            ox, oy = ax1, ay1

        local_hits, local_discarded_green = self._detect_targets(crop)
        abs_hits = [(ox + lx, oy + ly, a) for lx, ly, a in local_hits]
        selected = self._select_target(abs_hits, w)

        if local_discarded_green:
            ref = self._last_x if self._last_x is not None else w / 2.0
            self._log_selection_rationale(ox, oy, local_discarded_green, selected, ref)

        if selected is not None:
            abs_x, abs_y = selected
            self._last_x = abs_x
            self._last_y = abs_y
            self._last_seen_ts = ts
            self._roi_miss_count = 0
            if self._mode in (TrackMode.ACQUIRING, TrackMode.LOST_GRACE):
                self._mode = TrackMode.TRACKING
                self._current_roi_scale = self._roi_scale
                logger.debug("TargetTracker: acquired target at (%.0f, %.0f)", abs_x, abs_y)
            self._roi_rect = self._compute_roi(abs_x, abs_y, w, h)
        else:
            self._handle_miss(ts, w, h)

        error_norm: "float | None" = None
        error_norm_y: "float | None" = None
        if self._last_x is not None and self._mode != TrackMode.SEARCHING:
            error_norm = float(np.clip((self._last_x - w / 2.0) / (w / 2.0), -1.0, 1.0))
            error_norm_y = float(np.clip((self._last_y - h / 2.0) / (h / 2.0), -1.0, 1.0))

        return {
            "mode": self._mode.name,
            "visible": selected is not None,
            "centroid_x": self._last_x,
            "centroid_y": self._last_y,
            "error_norm": error_norm,
            "error_norm_y": error_norm_y,
            "n_detections": len(local_hits),
            "roi_rect": self._roi_rect,
        }

    def detect_padlock_off(self, frame: np.ndarray) -> bool:
        """ADR 136: True if the padlock-off indicator — a dashed green ring
        centered on screen, made of many short dash segments rather than
        one filled shape — is visible this frame.

        Counts contours sized like a single dash (min AND max area bounds,
        no aspect-ratio filter since dashes are short curved strokes, not
        tall bars) and requires at least min_dashes of them, rather than
        treating any one green blob as confirmation — a single stray green
        pixel elsewhere in the region would otherwise false-positive.
        """
        if frame is None or frame.size == 0:
            return False
        h, w = frame.shape[:2]
        x1 = int(w * self._padlock_region_pct[0])
        y1 = int(h * self._padlock_region_pct[1])
        x2 = int(w * self._padlock_region_pct[2])
        y2 = int(h * self._padlock_region_pct[3])
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return False
        mask = cv2.inRange(hsv, self._padlock_green_lower, self._padlock_green_upper)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dash_count = sum(
            1 for c in contours
            if self._padlock_min_area <= cv2.contourArea(c) <= self._padlock_max_area
        )
        return dash_count >= self._padlock_min_dashes

    def detect_padlock_center_dot(self, frame: np.ndarray) -> bool:
        """ADR 140: True if the fixed-screen-center padlock-off dot is
        visible this frame.

        Deliberately a tight, fixed crop rather than tracking the moving
        ring `detect_padlock_off` reads (ADR 136 D4 found that ring drifts
        with flight attitude) — this detector will simply fail to find the
        dot, not misidentify something else, whenever it isn't at true
        screen center. That is the safe failure mode for a signal only
        ever consumed as one leg of a multi-tick, multi-signal fusion
        (ADR 140 D4), not as a per-frame source of truth on its own.
        """
        if frame is None or frame.size == 0:
            return False
        h, w = frame.shape[:2]
        x1 = int(w * self._padlock_center_region_pct[0])
        y1 = int(h * self._padlock_center_region_pct[1])
        x2 = int(w * self._padlock_center_region_pct[2])
        y2 = int(h * self._padlock_center_region_pct[3])
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return False
        mask = cv2.inRange(hsv, self._padlock_center_green_lower,
                           self._padlock_center_green_upper)
        return int(np.count_nonzero(mask)) >= self._padlock_center_min_pixels

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _handle_miss(self, ts: float, w: int, h: int) -> None:
        if self._mode == TrackMode.TRACKING:
            self._mode = TrackMode.LOST_GRACE
            self._roi_miss_count = 1
        elif self._mode == TrackMode.LOST_GRACE:
            self._roi_miss_count += 1
            elapsed = ts - self._last_seen_ts
            if elapsed >= self._lost_timeout:
                logger.debug("TargetTracker: grace timeout after %.2fs — ACQUIRING", elapsed)
                self._mode = TrackMode.ACQUIRING
                self._last_x = None
                self._last_y = None
                self._roi_rect = None
                self._roi_miss_count = 0
            elif self._roi_miss_count >= self._roi_reacquire_cycles:
                new_scale = min(self._current_roi_scale * self._roi_expand, self._roi_max_scale)
                self._current_roi_scale = new_scale
                self._roi_miss_count = 0
                if self._last_x is not None and self._last_y is not None:
                    self._roi_rect = self._compute_roi(self._last_x, self._last_y, w, h)
                if new_scale >= self._roi_max_scale:
                    logger.debug("TargetTracker: ROI at max scale — falling back to ACQUIRING")
                    self._mode = TrackMode.ACQUIRING

    def _contours_to_hits(self, mask: np.ndarray) -> "list[tuple[float, float, float]]":
        """Return (cx, cy, area) in crop-local coords for each valid target bar in `mask`."""
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        results: "list[tuple[float, float, float]]" = []
        for c in contours:
            area = float(cv2.contourArea(c))
            if area < self._min_area:
                continue
            x, y, cw, ch = cv2.boundingRect(c)
            if cw == 0 or ch / cw < self._min_aspect:
                continue
            results.append((float(x + cw / 2), float(y + ch / 2), area))
        return results

    def _detect_targets(
        self, crop: np.ndarray
    ) -> "tuple[list[tuple[float, float, float]], list[tuple[float, float, float]]]":
        """Return (hits, discarded_green) in crop-local coords.

        `hits` is exactly what this method always returned — the winning
        color class's valid target bars, unchanged by HLDD 005 Selection
        Hardening below. `discarded_green` is new (2026-09-21, logging only):
        the green candidates that were excluded *because* red won, so the
        caller can log how often — and by how much — that exclusion
        discarded something closer to the tracked/centered position than
        the surviving red candidate. Only ever non-empty when red actually
        won (rule 1 can only discard candidates in that one direction); when
        green wins there is nothing red to have discarded, so no extra
        contour pass is spent computing an always-empty list.
        """
        if crop is None or crop.size == 0:
            return [], []
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return [], []

        # Red: locked target — hue wraps; check primary + wrap-around range
        red_wrap_lower = np.array(
            [170, int(self._red_lower[1]), int(self._red_lower[2])], dtype=np.uint8
        )
        red_wrap_upper = np.array(
            [180, int(self._red_upper[1]), int(self._red_upper[2])], dtype=np.uint8
        )
        mask_red = (
            cv2.inRange(hsv, self._red_lower, self._red_upper)
            | cv2.inRange(hsv, red_wrap_lower, red_wrap_upper)
        )
        mask_green = cv2.inRange(hsv, self._green_lower, self._green_upper)

        red_won = self._prefer_red and bool(np.any(mask_red))
        mask = mask_red if red_won else mask_green

        hits = self._contours_to_hits(mask)
        discarded_green = self._contours_to_hits(mask_green) if red_won else []
        return hits, discarded_green

    def _log_selection_rationale(
        self,
        ox: int,
        oy: int,
        local_discarded_green: "list[tuple[float, float, float]]",
        selected: "tuple[float, float] | None",
        ref: float,
    ) -> None:
        """HLDD 005 Selection Hardening Phase 1: rate-limited rationale log.

        Pure logging — never changes `selected`. Fires only on the tick
        `_detect_targets` found at least one green candidate that rule 1
        (color priority) discarded in favor of red. Reports how far the
        discarded candidate(s) sat from `ref` compared to the target that
        was actually selected, which is exactly the evidence Selection
        Hardening's Phase 1 needs before Phase 2's ranked-pool change is
        worth writing at all.
        """
        self._selection_shadow_count += 1
        n = self._selection_shadow_count
        if n not in (1, 10, 100) and n % 500 != 0:
            return
        discarded_abs = [(ox + lx, oy + ly) for lx, ly, _area in local_discarded_green]
        discarded_desc = ", ".join(
            f"(x={dx:.0f} dist={abs(dx - ref):.0f})" for dx, _dy in discarded_abs
        )
        if selected is not None:
            selected_desc = f"x={selected[0]:.0f} dist={abs(selected[0] - ref):.0f}"
        else:
            selected_desc = "none"
        logger.info(
            "SELECT[shadow]: red won, discarded %d green candidate(s) [%s]; "
            "selected %s (%d so far)",
            len(discarded_abs), discarded_desc, selected_desc, n,
        )

    def _select_target(
        self,
        abs_hits: "list[tuple[float, float, float]]",
        frame_w: int,
    ) -> "tuple[float, float] | None":
        if not abs_hits:
            return None
        ref = self._last_x if self._last_x is not None else frame_w / 2.0
        best = min(abs_hits, key=lambda d: abs(d[0] - ref))
        return (best[0], best[1])

    def _compute_roi(self, cx: float, cy: float, fw: int, fh: int) -> "tuple[int, int, int, int]":
        rw = max(self._roi_min_w, int(fw * self._current_roi_scale))
        rh = max(self._roi_min_h, int(fh * self._current_roi_scale))
        x = int(max(0, min(cx - rw / 2, fw - rw)))
        y = int(max(0, min(cy - rh / 2, fh - rh)))
        return (x, y, rw, rh)

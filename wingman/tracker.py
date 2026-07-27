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

        self._mode = TrackMode.SEARCHING
        self._last_x: "float | None" = None
        self._last_y: "float | None" = None
        self._last_seen_ts: float = 0.0
        self._current_roi_scale: float = 0.0
        self._roi_rect: "tuple[int, int, int, int] | None" = None
        self._roi_miss_count: int = 0

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

        local_hits = self._detect_targets(crop)
        abs_hits = [(ox + lx, oy + ly, a) for lx, ly, a in local_hits]
        selected = self._select_target(abs_hits, w)

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
        if self._last_x is not None and self._mode != TrackMode.SEARCHING:
            error_norm = float(np.clip((self._last_x - w / 2.0) / (w / 2.0), -1.0, 1.0))

        return {
            "mode": self._mode.name,
            "visible": selected is not None,
            "centroid_x": self._last_x,
            "centroid_y": self._last_y,
            "error_norm": error_norm,
            "n_detections": len(local_hits),
            "roi_rect": self._roi_rect,
        }

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

    def _detect_targets(self, crop: np.ndarray) -> "list[tuple[float, float, float]]":
        """Return (cx, cy, area) in crop-local coords for each valid target bar."""
        if crop is None or crop.size == 0:
            return []
        try:
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        except Exception:
            return []

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

        mask = mask_red if (self._prefer_red and bool(np.any(mask_red))) else mask_green

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

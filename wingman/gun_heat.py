"""The gun heat bar, and whether the gun has overheated (ADR 173 D2b).

The bar sits under the crosshair while the gun is in use (Design 021: x 760 to 1160,
y 813 on 1920x1200): a grey track over a solid green bracket line, with a fill that
grows outward from the centre as the gun heats. Measured on the 13 hand-flown gun
frames of 2026-11-13 and the MiG-23 frame of 2026-11-07:

- the bracket is a green row across the whole crop (rows 816-818), and only shows with
  the bar;
- the fill is the six rows above it, one run through the centre: hue 16 (orange) on
  every heating frame (fill 0.03 to 0.65 of the bar), hue 6-7 (red) on both overheated
  frames ("OVERHEAT" above it), at fill 0.53 and 0.50.

So "overheated" is a red fill on the bar, at any width: the bar cools while the gun
stays locked out. The operator (2026-11-15) wants the gun cool-down before that:
"we should activate when the overheat indicator reaches 80%". So the reader's state
is "hot": the fill at `hot_at` (0.8) of the bar or more, or red. One read sets it
and one clears it (D2f: the bar goes from 0.8 to overheated in about a second, less
than the 1.5 s between reads). The widest orange fill in those frames was 0.65, so whether
the bar is still orange at 0.8 is not yet seen; the red case covers it either way.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

_BRACKET_MIN_GREEN = 0.85        # of the crop's width, on one row
_FILL_ROWS_PER_1200 = 6          # the fill's height above the bracket, at 1200 px high
_OVERHEAT_MAX_HUE = 11           # OpenCV hue (0-180): red 6-7 overheated, orange 16 heating


def read_heat(frame, crop_box) -> "tuple[float, float] | None":
    """(fill, hue) of the gun heat bar in this BGR frame; (0.0, nan) when the bar is up
    with no fill; None when the bar is not on screen."""
    detail = read_heat_detail(frame, crop_box)
    return None if detail is None else (detail["fill"], detail["hue"])


def read_heat_detail(frame, crop_box) -> "dict | None":
    """read_heat's reading with what it was made from, for the GUNHEAT: line:
    `fill` (the run of fill through the centre), `hue`, and `reach_l`/`reach_r`, how
    far the fill pixels reach from the centre towards each end (0..1), whether or not
    the centre is covered. On 2026-11-07's MiG-23 frame the centre run read 0.50
    while red reached nearly both ends, so the two can disagree (ADR 173 D2e)."""
    import cv2

    h, w = frame.shape[:2]
    x1, y1, x2, y2 = crop_box[:4]
    xa, xb = int(w * x1), int(w * x2)
    ya, yb = int(h * y1), int(h * y2)
    if xb - xa < 10 or yb - ya < 4:
        return None
    hsv = cv2.cvtColor(np.ascontiguousarray(frame[ya:yb, xa:xb]),
                       cv2.COLOR_BGR2HSV).astype(np.int16)
    H, S, V = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    green = ((H >= 35) & (H <= 85) & (S >= 60) & (V >= 150)).mean(axis=1)
    gy = int(np.argmax(green))
    if green[gy] < _BRACKET_MIN_GREEN:
        return None
    top = max(0, gy - max(1, int(round(h * _FILL_ROWS_PER_1200 / 1200))))
    nothing = {"fill": 0.0, "hue": float("nan"), "reach_l": 0.0, "reach_r": 0.0}
    if top >= gy:
        return nothing
    fill = ((S[top:gy] >= 150) & (V[top:gy] >= 180)
            & ((H[top:gy] <= 22) | (H[top:gy] >= 172)))
    cols = fill.mean(axis=0) >= 0.5
    cx = cols.shape[0] // 2
    lit_cols = np.flatnonzero(cols)
    reach_l = reach_r = 0.0
    if lit_cols.size:
        reach_l = max(0.0, (cx - int(lit_cols.min())) / max(cx, 1))
        reach_r = max(0.0, (int(lit_cols.max()) - cx) / max(cols.shape[0] - 1 - cx, 1))
    if not cols[cx]:
        return dict(nothing, reach_l=reach_l, reach_r=reach_r)
    left = cx
    while left > 0 and cols[left - 1]:
        left -= 1
    right = cx
    while right < cols.shape[0] - 1 and cols[right + 1]:
        right += 1
    hues = H[top:gy, left:right + 1][fill[:, left:right + 1]].copy()
    hues[hues >= 172] -= 180          # red wraps round 180
    return {"fill": (right - left + 1) / cols.shape[0], "hue": float(np.median(hues)),
            "reach_l": reach_l, "reach_r": reach_r}


class GunHeatReader:
    """Whether the gun is overheated, read each battle tick, for the handlers to ask."""

    def __init__(self, crop_box, hot_at: float = 0.8, stale_after_s: float = 5.0,
                 clock=time.time):
        self._box = crop_box
        self._hot_at = float(hot_at)
        self._stale_after_s = float(stale_after_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._streak = 0
        self._hot = False
        self._ts = 0.0

    def update(self, frame) -> None:
        """Read the bar in this frame. Never raises."""
        if self._box is None or frame is None or getattr(frame, "ndim", 0) != 3:
            return
        try:
            detail = read_heat_detail(frame, self._box)
        except Exception as exc:  # perception must not cost the tick
            logger.debug("Gun heat: read failed: %s", exc)
            return
        if detail is not None:
            # ADR 173 D2e: one line per read while the bar is up, to see how high a
            # burst takes the heat and which measure tracks the game's bar.
            logger.debug("GUNHEAT: fill=%.2f reach=%.2f/%.2f hue=%s", detail["fill"],
                         detail["reach_l"], detail["reach_r"],
                         "-" if detail["hue"] != detail["hue"] else "%.0f" % detail["hue"])
        self.note(None if detail is None else (detail["fill"], detail["hue"]))

    def note(self, reading) -> None:
        """Record one read: a (fill, hue) pair, or None with no bar on screen."""
        filled = reading is not None and reading[0] > 0
        red = filled and reading[1] == reading[1] and reading[1] <= _OVERHEAT_MAX_HUE
        hot = filled and (red or reading[0] >= self._hot_at)
        with self._lock:
            self._streak = self._streak + 1 if hot else 0
            was = self._hot
            # One read, not two (ADR 173 D2f): on 2026-11-15 at 12:09 the F-100's bar
            # read 0.86 orange and, 1.5 s later, red (overheated), so a second read
            # can only ever confirm the overheat the 80% press is meant to prevent.
            self._hot = self._streak >= 1
            self._ts = self._clock()
        if self._hot != was:
            logger.info("Gun heat: %s%s", ("OVERHEATED" if red else "HOT")
                        if self._hot else "cooled",
                        f" (fill {reading[0]:.2f}, hue {reading[1]:.0f})" if hot else "")

    def hot(self) -> "bool | None":
        """True while the bar is at `hot_at` or more, or red; False when not; None with
        no fresh read."""
        with self._lock:
            if self._ts == 0.0 or self._clock() - self._ts > self._stale_after_s:
                return None
            return self._hot

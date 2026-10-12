"""Compass heading from the minimap rim (Design 017, phase 4).

The minimap's rim carries the compass letters, and they turn as the view turns.
N is the only one drawn in orange. Where it sits on the rim says which way the
top of the minimap points:

    heading = (360 - bearing of N, clockwise from the top) mod 360

With the padlock camera off the top of the minimap is the aircraft's nose, so
that is the aircraft's heading. With the padlock on, the minimap follows the
camera and this is the camera's bearing, not the aircraft's (Design 017, "Phase
1, stitching and placing").

The rim is see-through. Over orange rock the backdrop floods it with redder,
wider blobs, and enemy markers on the rim are yellow. The letter is told from
both by being a small blob of one hue at one distance from the centre, and by
being the only such blob: with two candidates the reader says nothing, because
a wrong heading is worse than none.

Standard library, numpy and OpenCV only. Shadow use: nothing steers on this yet.
"""

import math

import cv2
import numpy as np

from .config_schema import schema_default


def _d(key: str):
    return schema_default(f"minimap.compass.{key}")


class CompassReader:
    """Reads the heading from a minimap crop (the MINIMAP crop region, BGR)."""

    def __init__(self, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}
        self.enabled = bool(cfg.get("enabled", _d("enabled")))
        self._lower = np.array(cfg.get("n_hsv_lower", _d("n_hsv_lower")), dtype=np.uint8)
        self._upper = np.array(cfg.get("n_hsv_upper", _d("n_hsv_upper")), dtype=np.uint8)
        self._band = (float(cfg.get("rim_inner_frac", _d("rim_inner_frac"))),
                      float(cfg.get("rim_outer_frac", _d("rim_outer_frac"))))
        self._area = (float(cfg.get("letter_min_area_frac", _d("letter_min_area_frac"))),
                      float(cfg.get("letter_max_area_frac", _d("letter_max_area_frac"))))
        self._grid: "tuple | None" = None   # (width, height, dx, dy, r) for the last crop size

    def _geometry(self, width: int, height: int):
        grid = self._grid
        if grid is None or grid[0] != width or grid[1] != height:
            cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
            yy, xx = np.mgrid[:height, :width]
            dx, dy = xx - cx, yy - cy
            grid = (width, height, dx, dy, np.hypot(dx, dy))
            self._grid = grid
        return grid[2], grid[3], grid[4]

    def north_bearing(self, crop: np.ndarray) -> "float | None":
        """Where N sits on the rim: degrees clockwise from the top, -180 to 180. None if not read."""
        if crop is None or crop.size == 0:
            return None
        height, width = crop.shape[:2]
        radius = min(width, height) / 2.0
        if radius < 20:
            return None
        dx, dy, r = self._geometry(width, height)
        mask = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), self._lower, self._upper)
        mask[(r < self._band[0] * radius) | (r > self._band[1] * radius)] = 0
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
        lo, hi = self._area[0] * radius * radius, self._area[1] * radius * radius
        found = None
        for label in range(1, count):
            if not lo <= stats[label, cv2.CC_STAT_AREA] <= hi:
                continue
            if found is not None:
                return None             # two letters' worth of orange: do not guess
            ys, xs = np.nonzero(labels == label)
            found = math.degrees(math.atan2(dx[ys, xs].mean(), -dy[ys, xs].mean()))
        return found

    def heading(self, crop: np.ndarray) -> "float | None":
        """Compass heading of the top of the minimap, 0 to 360 (0 north, 90 east). None if not read."""
        bearing = self.north_bearing(crop)
        return None if bearing is None else (360.0 - bearing) % 360.0


def fmt_heading(heading: "float | None", reason: "str | None" = None) -> str:
    """`214` for a reading, `n/a` or `n/a(reason)` for none, for the BT log line."""
    if heading is None:
        return f"n/a({reason})" if reason else "n/a"
    return f"{round(heading) % 360}"

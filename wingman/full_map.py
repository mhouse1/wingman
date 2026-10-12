"""Where the aircraft is, read from the game's full map (Design 017, "The full map").

`m` puts the whole arena on screen: a north-up disc in the middle of the
picture, with the aircraft's own icon and view cone drawn at their true place.
One picture gives a position in the arena and a heading, with nothing to stitch
and nothing to search:

    east  = (icon x - centre x) / radius        north = (centre y - icon y) / radius

Both are in arena radii, so (0, 0) is the middle of the arena and a distance of
1 from it is the edge.

The map covers the forward view and the speed and altitude readouts while it
is open, so it is a look of under a second, not something to fly with. Measured
on 2026-10-04 16:43: fully drawn 0.35 s after the key, gone 0.33 s after the
second press. For the first 0.3 s the disc is on screen but still moving, and
the letters are not drawn yet; the orange N at the top of the rim is the sign
that it has settled, so nothing is read until the N is there. The dark ring
round the disc is see-through and is not relied on: over bright cloud it is
not dark.

The reader says nothing unless it is sure: no N, or two candidates for the own
icon, and there is no fix. A wrong position is worse than none.

Standard library, numpy and OpenCV only.
"""

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config_schema import schema_default


def _d(key: str):
    return schema_default(f"full_map.{key}")


@dataclass(frozen=True)
class FullMapFix:
    """A position in arena radii and, when the view cone was found, a heading."""

    east: float
    north: float
    heading: "float | None" = None      # compass degrees, from the direction of the view cone

    @property
    def radius(self) -> float:
        """Distance from the arena's centre, in arena radii: 1.0 is the edge."""
        return math.hypot(self.east, self.north)


class FullMapReader:
    """Reads a full frame (BGR) that may or may not have the full map open."""

    def __init__(self, cfg: "dict | None" = None) -> None:
        cfg = cfg or {}

        def get(key):
            return cfg.get(key, _d(key))

        self.enabled = bool(get("enabled"))
        self._radius_frac = float(get("radius_frac"))
        self._n_offset = float(get("n_offset_frac"))
        self._n_window = float(get("n_window_frac"))
        self._n_lower = np.array(get("n_hsv_lower"), dtype=np.uint8)
        self._n_upper = np.array(get("n_hsv_upper"), dtype=np.uint8)
        self._n_area = (float(get("n_min_area_frac")), float(get("n_max_area_frac")))
        self._n_clear = float(get("n_clear_frac"))
        self._n_around_max = float(get("n_around_max"))
        self._ring = (float(get("ring_inner_frac")), float(get("ring_outer_frac")))
        self._ring_dark_v = int(get("ring_dark_v"))
        self._ring_dark_share = float(get("ring_dark_share"))
        self._icon_max_s = int(get("icon_max_s"))
        self._icon_min_v = int(get("icon_min_v"))
        self._icon_area = (float(get("icon_min_area_frac")), float(get("icon_max_area_frac")))
        self._cone_lower = np.array(get("cone_hsv_lower"), dtype=np.uint8)
        self._cone_upper = np.array(get("cone_hsv_upper"), dtype=np.uint8)
        self._cone_min_area = float(get("cone_min_area_frac"))
        self._cone_max_dist = float(get("cone_max_dist_frac"))
        self._grid: "tuple | None" = None   # (width, height, dx, dy, r, radius) for the last frame size

    def _geometry(self, width: int, height: int):
        grid = self._grid
        if grid is None or grid[0] != width or grid[1] != height:
            cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
            yy, xx = np.mgrid[:height, :width]
            dx, dy = xx - cx, yy - cy
            grid = (width, height, dx, dy, np.hypot(dx, dy), self._radius_frac * height)
            self._grid = grid
        return grid[2], grid[3], grid[4], grid[5]

    def _settled(self, hsv: np.ndarray, dx, dy, radius: float) -> bool:
        """The orange N is at the top of the rim, and it is alone there.

        Not the ring's darkness: the ring is see-through. Over a dark scene
        0.80 to 0.92 of it reads dark; over bright cloud, just after a spawn,
        0.64 to 0.66 did in a compressed copy of the picture and less than
        the 0.6 then asked for in the picture itself, and an open map went
        unrecognised for ten seconds (thirteenth flight, 2026-10-04 18:01:11
        and 18:02:12). The N's size did not move: 0.0033 to 0.0041 of the
        radius squared on every picture of the map, with no other orange
        near it. Canyon rock is orange too, and it is never an N-sized patch
        by itself at that spot: 274 flight pictures, none.
        """
        from_n = np.hypot(dx, dy + self._n_offset * radius)
        orange = cv2.inRange(hsv, self._n_lower, self._n_upper) > 0
        area = float((orange & (from_n < self._n_window * radius)).sum()) / (radius * radius)
        if not self._n_area[0] <= area <= self._n_area[1]:
            return False
        around = float((orange & (from_n >= self._n_window * radius)
                        & (from_n < self._n_clear * radius)).sum()) / (radius * radius)
        return around <= self._n_around_max * area

    def covering(self, frame: np.ndarray) -> bool:
        """The dark ring is there, whether or not the N has been drawn: the map
        is on screen, or still arriving, or the scene happens to be dark there.
        Weaker than `shown`, and only for deciding to close a map that was just
        opened and could not be read."""
        if frame is None or frame.size == 0 or frame.shape[0] < 200:
            return False
        _dx, _dy, r, radius = self._geometry(frame.shape[1], frame.shape[0])
        ring = (r > self._ring[0] * radius) & (r < self._ring[1] * radius)
        value = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)[..., 2][ring]
        return float((value < self._ring_dark_v).mean()) >= self._ring_dark_share

    def shown(self, frame: np.ndarray) -> bool:
        """The full map is open and has finished drawing."""
        if frame is None or frame.size == 0 or frame.shape[0] < 200:
            return False
        dx, dy, r, radius = self._geometry(frame.shape[1], frame.shape[0])
        return self._settled(cv2.cvtColor(frame, cv2.COLOR_BGR2HSV), dx, dy, radius)

    def read(self, frame: np.ndarray) -> "FullMapFix | None":
        """The aircraft's place on the full map, or None when it is not open or not sure."""
        if frame is None or frame.size == 0 or frame.shape[0] < 200:
            return None
        dx, dy, r, radius = self._geometry(frame.shape[1], frame.shape[0])
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        if not self._settled(hsv, dx, dy, radius):
            return None
        # The own icon is the only pure white inside the disc: the markers are
        # yellow and green, the grid is grey, and the white letters are outside.
        white = ((hsv[..., 1] <= self._icon_max_s) & (hsv[..., 2] >= self._icon_min_v)
                 & (r < 0.987 * radius)).astype(np.uint8)
        count, _labels, stats, centres = cv2.connectedComponentsWithStats(white)
        lo, hi = self._icon_area[0] * radius * radius, self._icon_area[1] * radius * radius
        icons = [centres[i] for i in range(1, count) if lo <= stats[i, cv2.CC_STAT_AREA] <= hi]
        if len(icons) != 1:
            return None             # none, or two whites of the icon's size: do not guess
        ix, iy = float(icons[0][0]), float(icons[0][1])
        cx, cy = (frame.shape[1] - 1) / 2.0, (frame.shape[0] - 1) / 2.0
        return FullMapFix(east=(ix - cx) / radius, north=(cy - iy) / radius,
                          heading=self._cone_heading(hsv, ix, iy, radius))

    def _cone_heading(self, hsv: np.ndarray, ix: float, iy: float, radius: float) -> "float | None":
        """The compass direction from the icon to the middle of its green view cone."""
        green = cv2.inRange(hsv, self._cone_lower, self._cone_upper)
        green = cv2.morphologyEx(green, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
        count, _labels, stats, centres = cv2.connectedComponentsWithStats(green)
        best = None
        for i in range(1, count):
            area = float(stats[i, cv2.CC_STAT_AREA])
            gx, gy = float(centres[i][0]), float(centres[i][1])
            if area < self._cone_min_area * radius * radius:
                continue            # a friendly marker, not the cone
            if math.hypot(gx - ix, gy - iy) > self._cone_max_dist * radius:
                continue
            if best is None or area > best[0]:
                best = (area, gx, gy)
        if best is None:
            return None
        return math.degrees(math.atan2(best[1] - ix, -(best[2] - iy))) % 360.0


def fmt_fix(fix: "FullMapFix | None") -> str:
    """`east=-0.330 north=-0.685 r=0.76 cone=134` for the log, or `n/a`."""
    if fix is None:
        return "n/a"
    cone = "n/a" if fix.heading is None else f"{round(fix.heading) % 360}"
    return f"east={fix.east:+.3f} north={fix.north:+.3f} r={fix.radius:.2f} cone={cone}"

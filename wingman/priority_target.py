"""The priority target: the crown objective's yellow marker and its direction pin.

Operator, 2026-10-09: "similar to the resupply icon ... implement steering
towards prioritytarget defined by the yellow icon, where the small icon near
the center of the screen indicates direction to steer towards."

The game marks the crown objective two ways, as it does the resupply point:

- In view: a dark disc with a yellow outline and a yellow crown inside,
  ringed by four arcs with arrowheads. `find_priority_marker` finds the disc.
- Off screen: a small yellow pin on the ring the red aircraft icons use, the
  same pin as the resupply point's with a crown in it. Its place on the ring
  is the direction. `find_priority_ring_icons` finds it.

Both are told from the resupply point's by the glyph. The crown lies on the
axes of its disc, the crossed missiles reach the corners.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .icon_steering import RingIcon
from .resupply import ring_pins


@dataclass(frozen=True)
class PriorityMarker:
    x: float
    y: float
    width: int
    height: int
    area: int
    angle_deg: float


# Where a marker counts when the caller names no region: [x1, y1, x2, y2] in
# frame fractions. The pursuit passes the tracker's acquisition region.
_DEFAULT_REGION_PCT = (0.08, 0.08, 0.92, 0.92)
# The minimap draws its own copy of the crown, 20 px across like the far
# marker, so no size floor can keep it out: [x1, y1, x2, y2] in frame fractions.
_MINIMAP_PCT = (0.82, 0.0, 1.0, 0.28)
# Crown pins measured 11% to 16% of the glyph in the corners of the hole
# (2026-10-09, with the crown pin of 2026-10-02); the resupply pin 47% to 59%.
PRIORITY_PIN_MAX_CORNER_SHARE = 0.25
# The pin's outline measured 53 and 57 px against a bright sky, where the
# pointer falls out of the color band, and 101 px with it.
_PIN_MIN_AREA_PX = 45.0


def find_priority_marker(frame, region_pct=None) -> "PriorityMarker | None":
    """Find the crown objective's disc in a color capture.

    Measured on six markers, 15 to 39 px across (2026-10-09 and the crown
    icon of 2026-10-02): the outline is a bare circle that fills 12% to 19% of
    its box; the crown inside is 12% to 14% of the box, 1.3 to 1.6 times as
    wide as it is tall, a solid block that fills 63% to 80% of its own box,
    with 0% to 30% of it in the corners; the rest of the disc is dark (value
    54). The resupply icon has the same outline and fails the glyph: its
    crossed missiles are 6% to 9% of the box, as tall as they are wide, fill
    18% to 28% of their own box and put 44% or more of it in the corners (12
    icons). The four arcs around the disc are not used: far away they are
    fainter than the color band.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] < 3:
        return None
    frame_height, frame_width = frame.shape[:2]
    if frame_height < 1 or frame_width < 1:
        return None
    region_x1, region_y1, region_x2, region_y2 = (
        float(value) for value in (region_pct or _DEFAULT_REGION_PCT))

    hsv = cv2.cvtColor(np.ascontiguousarray(frame[:, :, :3]), cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array([22, 140, 190], dtype=np.uint8),
        np.array([32, 255, 255], dtype=np.uint8),
    )
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8)

    # The disc measured 15 to 39 px across on a 1920 px wide frame.
    min_size = 0.0068 * frame_width
    max_size = 0.036 * frame_width
    candidates = []
    for index in range(1, count):
        left, top, width, height, area = map(int, stats[index, :5])
        if not (min_size <= width <= max_size and min_size <= height <= max_size):
            continue
        if not 0.8 <= width / height <= 1.25 or not 0.09 <= area / (width * height) <= 0.27:
            continue
        x = left + width / 2
        y = top + height / 2
        if not (region_x1 * frame_width <= x <= region_x2 * frame_width
                and region_y1 * frame_height <= y <= region_y2 * frame_height):
            continue
        if (_MINIMAP_PCT[0] * frame_width <= x <= _MINIMAP_PCT[2] * frame_width
                and _MINIMAP_PCT[1] * frame_height <= y <= _MINIMAP_PCT[3] * frame_height):
            continue
        component = labels[top:top + height, left:left + width] == index
        rows, cols = np.mgrid[0:height, 0:width]
        dx = (cols - (width - 1) / 2) / (width / 2)
        dy = (rows - (height - 1) / 2) / (height / 2)
        radius = np.hypot(dx, dy)
        # The component must be the bare outline: no yellow of its own through
        # the middle.
        if (component & (radius < 0.62)).sum() > 0.10 * area:
            continue
        # A far outline is one pixel wide and breaks into pieces (two of the
        # six), so it is not asked to close or to be one component. The yellow
        # at its radius is asked to go all the way round.
        box_yellow = mask[top:top + height, left:left + width] > 0
        outline = box_yellow & (radius >= 0.72)
        half_w, half_h = width // 2, height // 2
        quarters = (
            outline[:half_h, :half_w].sum(),
            outline[:half_h, width - half_w:].sum(),
            outline[height - half_h:, :half_w].sum(),
            outline[height - half_h:, width - half_w:].sum(),
        )
        if min(quarters) < 0.08 * sum(quarters):
            continue
        glyph = box_yellow & ~component & (radius < 0.70)
        glyph_area = int(glyph.sum())
        if not 0.105 <= glyph_area / (width * height) <= 0.21:
            continue
        glyph_rows, glyph_cols = np.nonzero(glyph)
        glyph_w = int(glyph_cols.max() - glyph_cols.min() + 1)
        glyph_h = int(glyph_rows.max() - glyph_rows.min() + 1)
        if glyph_w < 1.15 * glyph_h or not 0.38 <= glyph_w / width <= 0.72:
            continue
        if glyph_area < 0.45 * glyph_w * glyph_h:
            continue
        # The crown sits a little below the centre of the disc.
        if abs(float(dx[glyph].mean())) > 0.2 or not -0.1 <= float(dy[glyph].mean()) <= 0.3:
            continue
        corners = (glyph & (np.abs(dx) > 0.22) & (np.abs(dy) > 0.22)).sum()
        if corners >= 0.40 * glyph_area:
            continue
        # The disc is dark wherever it is not yellow.
        dark = hsv[top:top + height, left:left + width, 2][~box_yellow & (radius < 0.62)]
        if dark.size == 0 or float(np.median(dark)) > 110:
            continue
        angle = math.degrees(math.atan2(y - frame_height / 2, x - frame_width / 2))
        candidates.append(PriorityMarker(x, y, width, height, area, angle))

    return max(candidates, key=lambda marker: marker.area, default=None)


def find_priority_ring_icons(frame, _cfg=None) -> "list[RingIcon]":
    """The crown objective's direction pin on the indicator ring, as ring icons.

    The pin is the resupply pin with a crown in place of the crossed missiles
    (`resupply.ring_pins` finds both), so the result feeds the same points
    law the red icons and the resupply pin do. The second argument is ignored;
    it gives this the signature of `find_ring_icons`.
    """
    icons = [icon for icon, corner_share in ring_pins(frame, min_area_px=_PIN_MIN_AREA_PX)
             if corner_share <= PRIORITY_PIN_MAX_CORNER_SHARE]
    icons.sort(key=lambda icon: icon.area, reverse=True)
    return icons

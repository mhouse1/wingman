"""Air superiority: the control points A, B and C the enemy holds.

Operator, 2026-10-09: "similar to prioritytarget ... implement steering
towards air superiority icons A, B, or C, if the icons are red, the small
icons indicates the direction to steer towards ... it flies towards the A mark
because it is a target on screen and closer rather than steer towards B, it
should fly through A then proceed with B."

The game marks a control point as it marks the crown objective and the
resupply point, in the color of the team that holds it:

- In view: a dark disc with an outline and its letter, ringed by four arcs
  with arrowheads. `find_control_point_marker` finds the red discs and returns
  the largest, which is the nearest.
- Off screen: a small pin on the ring the red aircraft icons use, a circle
  around the letter with a pointer. Its place on the ring is the direction.
  `find_control_point_ring_icons` finds the red pins.

Red is a point the enemy holds, and the only kind found here. Flown through,
the point turns blue, its marker and its pin stop being found, and the next
red one is what is left to steer at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .icon_steering import RingIcon
from .resupply import red_mask, ring_pins


@dataclass(frozen=True)
class ControlPointMarker:
    x: float
    y: float
    width: int
    height: int
    area: int
    angle_deg: float


# Where a marker counts when the caller names no region: [x1, y1, x2, y2] in
# frame fractions. The pursuit passes the tracker's acquisition region, which
# also keeps out the score bar's own row of circled letters.
_DEFAULT_REGION_PCT = (0.08, 0.08, 0.92, 0.92)
# The minimap draws the same circled letters: [x1, y1, x2, y2] in frame fractions.
_MINIMAP_PCT = (0.82, 0.0, 1.0, 0.28)
# The pin's outline measured 96 to 103 px (6 pins, 2026-10-09).
_PIN_MIN_AREA_PX = 60.0


def find_control_point_marker(frame, region_pct=None) -> "ControlPointMarker | None":
    """Find the nearest enemy-held control point's disc in a color capture.

    Measured on four discs, 22 to 56 px across (2026-10-09): a red circle
    whose outline fills 18% to 20% of its box, around a red letter that fills
    14% to 16% of the box and sits in its middle; the rest of the disc is
    dark. The letter can touch the outline and the crosshair can cut it, so
    neither is asked to be a component of its own: the red at the rim has to
    go all the way round, and the red inside has to be the letter's share.
    The four arcs around the disc are each one corner of a circle and fail
    the first; an aircraft icon is solid and fails the second.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] < 3:
        return None
    frame_height, frame_width = frame.shape[:2]
    if frame_height < 1 or frame_width < 1:
        return None
    region_x1, region_y1, region_x2, region_y2 = (
        float(value) for value in (region_pct or _DEFAULT_REGION_PCT))

    hsv = cv2.cvtColor(np.ascontiguousarray(frame[:, :, :3]), cv2.COLOR_BGR2HSV)
    mask = red_mask(hsv)
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8)

    # The disc measured 22 to 56 px across on a 1920 px wide frame.
    min_size = 0.0094 * frame_width
    max_size = 0.045 * frame_width
    candidates = []
    for index in range(1, count):
        left, top, width, height, area = map(int, stats[index, :5])
        if not (min_size <= width <= max_size and min_size <= height <= max_size):
            continue
        if not 0.85 <= width / height <= 1.18:
            continue
        x = left + width / 2
        y = top + height / 2
        if not (region_x1 * frame_width <= x <= region_x2 * frame_width
                and region_y1 * frame_height <= y <= region_y2 * frame_height):
            continue
        if (_MINIMAP_PCT[0] * frame_width <= x <= _MINIMAP_PCT[2] * frame_width
                and _MINIMAP_PCT[1] * frame_height <= y <= _MINIMAP_PCT[3] * frame_height):
            continue
        rows, cols = np.mgrid[0:height, 0:width]
        dx = (cols - (width - 1) / 2) / (width / 2)
        dy = (rows - (height - 1) / 2) / (height / 2)
        radius = np.hypot(dx, dy)
        box_red = mask[top:top + height, left:left + width] > 0
        # The rim: red most of the way round, in every quarter.
        rim = (radius >= 0.80) & (radius <= 1.02)
        rim_red = box_red & rim
        if rim_red.sum() < 0.45 * rim.sum():
            continue
        half_w, half_h = width // 2, height // 2
        quarters = (
            rim_red[:half_h, :half_w].sum(),
            rim_red[:half_h, width - half_w:].sum(),
            rim_red[height - half_h:, :half_w].sum(),
            rim_red[height - half_h:, width - half_w:].sum(),
        )
        if min(quarters) < 0.15 * sum(quarters):
            continue
        # A circle leaves the corners of its box empty. A squad tag's boxed
        # letter does not (one of 39 hits on the archived frames).
        corners = radius >= 1.12
        if (box_red & corners).sum() > 0.10 * corners.sum():
            continue
        # The letter: red in the middle, neither missing nor filling the disc.
        letter = box_red & (radius < 0.62)
        letter_area = int(letter.sum())
        if not 0.08 <= letter_area / (width * height) <= 0.24:
            continue
        if abs(float(dx[letter].mean())) > 0.2 or abs(float(dy[letter].mean())) > 0.2:
            continue
        # A, B and C are no wider than they are tall. The crown is, and an
        # enemy's crown is drawn in this red (one of the 39).
        letter_rows, letter_cols = np.nonzero(letter)
        if (letter_cols.max() - letter_cols.min() + 1) > 1.15 * (
                letter_rows.max() - letter_rows.min() + 1):
            continue
        # Between the rim and the letter, and in the letter's gaps, the disc
        # is dark.
        dark = hsv[top:top + height, left:left + width, 2][~box_red & (radius < 0.70)]
        if dark.size == 0 or float(np.median(dark)) > 110:
            continue
        angle = math.degrees(math.atan2(y - frame_height / 2, x - frame_width / 2))
        candidates.append(ControlPointMarker(x, y, width, height, area, angle))

    return max(candidates, key=lambda marker: marker.width * marker.height, default=None)


def find_control_point_ring_icons(frame, _cfg=None) -> "list[RingIcon]":
    """The red control-point pins on the indicator ring, as ring icons.

    The pin is the resupply pin in red with a letter in it
    (`resupply.ring_pins` finds both), so the result feeds the same points law
    the red aircraft icons do. An aircraft icon on the ring is as red, and is
    not a pin: solid ones enclose no hole and outlined ones are twice the
    size. A pin that touches an aircraft icon, or lies under another pin, is
    not found. The second argument is ignored; it gives this the signature of
    `find_ring_icons`.
    """
    icons = [icon for icon, _corner_share in ring_pins(
        frame, min_area_px=_PIN_MIN_AREA_PX, color="red")]
    icons.sort(key=lambda icon: icon.area, reverse=True)
    return icons

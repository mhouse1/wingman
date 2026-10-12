"""Yellow resupply-marker detection and per-pursuit missile urgency."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .icon_steering import RingIcon


@dataclass(frozen=True)
class ResupplyMarker:
    x: float
    y: float
    width: int
    height: int
    area: int
    angle_deg: float


@dataclass(frozen=True)
class MissilePriority:
    missiles_spent: int
    empty: bool
    rearmed: bool = False


RESUPPLY_MIN_MISSILES_SPENT = 2
RESUPPLY_MARKER_HOLD_S = 0.5
# Where a marker counts when the caller names no region: [x1, y1, x2, y2] in
# frame fractions. The pursuit passes the tracker's acquisition region.
_DEFAULT_REGION_PCT = (0.08, 0.08, 0.92, 0.92)


def find_resupply_marker(frame, region_pct=None) -> "ResupplyMarker | None":
    """Find the resupply icon's yellow disc in a color capture.

    The icon is a disc in one saturated yellow: a circular outline around a
    crossed-missiles glyph, which fills about 18% of a square box and spreads
    evenly over its four quarters. The dashed ring around it is not used: it
    grows from about 70 to 190 px as the aircraft closes, so its arcs break
    apart and none of them is centred on the icon. Terrain, exhaust flame,
    explosions and flares are a duller or redder yellow and fail the color
    band; ring arcs and the glyph alone fail the quarter balance; the
    minimap's own 20 px copies of the icon fail the size floor. Icon-colored
    things that are not the icon (exhaust behind an indicator, glare, the
    squad arrow, the crown icon) fail the outline and glyph checks (sessions
    of 2026-10-02, docs/anomaly/011).
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
        np.array([24, 140, 200], dtype=np.uint8),
        np.array([30, 255, 255], dtype=np.uint8),
    )
    count, labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8)

    # The disc measured 21 to 58 px across on a 1920 px wide frame.
    min_size = 0.0125 * frame_width
    max_size = 0.036 * frame_width
    candidates = []
    for index in range(1, count):
        left, top, width, height, area = map(int, stats[index, :5])
        if not (min_size <= width <= max_size and min_size <= height <= max_size):
            continue
        aspect = width / height
        fill = area / (width * height)
        if not 0.85 <= aspect <= 1.18 or not 0.12 <= fill <= 0.28:
            continue

        x = left + width / 2
        y = top + height / 2
        if not (region_x1 * frame_width <= x <= region_x2 * frame_width
                and region_y1 * frame_height <= y <= region_y2 * frame_height):
            continue
        component = labels[top:top + height, left:left + width] == index
        half_w, half_h = width // 2, height // 2
        quarters = (
            component[:half_h, :half_w].sum(),
            component[:half_h, width - half_w:].sum(),
            component[height - half_h:, :half_w].sum(),
            component[height - half_h:, width - half_w:].sum(),
        )
        if min(quarters) < 0.10 * sum(quarters):
            continue
        # The component must be the bare outline, and the yellow inside it the
        # crossed missiles: four arms that reach the corners. Exhaust, glare
        # and HUD arrows put yellow through the middle of the component (29%
        # or more of it, the icon 4% or less); the crown icon's glyph is
        # larger (13% of the box against 4% to 10%) and sits on the axes (9%
        # and 18% of it in the corners against 26% or more).
        rows, cols = np.mgrid[0:height, 0:width]
        dx = (cols - (width - 1) / 2) / (width / 2)
        dy = (rows - (height - 1) / 2) / (height / 2)
        radius = np.hypot(dx, dy)
        if (component & (radius < 0.62)).sum() > 0.10 * area:
            continue
        glyph = ((mask[top:top + height, left:left + width] > 0)
                 & ~component & (radius < 0.70))
        glyph_area = int(glyph.sum())
        if not 0.03 <= glyph_area / (width * height) <= 0.115:
            continue
        corners = (glyph & (np.abs(dx) > 0.22) & (np.abs(dy) > 0.22)).sum()
        if corners < 0.22 * glyph_area:
            continue
        angle = math.degrees(math.atan2(y - frame_height / 2, x - frame_width / 2))
        candidates.append(ResupplyMarker(x, y, width, height, area, angle))

    return max(candidates, key=lambda marker: marker.area, default=None)


# The direction icon's centroid measured 199 to 208 px from the centre of a
# 1920 x 1200 frame (median 203): fractions of the frame height.
_INDICATOR_RADIUS_PCT = (0.158, 0.18)


# The crossed missiles put 40% or more of their glyph in the corners of the
# pin's hole, the crown 5% to 22% (96 pins in the frames of 2026-10-02, 6 of
# them crowns; 3 more crowns on 2026-10-09 at 11% to 16%).
RESUPPLY_PIN_MIN_CORNER_SHARE = 0.30
_PIN_MIN_AREA_PX = 60.0


def red_mask(hsv):
    """The red the game draws an enemy's objectives in: hue 2 to 3 for the
    outline and the letter of a control point, measured on 4 discs and 6
    pins (2026-10-09). The dark red inside the disc (hue 175 to 179) is too dim to
    pass, as is the blue of a point the own team holds (hue 113 to 118)."""
    return (cv2.inRange(hsv, np.array([0, 110, 150], dtype=np.uint8),
                        np.array([6, 255, 255], dtype=np.uint8))
            | cv2.inRange(hsv, np.array([172, 110, 150], dtype=np.uint8),
                          np.array([180, 255, 255], dtype=np.uint8)))


def ring_pins(frame, min_area_px: float = _PIN_MIN_AREA_PX,
              color: str = "yellow") -> "list[tuple[RingIcon, float]]":
    """Pins of one color on the indicator ring, with each one's glyph corner share.

    The game draws a small pin on the ring the red aircraft icons use for an
    objective that is off screen: a circle around a glyph, with a solid
    pointer. Its place on the ring is the direction. A pin is told from
    exhaust glow and from the aircraft icons on the ring by the hole its
    circle encloses. Which objective it is, the color and the glyph say:
    yellow for the resupply point and the crown, red for a control point the
    enemy holds (`air_superiority`). The second value is the share of the
    glyph's pixels in the corners of the hole. `min_area_px` is the outline's
    area floor at 1200 px of frame height.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] < 3:
        return []
    frame_height, frame_width = frame.shape[:2]
    scale = frame_height / 1200.0
    centre_x, centre_y = frame_width / 2, frame_height / 2
    r_min = frame_height * _INDICATOR_RADIUS_PCT[0]
    r_max = frame_height * _INDICATOR_RADIUS_PCT[1]
    reach = int(math.ceil(r_max + 30 * scale))
    x1, y1 = max(0, int(centre_x) - reach), max(0, int(centre_y) - reach)
    x2 = min(frame_width, int(centre_x) + reach + 1)
    y2 = min(frame_height, int(centre_y) + reach + 1)
    if x2 <= x1 or y2 <= y1:
        return []
    hsv = cv2.cvtColor(np.ascontiguousarray(frame[y1:y2, x1:x2, :3]), cv2.COLOR_BGR2HSV)
    if color == "red":
        mask = red_mask(hsv)
    else:
        mask = cv2.inRange(
            hsv,
            np.array([22, 150, 190], dtype=np.uint8),
            np.array([32, 255, 255], dtype=np.uint8),
        )
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    pins: "list[tuple[RingIcon, float]]" = []
    for index in range(1, count):
        left, top, width, height, area = map(int, stats[index, :5])
        if not (14 * scale <= width <= 28 * scale and 14 * scale <= height <= 28 * scale
                and min_area_px * scale * scale <= area <= 170 * scale * scale):
            continue
        x = x1 + float(centroids[index][0])
        y = y1 + float(centroids[index][1])
        if not r_min <= math.hypot(x - centre_x, y - centre_y) <= r_max:
            continue
        # The inside of the circle: what a fill from outside the box cannot reach.
        component = (labels[top:top + height, left:left + width] == index).astype(np.uint8)
        padded = cv2.copyMakeBorder(component, 1, 1, 1, 1, cv2.BORDER_CONSTANT)
        cv2.floodFill(padded, None, (0, 0), 2)
        hole = padded[1:-1, 1:-1] == 0
        hole_area = int(hole.sum())
        if hole_area < 0.35 * width * height:
            continue
        # The glyph is the brighter part of the hole.
        value = hsv[top:top + height, left:left + width, 2].astype(np.int32)
        inside = value[hole]
        glyph = hole & (value >= (np.percentile(inside, 20) + np.percentile(inside, 95)) / 2)
        glyph_area = int(glyph.sum())
        if glyph_area == 0:
            continue
        hole_rows, hole_cols = np.nonzero(hole)
        rows, cols = np.mgrid[0:height, 0:width]
        dx = (cols - (hole_cols.min() + hole_cols.max()) / 2) / (
            (hole_cols.max() - hole_cols.min() + 1) / 2)
        dy = (rows - (hole_rows.min() + hole_rows.max()) / 2) / (
            (hole_rows.max() - hole_rows.min() + 1) / 2)
        corners = int((glyph & (np.abs(dx) > 0.3) & (np.abs(dy) > 0.3)).sum())
        pins.append((RingIcon(
            x, y, area, width, height,
            math.degrees(math.atan2(y - centre_y, x - centre_x)),
            int(np.median(hsv[labels == index][:, 0]))), corners / glyph_area))
    return pins


def find_resupply_ring_icons(frame, _cfg=None) -> "list[RingIcon]":
    """The resupply direction icon on the indicator ring, as ring icons.

    With the resupply point off screen the game draws a small yellow pin on
    the ring the red aircraft icons use, pointing toward it (operator,
    2026-10-02): a circle around the crossed missiles with a solid pointer.
    Its place on the ring is the direction, as for the red icons, so the
    result feeds the same points law. `ring_pins` finds the pins; this keeps
    the ones whose glyph is the crossed missiles, which leaves out the crown
    pin of the priority target (`priority_target.find_priority_ring_icons`).
    The second argument is ignored; it gives this the signature of
    `find_ring_icons`.
    """
    icons = [icon for icon, corner_share in ring_pins(frame)
             if corner_share >= RESUPPLY_PIN_MIN_CORNER_SHARE]
    icons.sort(key=lambda icon: icon.area, reverse=True)
    return icons


class ResupplyMarkerMemory:
    """Hold a detected marker target briefly across scan dropouts."""

    def __init__(self, hold_s: float = RESUPPLY_MARKER_HOLD_S) -> None:
        self._hold_s = max(0.0, float(hold_s))
        self._marker: "ResupplyMarker | None" = None
        self._seen_at: "float | None" = None

    def resolve(self, marker: "ResupplyMarker | None", now: float, *,
                seeking: bool) -> tuple["ResupplyMarker | None", bool]:
        if marker is not None:
            self._marker = marker
            self._seen_at = now
            return marker, False
        if (seeking and self._marker is not None and self._seen_at is not None
                and now - self._seen_at <= self._hold_s):
            return self._marker, True
        return None, False

    def clear(self) -> None:
        self._marker = None
        self._seen_at = None


class MissileUrgency:
    """Confirm ammo changes and retain spent-missile urgency for one pursuit."""

    def __init__(self, confirm_reads: int = 3) -> None:
        self._confirm_reads = max(1, int(confirm_reads))
        self._rack = None
        self._candidate: "int | None" = None
        self._candidate_reads = 0
        self._read_id: "int | None" = None
        self._ammo_by_rack: dict[object, int] = {}
        self._last_positive: dict[object, int] = {}
        self._missiles_spent = 0
        self._empty = False

    def observe(self, ammo: "int | None", rack: object, *,
                terminal_zero: bool = False,
                resupply_seeking: bool = False,
                read_id: "int | None" = None) -> MissilePriority:
        """Record only stable counts; a rack change does not clear urgency.

        `read_id` identifies the OCR read `ammo` came from. A count seen again
        under the same id is the same read polled twice and does not advance
        the confirmation run; without an id every call counts as a read.

        `terminal_zero` is false for the first rack while a deferred switch is
        pending and during the post-switch HUD grace period. A stable ammo
        increase while actively seeking resupply represents a rearm and starts
        a new priority cycle without requiring a prior zero.
        """
        rearmed = False
        if rack != self._rack:
            self._rack = rack
            self._candidate = None
            self._candidate_reads = 0
        if ammo is None or int(ammo) < 0:
            self._candidate = None
            self._candidate_reads = 0
            return self.snapshot()

        ammo = int(ammo)
        if ammo > 0:
            self._last_positive[rack] = ammo
        new_read = read_id is None or read_id != self._read_id
        self._read_id = read_id
        if ammo != self._candidate:
            self._candidate = ammo
            self._candidate_reads = 1
        elif new_read:
            self._candidate_reads += 1
        if self._candidate_reads < self._confirm_reads:
            return self.snapshot()

        if ammo == 0 and terminal_zero:
            self._empty = True

        previous = self._ammo_by_rack.get(rack)
        if ammo > 0 and (self._empty or (resupply_seeking and previous is not None
                                         and ammo > previous)):
            self._missiles_spent = 0
            self._empty = False
            self._ammo_by_rack.clear()
            self._ammo_by_rack[rack] = ammo
            self._last_positive = {rack: ammo}
            rearmed = True
        elif previous is None:
            self._ammo_by_rack[rack] = ammo
        elif ammo < previous:
            self._missiles_spent += previous - ammo
            self._ammo_by_rack[rack] = ammo
        # An increase without an active resupply seek is treated as OCR noise.
        return self.snapshot(rearmed=rearmed)

    def rack_emptied(self, rack: object) -> MissilePriority:
        """The caller confirmed `rack` empty and is switching away from it.

        The pursuit switches racks after a second of zero readings, before
        `observe` has seen the three OCR reads it needs, so the rack's last
        missiles would never be counted. Credit what the rack still held: its
        confirmed count, or the last positive count read from it when none
        was confirmed.
        """
        remaining = self._ammo_by_rack.get(rack)
        if remaining is None:
            remaining = self._last_positive.get(rack, 0)
        self._missiles_spent += max(0, remaining)
        self._ammo_by_rack[rack] = 0
        self._last_positive.pop(rack, None)
        return self.snapshot()

    def snapshot(self, *, rearmed: bool = False) -> MissilePriority:
        return MissilePriority(self._missiles_spent, self._empty, rearmed)


def resupply_preempts(*, priority: MissilePriority,
                      marker_visible: bool) -> bool:
    """Whether a visible marker takes priority after two confirmed missiles."""
    if not marker_visible:
        return False
    return (priority.empty
            or priority.missiles_spent >= RESUPPLY_MIN_MISSILES_SPENT)


def marker_nearer_than_target(marker: ResupplyMarker, target_error_x: "float | None",
                              target_error_y: "float | None",
                              frame_width: int, frame_height: int) -> bool:
    """Whether the marker is nearer the screen centre than the visible target.

    While weapons remain, the pursuit goes for whichever of the two is nearer
    the centre (operator, 2026-10-02). The target errors are the tracker's,
    in half-frame units; a target with no error on an axis counts as centred
    on it, and no target at all leaves the marker nearer.
    """
    if target_error_x is None and target_error_y is None:
        return True
    marker_px = math.hypot(marker.x - frame_width / 2, marker.y - frame_height / 2)
    target_px = math.hypot((target_error_x or 0.0) * frame_width / 2,
                           (target_error_y or 0.0) * frame_height / 2)
    return marker_px <= target_px

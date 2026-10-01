"""Yellow resupply-marker detection and per-pursuit missile urgency."""

from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np


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


def find_resupply_marker(frame) -> "ResupplyMarker | None":
    """Find the sparse, near-square yellow marker in a color capture.

    The two operator captures place the marker at different screen positions;
    its color and grouped component shape, not a fixed crop or the enemy ring,
    identify it. Dense yellow scene objects and small HUD glyphs are rejected.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] < 3:
        return None
    frame_height, frame_width = frame.shape[:2]
    if frame_height < 1 or frame_width < 1:
        return None

    hsv = cv2.cvtColor(np.ascontiguousarray(frame[:, :, :3]), cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(
        hsv,
        np.array([18, 100, 100], dtype=np.uint8),
        np.array([42, 255, 255], dtype=np.uint8),
    )
    kernel_size = max(3, int(round(frame_height * 0.006)))
    if kernel_size % 2 == 0:
        kernel_size += 1
    closed = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, np.ones((kernel_size, kernel_size), dtype=np.uint8))
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(
        closed, connectivity=8)

    min_area = int(frame_width * frame_height * 0.0002)
    max_area = int(frame_width * frame_height * 0.003)
    candidates = []
    for index in range(1, count):
        _left, _top, width, height, area = map(int, stats[index, :5])
        if not min_area <= area <= max_area:
            continue
        if not (0.03 * frame_width <= width <= 0.08 * frame_width
                and 0.045 * frame_height <= height <= 0.11 * frame_height):
            continue
        aspect = width / height
        fill = area / (width * height)
        if not 0.65 <= aspect <= 1.35 or not 0.12 <= fill <= 0.42:
            continue

        x, y = map(float, centroids[index])
        if not (0.08 * frame_width <= x <= 0.92 * frame_width
                and 0.08 * frame_height <= y <= 0.92 * frame_height):
            continue
        angle = math.degrees(math.atan2(y - frame_height / 2, x - frame_width / 2))
        candidates.append(ResupplyMarker(x, y, width, height, area, angle))

    return max(candidates, key=lambda marker: marker.area, default=None)


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
        self._ammo_by_rack: dict[object, int] = {}
        self._missiles_spent = 0
        self._empty = False

    def observe(self, ammo: "int | None", rack: object, *,
                terminal_zero: bool = False,
                resupply_seeking: bool = False) -> MissilePriority:
        """Record only stable counts; a rack change does not clear urgency.

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
        if ammo == self._candidate:
            self._candidate_reads += 1
        else:
            self._candidate = ammo
            self._candidate_reads = 1
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
            rearmed = True
        elif previous is None:
            self._ammo_by_rack[rack] = ammo
        elif ammo < previous:
            self._missiles_spent += previous - ammo
            self._ammo_by_rack[rack] = ammo
        # An increase without an active resupply seek is treated as OCR noise.
        return self.snapshot(rearmed=rearmed)

    def snapshot(self, *, rearmed: bool = False) -> MissilePriority:
        return MissilePriority(self._missiles_spent, self._empty, rearmed)


def resupply_preempts(*, priority: MissilePriority,
                      marker_visible: bool) -> bool:
    """Whether a visible marker takes priority after two confirmed missiles."""
    if not marker_visible:
        return False
    return (priority.empty
            or priority.missiles_spent >= RESUPPLY_MIN_MISSILES_SPENT)

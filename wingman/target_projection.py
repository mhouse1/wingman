"""Where the tracked target is going (Design 021 Phase 5, shadow).

Operator, 2026-11-13: "we can see the rate at which a target is moving and
project where it will be in the future". The projected point is where the gun
would have to aim for the bullets to meet the target: the game draws that point
itself, as the lead ring, but only at about 1.1 km and under.

The marker's speed across the screen says almost nothing by itself, because the
marker moves mostly as wingman turns (630 ring sightings, 2026-11-13: a median
miss of 134 px, the same as no lead). With the travel the chase's own keys
account for added back, smoothed, and multiplied by the range, the miss is 71 px
and the side the target is going is right 89% of the time. That is good enough
to show and to turn on, not to fire on.

This module only says where the point is. Nothing flies on it: the chase logs it
and the HUD draws it. No I/O and no clock: every time is passed in.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# The frame height the pixel sizes below were measured at.
_REF_H = 1200.0
# Two pictures further apart than this are two looks, not one motion; closer
# than _MIN_GAP_S the difference is pixel noise.
_MAX_GAP_S = 0.6
_MIN_GAP_S = 0.05
# A marker that moves faster than this between two pictures is a lock that
# jumped to another aircraft, px a second at 1200 high.
_MAX_SPEED_PX_S = 1500.0
# The longest lead drawn, px at 1200 high.
_MAX_LEAD_PX = 600.0


@dataclass(frozen=True)
class Projection:
    x: float            # the projected point, px
    y: float
    vx: float           # the target's own motion across the screen, px a second,
    vy: float           # with wingman's turning taken out
    km: float

    @property
    def speed(self) -> float:
        return math.hypot(self.vx, self.vy)


class TargetProjection:
    """Follows the tracked marker from picture to picture.

    `lead_s_per_km`: seconds of the target's motion the point leads by, for each
    km of range, per axis. `offset_px`: where the point sits with the target
    still, from the marker. `smooth_s`: the motion is averaged over about this
    long. All fitted on the ring sightings of 2026-11-13 (Design 021)."""

    def __init__(self, lead_s_per_km, offset_px, smooth_s: float) -> None:
        self.lead_s_per_km = (float(lead_s_per_km[0]), float(lead_s_per_km[1]))
        self.offset_px = (float(offset_px[0]), float(offset_px[1]))
        self.smooth_s = max(1e-3, float(smooth_s))
        self._last: "tuple[float, float, float] | None" = None      # ts, x, y
        self._v: "tuple[float, float] | None" = None
        self._latest: "Projection | None" = None

    def reset(self) -> None:
        """The target is gone, or another one took the lock."""
        self._last = self._v = self._latest = None

    def update(self, marker: "tuple[float, float]", ts: float, km: "float | None",
               frame_height: float, own_travel_px) -> "Projection | None":
        """The projected point for the picture first grabbed at `ts`, or None
        until two pictures of the target have been seen, or without a range.

        `own_travel_px(t0, t1)` is how far, in px on each axis, the chase's own
        keys moved the marker between two pictures: a key held toward the target
        brings it to the centre, and that travel is not the target's."""
        scale = frame_height / _REF_H
        prev = self._last
        if prev is not None and ts <= prev[0]:
            return self._latest                     # the same picture again
        self._last = (ts, float(marker[0]), float(marker[1]))
        if prev is None:
            return None
        gap = ts - prev[0]
        if not _MIN_GAP_S <= gap <= _MAX_GAP_S:
            self._v = self._latest = None
            return None
        sx, sy = (marker[0] - prev[1]) / gap, (marker[1] - prev[2]) / gap
        if math.hypot(sx, sy) > _MAX_SPEED_PX_S * scale:
            self._v = self._latest = None
            return None
        ox, oy = own_travel_px(prev[0], ts)
        cur = (sx + ox / gap, sy + oy / gap)
        if self._v is None:
            self._v = cur
        else:
            w = 1.0 - math.exp(-gap / self.smooth_s)
            self._v = (self._v[0] + w * (cur[0] - self._v[0]),
                       self._v[1] + w * (cur[1] - self._v[1]))
        if km is None:
            self._latest = None
            return None
        lx = self.lead_s_per_km[0] * self._v[0] * km
        ly = self.lead_s_per_km[1] * self._v[1] * km
        length = math.hypot(lx, ly)
        longest = _MAX_LEAD_PX * scale
        if length > longest:
            lx, ly = lx * longest / length, ly * longest / length
        self._latest = Projection(
            x=marker[0] + self.offset_px[0] * scale + lx,
            y=marker[1] + self.offset_px[1] * scale + ly,
            vx=self._v[0], vy=self._v[1], km=float(km))
        return self._latest

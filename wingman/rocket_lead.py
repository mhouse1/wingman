"""The lead diamond of a weapon that is fired by holding `f` (ADR 180).

Operator, 2026-11-15 14:08, on the F-100's LAU-3 Hydra Rocket Pod: "fire rockets
by holding down the 'f' FIRE_ACTIVE_WEAPON key". The pursuit taps `f` about every
0.35 s for missiles; with the pod selected that emptied it, 38 rockets, in about
23 s, fired at nothing in particular.

Which weapon is selected is read from its name (weapon_name.py). This module
finds the weapon's lead: a green diamond with a tick at each corner, 57 px across
the ticks (38 px the body) at 1200 px high, drawn ahead of the target on the
dotted line that also carries the gun's lead ring. Measured on the operator's
`v` screenshots of 14:08:36 and 14:08:47: the same size at 1.0 km and at about
2 km, so one template finds it. On the two frames it scored 0.66 and 0.73; the
best other match in eight recent frames was 0.55, the crosshair over a tilted,
tickless missile box with the AIM-9M selected.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_REF_H = 1200.0
# The diamond at 1200 px high: half the body across, the stroke, the ticks' length.
_BODY_R = 19
_STROKE = 3
_TICK = 9
# HUD green, as the gun lead ring's dots (lead_ring.py).
_GREEN_LO, _GREEN_HI = (38, 40, 150), (75, 255, 255)
# The bottom of the picture is the weapon panel, whose own small diamond is not a lead.
_SEARCH_MAX_Y = 0.88
# Two pictures within this distance confirm a diamond; it moves with the target.
_CONFIRM_PX = 160.0


@dataclass(frozen=True)
class Diamond:
    x: float
    y: float
    score: float


def _green(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(np.ascontiguousarray(bgr), cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, _GREEN_LO, _GREEN_HI)


def diamond_template(scale: float = 1.0) -> np.ndarray:
    """The lead diamond's outline and ticks as a float mask, at `scale` of 1200 px."""
    r = max(4, int(round(_BODY_R * scale)))
    t = max(1, int(round(_STROKE * scale)))
    tick = max(2, int(round(_TICK * scale)))
    n = 2 * (r + tick) + 1
    c = n // 2
    m = np.zeros((n, n), np.float32)
    pts = np.array([[c, c - r], [c + r, c], [c, c + r], [c - r, c]], np.int32)
    cv2.polylines(m, [pts], True, 1.0, t)
    for (x0, y0), (x1, y1) in (((c, c - r - tick), (c, c - r)), ((c, c + r), (c, c + r + tick)),
                               ((c - r - tick, c), (c - r, c)), ((c + r, c), (c + r + tick, c))):
        cv2.line(m, (x0, y0), (x1, y1), 1.0, t)
    return m


def find_diamonds(frame: np.ndarray, around: "tuple[float, float]", search_px: float,
                  min_score: float, scale: "float | None" = None,
                  most: int = 3) -> "list[Diamond]":
    """Lead diamonds within `search_px` (at 1200 high) of `around`, best first."""
    h, w = frame.shape[:2]
    s = h / _REF_H if scale is None else scale
    tmpl = diamond_template(s)
    half = tmpl.shape[0] // 2
    r = search_px * s + half
    xa, xb = max(0, int(around[0] - r)), min(w, int(around[0] + r))
    ya, yb = max(0, int(around[1] - r)), min(int(h * _SEARCH_MAX_Y), int(around[1] + r))
    if xb - xa <= tmpl.shape[1] or yb - ya <= tmpl.shape[0]:
        return []
    mask = (_green(frame[ya:yb, xa:xb]) > 0).astype(np.float32)
    if not mask.any():
        return []
    res = cv2.matchTemplate(mask, tmpl, cv2.TM_CCOEFF_NORMED)
    out: "list[Diamond]" = []
    for _ in range(most):
        _, score, _, loc = cv2.minMaxLoc(res)
        if not math.isfinite(score) or score < min_score:
            break
        out.append(Diamond(xa + loc[0] + half, ya + loc[1] + half, float(score)))
        cv2.circle(res, loc, int(2 * half), -1.0, -1)
    return out


class RocketLead:
    """Where a confirmed lead diamond is, asked by the pursuit on each picture.
    Used on one steering thread at a time."""

    def __init__(self, min_score: float, search_px: float, memory_s: float):
        self._min_score = float(min_score)
        self._search_px = float(search_px)
        self._memory_s = float(memory_s)
        self._last: "tuple[float, Diamond] | None" = None     # the last picture's best
        self._confirmed: "tuple[float, Diamond] | None" = None
        self._seen_ts: "float | None" = None

    def observe(self, frame, around: "tuple[float, float]",
                picture_ts: float) -> "Diamond | None":
        """Look for the diamond near `around` (the tracked marker) on a new
        picture; returns it once two pictures in a row agree. A repeated picture
        changes nothing."""
        if picture_ts == self._seen_ts:
            return self.aim(picture_ts)
        self._seen_ts = picture_ts
        found = find_diamonds(frame, around, self._search_px, self._min_score)
        best = min(found, key=lambda d: math.hypot(d.x - around[0], d.y - around[1]),
                   default=None)
        if best is not None and self._last is not None:
            prev = self._last[1]
            if math.hypot(best.x - prev.x, best.y - prev.y) <= _CONFIRM_PX * frame.shape[0] / _REF_H:
                self._confirmed = (picture_ts, best)
        self._last = (picture_ts, best) if best is not None else None
        return self.aim(picture_ts)

    def aim(self, picture_ts: float) -> "Diamond | None":
        """The confirmed diamond, while it was confirmed within memory_s."""
        if self._confirmed is None or picture_ts - self._confirmed[0] > self._memory_s:
            return None
        return self._confirmed[1]

    def reset(self) -> None:
        self._last = self._confirmed = None
        self._seen_ts = None

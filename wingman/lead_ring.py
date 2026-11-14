"""The gun lead ring, Design 021 Phase 1 (shadow).

When an enemy is close the game draws a small ring ahead of it: where the gun
must point for the bullets to meet the target. The operator flies the crosshair
onto it and fires (2026-11-13: a median 30 px from the crosshair in eight firing
frames, while the target itself was 72 to 331 px away). It is 26 px across at
1200 px high, with four ears on the diagonals, drawn red, tan or green, and four
green dots run from it to the target aircraft.

Here the ring is found in a window around the tracked marker and its position
logged (Phase 1). In Phase 3 a share of rounds (`RingAimTrial`) steer the
crosshair onto it and fire on it; the rest steer at the marker as before.
"""

import logging
import math
import random
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .suppressed import log_suppressed

logger = logging.getLogger(__name__)

# The frame height the pixel sizes below were measured at.
_REF_H = 1200.0
_RING_D = 26          # outer diameter, px
_RING_T = 3           # stroke, px
_EAR_OFF = 9          # ear centre offset from the ring centre on each axis, px

# HSV ranges of the three ring colours seen (Design 021, 12 frames). One mask
# takes all three; the colour is named afterwards.
_COLOURS = {
    "red": (((0, 120, 180), (8, 255, 255)), ((174, 120, 180), (180, 255, 255))),
    "green": (((38, 40, 150), (75, 255, 255)),),
    "tan": (((8, 60, 90), (24, 200, 200)),),
}
# The dotted line's dots: the HUD's light green.
_DOT_LO, _DOT_HI = (38, 40, 150), (75, 255, 255)

# Two pictures in a row within this distance make a ring count.
_CONFIRM_PX = 80.0
# Rev 3 (Phase 3, 2026-11-13 20:50): or within this, when both pictures found at
# least _DOTTED_MIN dots on the line to the marker. At close range the ring moves
# fast: of 38 such pairs under 1.2 km, 18 moved 80 px or less, 15 moved 80 to
# 160 px and 5 more, so the 80 px rule dropped half of them and the chase's aim
# fell back to the marker between pictures.
_CONFIRM_DOTTED_PX = 160.0
_DOTTED_MIN = 2
# Range bands for the session summary, km.
_BANDS = ((0.0, 1.0), (1.0, 1.2), (1.2, 2.0))
_CROP_DIR = Path("test_screenshots/unknown_anomalies")
_FOUND_CROPS_PER_SESSION = 30
_MISS_CROPS_PER_SESSION = 15
# A miss crop only where the ring is expected (Design 021: it showed in 12 of 13
# frames at 1.1 km and under).
_MISS_CROP_MAX_KM = 1.0


@dataclass(frozen=True)
class LeadRing:
    x: float
    y: float
    score: float
    colour: str


def ring_template(scale: float = 1.0) -> np.ndarray:
    """The ring's shape as a 0/255 mask: a circle and four diagonal ears."""
    d = max(8, int(round(_RING_D * scale)))
    pad = max(3, int(round(3 * scale)))
    size = d + 2 * pad
    m = np.zeros((size, size), np.uint8)
    c = size // 2
    t = max(1, int(round(_RING_T * scale)))
    cv2.circle(m, (c, c), d // 2 - max(1, int(round(2 * scale))), 255, t)
    e = int(round(_EAR_OFF * scale))
    r = max(1, int(round(2 * scale)))
    for sx in (-1, 1):
        for sy in (-1, 1):
            cv2.circle(m, (c + sx * e, c + sy * e), r, 255, -1)
    return m


def _colour_mask(hsv: np.ndarray, name: str) -> np.ndarray:
    out = None
    for lo, hi in _COLOURS[name]:
        m = cv2.inRange(hsv, lo, hi)
        out = m if out is None else out | m
    return out


def crosshair(shape, pct) -> "tuple[float, float]":
    """The crosshair's centre in px for a frame of `shape`."""
    h, w = shape[:2]
    return float(pct[0]) * w, float(pct[1]) * h


def find_ring(frame: np.ndarray, marker: "tuple[float, float]", search_px: float,
              min_score: float, scale: "float | None" = None) -> "LeadRing | None":
    """The best ring match within `search_px` (at 1200 px high) of `marker`,
    or None when nothing scores `min_score`. `scale` is the game frame's
    height over 1200; it defaults to `frame`'s, so pass it for a crop."""
    h, w = frame.shape[:2]
    scale = h / _REF_H if scale is None else scale
    half = search_px * scale
    x0 = int(max(0, marker[0] - half))
    y0 = int(max(0, marker[1] - half * 700.0 / 900.0))
    x1 = int(min(w, marker[0] + half))
    y1 = int(min(h, marker[1] + half * 700.0 / 900.0))
    tpl = ring_template(scale)
    if x1 - x0 < tpl.shape[1] or y1 - y0 < tpl.shape[0]:
        return None
    hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
    # One match per colour, the best kept. A mask of all three colours at once
    # lost the ring where the green crosshair arms or the red name text cross it
    # (2026-11-13 frames 112721 and 112722: no match at 0.5).
    best = None
    for name in _COLOURS:
        mask = _colour_mask(hsv, name)
        if not mask.any():
            continue
        res = cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(res)
        if np.isfinite(score) and (best is None or score > best[0]):
            best = (float(score), loc, name)
    if best is None or best[0] < min_score:
        return None
    score, loc, colour = best
    cx = loc[0] + tpl.shape[1] // 2
    cy = loc[1] + tpl.shape[0] // 2
    return LeadRing(x=float(x0 + cx), y=float(y0 + cy), score=score, colour=colour)


def count_dots(frame: np.ndarray, ring: LeadRing, marker: "tuple[float, float]",
               scale: "float | None" = None) -> int:
    """How many of the four dotted-line dots, at fifths of the line from the
    ring to `marker`, have HUD green near them. The marker is where wingman
    thinks the aircraft is, not where the game draws the line's far end, so the
    neighbourhood grows with the line's length."""
    h, w = frame.shape[:2]
    scale = h / _REF_H if scale is None else scale
    dx, dy = marker[0] - ring.x, marker[1] - ring.y
    length = math.hypot(dx, dy)
    if length < 30 * scale:
        return 0
    r = int(round(6 * scale + 0.1 * length))
    found = 0
    for t in (0.2, 0.4, 0.6, 0.8):
        px, py = int(ring.x + t * dx), int(ring.y + t * dy)
        x0, y0 = max(0, px - r), max(0, py - r)
        x1, y1 = min(w, px + r + 1), min(h, py + r + 1)
        if x1 <= x0 or y1 <= y0:
            continue
        hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        if cv2.inRange(hsv, _DOT_LO, _DOT_HI).any():
            found += 1
    return found


class LeadRingWatch:
    """Finds the ring on each pursuit steering tick and logs it (shadow).

    A ring counts (`confirmed`) once seen in two pictures in a row within
    80 px; a repeated picture (the same picture time) is not a second one."""

    def __init__(self, search_px: float, min_score: float, crosshair_pct) -> None:
        self.search_px = float(search_px)
        self.min_score = float(min_score)
        self.crosshair_pct = (float(crosshair_pct[0]), float(crosshair_pct[1]))
        self._last_ts: "float | None" = None
        self._last_ring: "LeadRing | None" = None
        self._last_dots = 0
        self._last_confirmed = False
        self._was_confirmed = False
        self.latest: "LeadRing | None" = None      # the newest confirmed ring
        self.latest_ts: "float | None" = None
        self.latest_dots = 0                        # its dots on the line to the marker
        self._found_crops = 0
        self._miss_crops = 0
        # Session tallies, by range band: pictures searched, rings found, confirmed.
        self.searched = [0] * len(_BANDS)
        self.found = [0] * len(_BANDS)
        self.confirmed = [0] * len(_BANDS)
        self.colours: "dict[str, int]" = {}

    def observe(self, frame: np.ndarray, marker: "tuple[float, float]", km: float,
                ts: float, gun_held: bool) -> "LeadRing | None":
        """Search this picture; log a `LEADRING:` line; return the ring if it
        counts. A repeated picture is not searched again."""
        if ts == self._last_ts:
            return self.latest if self._last_confirmed else None
        started = time.perf_counter()
        try:
            ring = find_ring(frame, marker, self.search_px, self.min_score)
            dots = count_dots(frame, ring, marker) if ring is not None else 0
        except Exception as exc:
            log_suppressed(logger, "lead_ring_find", exc)
            return None
        cost_ms = (time.perf_counter() - started) * 1000.0
        scale = frame.shape[0] / _REF_H
        reach = (_CONFIRM_DOTTED_PX if dots >= _DOTTED_MIN and self._last_dots >= _DOTTED_MIN
                 else _CONFIRM_PX)
        confirmed = (ring is not None and self._last_ring is not None
                     and math.hypot(ring.x - self._last_ring.x,
                                    ring.y - self._last_ring.y) <= reach * scale)
        self._last_ts = ts
        self._last_ring = ring
        self._last_dots = dots
        self._last_confirmed = confirmed
        band = next((i for i, (lo, hi) in enumerate(_BANDS) if lo <= km < hi), None)
        if band is not None:
            self.searched[band] += 1
            self.found[band] += ring is not None
            self.confirmed[band] += confirmed
        cx, cy = crosshair(frame.shape, self.crosshair_pct)
        if ring is None:
            logger.debug("LEADRING: none km=%.2f marker=(%.0f,%.0f) gun=%s cost=%.0fms",
                         km, marker[0], marker[1], "fire" if gun_held else "-", cost_ms)
            if km < _MISS_CROP_MAX_KM:
                self._save(frame, marker, None, km, miss=True)
        else:
            self.colours[ring.colour] = self.colours.get(ring.colour, 0) + 1
            logger.debug(
                "LEADRING: %s at=(%.0f,%.0f) %s score=%.2f km=%.2f cross_px=%.0f "
                "marker_px=%.0f dots=%d gun=%s cost=%.0fms",
                "confirmed" if confirmed else "seen", ring.x, ring.y, ring.colour,
                ring.score, km, math.hypot(ring.x - cx, ring.y - cy),
                math.hypot(ring.x - marker[0], ring.y - marker[1]), dots,
                "fire" if gun_held else "-", cost_ms)
            if confirmed and not self._was_confirmed:
                self._save(frame, marker, ring, km, miss=False)
        if confirmed:
            self.latest, self.latest_ts, self.latest_dots = ring, ts, dots
        self._was_confirmed = confirmed
        return ring if confirmed else None

    def seen_at(self, ts: float) -> "LeadRing | None":
        """The ring found on the picture of time `ts`, confirmed or not."""
        return self._last_ring if ts == self._last_ts else None

    def aim_ring(self, ts: float, memory_s: float, min_dots: int) -> "LeadRing | None":
        """The ring to steer at for the picture of time `ts`: the newest
        confirmed one, if it was confirmed within `memory_s` and at least
        `min_dots` of the dots ran from it to the tracked marker. The memory
        carries the aim over a picture the ring was missed on, so the aim does
        not swing back to the marker and out again (the two were up to 307 px
        apart in the operator's frames)."""
        if self.latest is None or self.latest_ts is None:
            return None
        if ts - self.latest_ts > memory_s or self.latest_dots < min_dots:
            return None
        return self.latest

    def cross_px(self, frame_shape, ring: LeadRing) -> float:
        cx, cy = crosshair(frame_shape, self.crosshair_pct)
        return math.hypot(ring.x - cx, ring.y - cy)

    def _save(self, frame, marker, ring, km, miss: bool) -> None:
        """A crop for checking by eye: the first picture of each confirmed ring,
        and pictures under 1 km with none. Capped per session."""
        if miss:
            if self._miss_crops >= _MISS_CROPS_PER_SESSION:
                return
            self._miss_crops += 1
        else:
            if self._found_crops >= _FOUND_CROPS_PER_SESSION:
                return
            self._found_crops += 1
        try:
            h, w = frame.shape[:2]
            cx, cy = (marker if ring is None else
                      ((ring.x + marker[0]) / 2.0, (ring.y + marker[1]) / 2.0))
            x0 = int(min(max(0, cx - 320), max(0, w - 640)))
            y0 = int(min(max(0, cy - 240), max(0, h - 480)))
            _CROP_DIR.mkdir(parents=True, exist_ok=True)
            name = "leadring_%s%s_%.2fkm_x%d_y%d.png" % (
                "miss_" if miss else "", time.strftime("%Y%m%d_%H%M%S"), km, x0, y0)
            cv2.imwrite(str(_CROP_DIR / name), frame[y0:y0 + 480, x0:x0 + 640])
        except Exception as exc:
            log_suppressed(logger, "lead_ring_save", exc)

    def summary(self) -> str:
        parts = []
        for (lo, hi), s, f, c in zip(_BANDS, self.searched, self.found, self.confirmed, strict=True):
            parts.append("%.1f-%.1fkm %d/%d found, %d confirmed" % (lo, hi, f, s, c))
        cols = ", ".join("%s %d" % kv for kv in sorted(self.colours.items())) or "none"
        return "; ".join(parts) + "; colours: " + cols


class RingAimTrial:
    """Design 021 Phase 3: this round's arm. `ring` (with probability `share`)
    steers the crosshair onto the lead ring and fires on it; `marker` steers at
    the tracked marker and fires on the marker rule, as before. Drawn once a
    round at the round start (main loop); read on the pursuit thread."""

    def __init__(self, share: float, rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"steer_share {share!r} is outside 0 to 1")
        self.share = float(share)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the trial is off."""
        if not self.enabled:
            self.arm = None
            return None
        self.arm = "ring" if self._rng.random() < self.share else "marker"
        self.rounds += 1
        return self.arm

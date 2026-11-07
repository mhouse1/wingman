"""The special weapon's HUD icon, and when to spend the weapon (ADR 173).

Every jet that has a special weapon draws one icon above the `Q` key cap, left of the
speed and altitude readout. Its states, from live MiG-23 frames on 2026-11-07:

- ready: a thin white ring, all the way round, around the jet's own glyph;
- active, just after a press: the glyph with the ring draining as an arc, for about
  3.5 s;
- cooldown: a grey disc with the seconds left (60 down to 0);
- not unlocked: nothing at all, not even the key cap.

Only the full ring means a press will be taken, so the reader answers one question:
is the ring lit all the way round? The glyph differs per jet and the ring does not,
so the reader looks at the ring only. The disc behind it is translucent, so its
inside says nothing on its own. A sector of the ring counts as lit when the ring's
brightest pixels there are white and stand above both the disc just inside the ring
and the background just outside it. A bright cloud behind the icon lifts the
outside too, so the outside margin is small and the inside margin large.

`SpecialWeaponPresser` decides when the press goes out. It has no I/O and takes the
clock as an argument, so its rules are tested without a game.
"""

from __future__ import annotations

import logging
import math
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

# Bands as fractions of the ring's radius, measured on the 1920x1200 frames of
# 2026-11-07 (ring centre 570.5, 717.2 px, radius 34.4 px) and the 960x600 path
# black-box frames of the same session.
_RING_BAND = (0.85, 1.15)
_INSIDE_BAND = (0.55, 0.78)
_OUTSIDE_BAND = (1.22, 1.45)

TRIGGERS = ("incoming", "low_flares", "ready")


def _setting(cfg: dict, key: str):
    """`special_weapon.<key>` from the config, or the schema's one default (CR-018-16)."""
    from .config_schema import schema_default

    value = cfg.get(key)
    return schema_default(f"special_weapon.{key}") if value is None else value


class RingGeometry:
    """Per-sector pixel indices for one frame size, built once and reused."""

    def __init__(self, crop_box, frame_shape, sectors: int):
        h, w = frame_shape[:2]
        x1, y1, x2, y2 = crop_box[:4]
        cx = (x1 + x2) / 2.0 * w
        cy = (y1 + y2) / 2.0 * h
        # The crop hugs the ring, so its half-height is the ring's radius; the
        # height, not the width, so a frame with another aspect keeps a circle.
        r = (y2 - y1) / 2.0 * h
        self.radius = r
        half = int(math.ceil(r * _OUTSIDE_BAND[1])) + 1
        self.x0 = int(round(cx)) - half
        self.y0 = int(round(cy)) - half
        self.size = 2 * half
        self.valid = (r >= 6 and self.x0 >= 0 and self.y0 >= 0
                      and self.x0 + self.size <= w and self.y0 + self.size <= h)
        yy, xx = np.mgrid[0:self.size, 0:self.size]
        dy = yy - (cy - self.y0)
        dx = xx - (cx - self.x0)
        d = np.hypot(dy, dx) / max(r, 1e-6)
        sector = (((np.arctan2(dy, dx) + np.pi) / (2 * np.pi) * sectors).astype(int)
                  % sectors)
        self.ring, self.inside, self.outside = [], [], []
        for k in range(sectors):
            sk = sector == k
            self.ring.append(np.flatnonzero(sk & (d >= _RING_BAND[0]) & (d <= _RING_BAND[1])))
            self.inside.append(np.flatnonzero(sk & (d >= _INSIDE_BAND[0]) & (d < _INSIDE_BAND[1])))
            self.outside.append(np.flatnonzero(sk & (d > _OUTSIDE_BAND[0]) & (d <= _OUTSIDE_BAND[1])))


# A sector whose ring pixels are mostly strongly coloured is covered by a HUD
# overlay (2026-11-07: both misreads caught on camera were a yellow target marker
# across the ring), not unlit. The ring itself is white.
_COVERED_MIN_SATURATION = 120
_COVERED_MIN_VALUE = 120
_COVERED_FRACTION = 0.3


def lit_sectors(frame, geometry: RingGeometry, **thresholds) -> int:
    """How many of the ring's sectors are lit in this BGR frame."""
    return ring_sectors(frame, geometry, **thresholds)[0]


def ring_sectors(frame, geometry: RingGeometry, *, min_value: float = 200.0,
                 max_saturation: float = 60.0, inside_margin: float = 40.0,
                 outside_margin: float = 8.0) -> "tuple[int, int]":
    """(lit, covered): the ring's sectors that are lit, and those that are not lit
    but covered by a coloured overlay, in this BGR frame."""
    import cv2

    g = geometry
    crop = frame[g.y0:g.y0 + g.size, g.x0:g.x0 + g.size]
    hsv = cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2HSV)
    v = hsv[..., 2].reshape(-1).astype(np.float32)
    s = hsv[..., 1].reshape(-1).astype(np.float32)
    lit = covered = 0
    for ring, inside, outside in zip(g.ring, g.inside, g.outside, strict=True):
        if ring.size == 0 or inside.size == 0 or outside.size == 0:
            continue
        rv = v[ring]
        rs = s[ring]
        peak = float(np.percentile(rv, 90))
        if (peak >= min_value
                and float(np.median(rs[rv >= peak - 1])) <= max_saturation
                and peak - float(np.median(v[inside])) >= inside_margin
                and peak - float(np.median(v[outside])) >= outside_margin):
            lit += 1
        elif float(np.mean((rs >= _COVERED_MIN_SATURATION)
                           & (rv >= _COVERED_MIN_VALUE))) >= _COVERED_FRACTION:
            covered += 1
    return lit, covered


class SpecialWeaponReader:
    """The ring's state, read each battle tick, for the handlers to ask.

    READY needs two reads in a row; anything else takes effect on one, so a taken
    press shows on the next read and a single misread cannot cost a press. A read
    older than `stale_after_s` answers None (unknown), never READY.
    """

    def __init__(self, crop_box, cfg: dict | None = None, clock=time.time):
        cfg = cfg or {}
        self._box = crop_box
        self._sectors = int(_setting(cfg, "sectors"))
        self._min_lit = int(_setting(cfg, "min_lit_sectors"))
        self._min_lit_covered = int(_setting(cfg, "min_lit_when_covered"))
        self._stale_after_s = float(_setting(cfg, "stale_after_s"))
        self._thresholds = {
            "min_value": float(_setting(cfg, "min_value")),
            "max_saturation": float(_setting(cfg, "max_saturation")),
            "inside_margin": float(_setting(cfg, "inside_margin")),
            "outside_margin": float(_setting(cfg, "outside_margin")),
        }
        self._clock = clock
        self._geometry: RingGeometry | None = None
        self._geometry_shape = None
        self._lock = threading.Lock()
        self._ready = False
        self._ready_streak = 0
        self._ts = 0.0
        # A READY that drops to a partial ring is either a taken press (the arc) or
        # a misread; the crop is saved so the two can be told apart against the
        # press log (2026-11-07: 8 one-read drops to 11-14/16 in 7 rounds).
        self._capture_dir = str(_setting(cfg, "capture_dir"))
        self._capture_left = int(_setting(cfg, "capture_max"))

    def update(self, frame) -> None:
        """Read the ring in this frame. Never raises."""
        if self._box is None or frame is None or getattr(frame, "ndim", 0) != 3:
            return
        try:
            if self._geometry_shape != frame.shape[:2]:
                self._geometry = RingGeometry(self._box, frame.shape, self._sectors)
                self._geometry_shape = frame.shape[:2]
            if not self._geometry.valid:
                return
            lit, covered = ring_sectors(frame, self._geometry, **self._thresholds)
        except Exception as exc:  # perception must not cost the tick
            logger.debug("Special weapon: ring read failed: %s", exc)
            return
        if self.note_lit(lit, covered) and lit >= self._sectors // 2:
            self._capture_drop(frame, lit)

    def _capture_drop(self, frame, lit: int) -> None:
        """Save the icon from a READY that dropped to a partial ring. Never raises."""
        if self._capture_left <= 0:
            return
        self._capture_left -= 1
        try:
            import os

            import cv2

            g = self._geometry
            pad = g.size // 2
            y0, x0 = max(0, g.y0 - pad), max(0, g.x0 - pad)
            crop = frame[y0:g.y0 + g.size + pad, x0:g.x0 + g.size + pad]
            os.makedirs(self._capture_dir, exist_ok=True)
            now = self._clock()
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(now))
            name = f"{stamp}.{int(now * 1000) % 1000:03d}_lit{lit:02d}.png"
            cv2.imwrite(os.path.join(self._capture_dir, name), crop)
            logger.debug("Special weapon icon: drop to %d/%d saved as %s",
                         lit, self._sectors, name)
        except Exception as exc:
            logger.debug("Special weapon: drop capture failed: %s", exc)

    def note_lit(self, lit: int, covered: int = 0) -> bool:
        """Record one read of `lit` sectors, `covered` more under a coloured overlay.
        True when this read ended a READY.

        A full ring is `min_lit_sectors` lit. A ring partly under a HUD marker is
        READY too when at least `min_lit_when_covered` are lit and the covered ones
        make up the rest; a taken press's draining arc (12-13 lit on its first read,
        fewer after) cannot hold that for the two reads READY needs."""
        lit_now = lit >= self._min_lit or (
            lit >= self._min_lit_covered and lit + covered >= self._min_lit)
        with self._lock:
            self._ready_streak = self._ready_streak + 1 if lit_now else 0
            was = self._ready
            self._ready = self._ready_streak >= 2
            self._ts = self._clock()
        if self._ready != was:
            logger.debug("Special weapon icon: %s (lit %d/%d)",
                         "READY" if self._ready else "not ready", lit, self._sectors)
        return was and not self._ready

    def ready(self) -> "bool | None":
        """True when the ring is lit, False when not, None when there is no fresh read."""
        with self._lock:
            if self._ts == 0.0 or self._clock() - self._ts > self._stale_after_s:
                return None
            return self._ready

    def reset(self) -> None:
        """Forget the reading: a new life or a new round starts unknown."""
        with self._lock:
            self._ready = False
            self._ready_streak = 0
            self._ts = 0.0


class SpecialWeaponPresser:
    """When a jet's special weapon is pressed (ADR 173 D3).

    `tick(now, ready, fired)`: `fired` is the jet's trigger this tick, an edge for
    `incoming` and a level for `low_flares` and `ready`. Returns True when `q` should
    go out now. A press that the icon does not answer (it stays READY) is repeated
    after `retry_s`, up to `max_presses` presses for one trigger; a new edge, or the
    icon leaving READY, starts the count again. A trigger that fires while the icon
    is not READY is dropped, not queued.
    """

    def __init__(self, trigger: str, *, retry_s: float = 3.0, max_presses: int = 2):
        if trigger not in TRIGGERS:
            raise ValueError(f"special weapon trigger {trigger!r} not one of {TRIGGERS}")
        self.trigger = trigger
        self._retry_s = float(retry_s)
        self._max_presses = int(max_presses)
        self._pending = False
        self._presses = 0
        self._last_press = None
        self.skipped_not_ready = 0
        self.gave_up = 0

    @classmethod
    def from_config(cls, trigger: str, cfg: dict | None) -> "SpecialWeaponPresser":
        cfg = cfg or {}
        return cls(trigger, retry_s=float(_setting(cfg, "press_retry_s")),
                   max_presses=int(_setting(cfg, "max_presses")))

    def tick(self, now: float, ready: "bool | None", fired: bool) -> bool:
        if ready is not True:
            # Not READY: a press went through, or there is nothing to press.
            self._presses = 0
            self._pending = False
            if fired and self.trigger == "incoming":
                self.skipped_not_ready += 1
            return False
        if self.trigger == "incoming":
            if fired:
                self._pending = True
                self._presses = 0
        else:
            self._pending = fired
        if not self._pending:
            return False
        if self._last_press is not None and now - self._last_press < self._retry_s:
            return False
        if self._presses >= self._max_presses:
            if self._pending and self.trigger == "incoming":
                self._pending = False
            self.gave_up += 1
            if self.gave_up == 1 or self.gave_up % 20 == 0:
                logger.warning("Special weapon: %d presses and the icon still reads "
                               "READY; waiting for it to change (%s trigger)",
                               self._presses, self.trigger)
            return False
        self._last_press = now
        self._presses += 1
        return True

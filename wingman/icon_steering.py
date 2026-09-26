"""Icon-directed search for the pursuit (HLDD 015, "Icon-Directed Search", 2026-09-26).

When an enemy is off the nose the game draws a red arrowhead on a fixed ring
around the screen centre, pointing toward it. Measured over 113,918 logged
icon-sized blobs: the ring is centred on the frame centre (959.7, 599.8 of
1920 x 1200) with radius 193.9 px, 10th to 90th percentile residual -3.7 to
+4.3 px. So the icon carries a direction (its angle) and nothing else.

Each scan adds points to two signed scores from the icon's direction
(`IconPoints.scan`), the operator's design: the reference frame
(`GAME_BATTLE_ENEMY_AT_NOSE_DOWN.png`) adds nose down 5, left 1. The scores
fade while icons keep coming, so they follow the latest direction, and hold
still while no icon is on screen: the aircraft keeps flying the direction the
icons gave until the target appears and the tracker locks (operator,
2026-09-26). They reset on a lock, a dive recovery, the icon crossing the
centre line and the end of the pursuit, and drive a per-axis hysteresis switch. `IconPoints.intent` turns the switches into the keys the
dominant-intent law would hold: a push only with the wings level, a roll only
with a pull (ADR 101: a bank without a pull does not turn the flight path).

Nothing here presses a key. The pursuit loop logs what the law would hold
(`ICONPTS`); with `wings_level` (rollout step 2a) it stops the fixed left
search roll while an icon or active points say where the enemy is, and with
`actuate_pitch` (step 2b) it also holds the law's vertical intent.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

import cv2
import numpy as np

# Key names the law reports. Names, not key constants: the shadow stage only
# logs them, and actuation (HLDD 015 rollout step 2) maps them to keys.
NOSE_DOWN = "NOSE_DOWN"
NOSE_UP = "NOSE_UP"
ROLL_LEFT = "ROLL_LEFT"
ROLL_RIGHT = "ROLL_RIGHT"


@dataclass(frozen=True)
class IconSteeringConfig:
    """`pursuit_mode.icon_steering`. Defaults are the HLDD 015 values; the
    ring geometry is measured, everything else is a named guess."""

    enabled: bool = False
    # HLDD 015 rollout step 2a: while an icon is on the ring or the points are
    # active, keep the wings level instead of the fixed left search roll.
    # Presses nothing new; off keeps the pure shadow.
    wings_level: bool = False
    # Rollout step 2b: the law's vertical intents act. "down" holds NOSE_DOWN
    # and "up" NOSE_UP, with the roll level; "turn" stays in shadow. Implies
    # wings_level, and replaces the look-down taps wherever an icon or active
    # points steer. Nose-down is withheld by the dive guard, a flight path at
    # or past icon_min_path_deg, or no fresh angle.
    actuate_pitch: bool = False
    # No icon nose-down without a fresh flight-path angle reading, whether or
    # not icon_min_path_deg is set (operator, 2026-09-26, kept when the -45 deg
    # limit was removed).
    require_fresh_angle: bool = True
    ring_centre_pct: tuple = (0.5, 0.5)
    # Fractions of the frame height: 168-216 px at 1200 px against 193.9 +-4.
    ring_radius_pct: tuple = (0.14, 0.18)
    area_px: tuple = (150, 2500)
    side_px: tuple = (15, 70)
    # Red icons measured at hue 2-3 (and 179, the wrap). Every archived ring
    # detection at hue 5-10 was the own afterburner, sunset sky, cloud or a flare
    # (2026-09-26 corpus, 293 frames), so red stops at 4.
    red_hue_max: int = 4
    # Orange icons (hue 12-13; operator: they count exactly as red) share their
    # hue with sunset sky and glare, so orange is accepted only when solid: at
    # least `orange_min_uniform` of the component's pixels within 10 of its
    # median saturation and at value 240 or more (orange icons 0.71-0.92, sunset
    # and glare 0.00; value medians 201-223 against 255).
    orange_hue: tuple = (11, 15)
    orange_min_uniform: float = 0.6
    sat_min: int = 110
    val_min: int = 200
    points_scale: float = 5.0
    points_half_life_s: float = 1.0
    points_cap: float = 25.0
    act_pts: float = 10.0
    release_pts: float = 4.0
    # Operator, 2026-09-26: no icon-led nose-down at or past -45 deg.
    icon_min_path_deg: "float | None" = -45.0
    blind_search_after_s: float = 3.0

    @classmethod
    def from_dict(cls, cfg: "dict | None") -> "IconSteeringConfig":
        cfg = cfg or {}
        d = cls()
        min_path = cfg.get("icon_min_path_deg", d.icon_min_path_deg)
        return cls(
            enabled=bool(cfg.get("enabled", d.enabled)),
            wings_level=bool(cfg.get("wings_level", d.wings_level)),
            actuate_pitch=bool(cfg.get("actuate_pitch", d.actuate_pitch)),
            require_fresh_angle=bool(cfg.get("require_fresh_angle", d.require_fresh_angle)),
            ring_centre_pct=tuple(float(v) for v in cfg.get("ring_centre_pct", d.ring_centre_pct)),
            ring_radius_pct=tuple(float(v) for v in cfg.get("ring_radius_pct", d.ring_radius_pct)),
            area_px=tuple(int(v) for v in cfg.get("area_px", d.area_px)),
            side_px=tuple(int(v) for v in cfg.get("side_px", d.side_px)),
            red_hue_max=int(cfg.get("red_hue_max", d.red_hue_max)),
            orange_hue=tuple(int(v) for v in cfg.get("orange_hue", d.orange_hue)),
            orange_min_uniform=float(cfg.get("orange_min_uniform", d.orange_min_uniform)),
            sat_min=int(cfg.get("sat_min", d.sat_min)),
            val_min=int(cfg.get("val_min", d.val_min)),
            points_scale=float(cfg.get("points_scale", d.points_scale)),
            points_half_life_s=float(cfg.get("points_half_life_s", d.points_half_life_s)),
            points_cap=float(cfg.get("points_cap", d.points_cap)),
            act_pts=float(cfg.get("act_pts", d.act_pts)),
            release_pts=float(cfg.get("release_pts", d.release_pts)),
            icon_min_path_deg=None if min_path is None else float(min_path),
            blind_search_after_s=float(cfg.get("blind_search_after_s", d.blind_search_after_s)),
        )


@dataclass(frozen=True)
class RingIcon:
    """One icon on the ring: centre in frame pixels, angle in degrees in screen
    convention (0 right, 90 down, 180 left, -90 up), median hue."""

    x: float
    y: float
    area: int
    w: int
    h: int
    angle_deg: float
    hue: int


def find_ring_icons(frame, cfg: IconSteeringConfig) -> "list[RingIcon]":
    """Red or orange components whose centre lies in the ring band, largest first.

    Only the ring's bounding square is scanned. The band is also what keeps the
    kill feed, the INCOMING banner and other fixed chrome out: none of it lies
    168-216 px from the centre. Icons come solid (arrowheads, jets) and hollow
    (jet outlines, whose anti-aliased edges are not uniform), so red is decided
    by hue alone; orange also has to be solid (see `IconSteeringConfig`).
    Returns [] for anything that is not a colour frame.
    """
    if not isinstance(frame, np.ndarray) or frame.ndim != 3 or frame.shape[2] < 3:
        return []
    fh, fw = frame.shape[:2]
    cx = fw * cfg.ring_centre_pct[0]
    cy = fh * cfg.ring_centre_pct[1]
    r_min = fh * cfg.ring_radius_pct[0]
    r_max = fh * cfg.ring_radius_pct[1]
    reach = int(math.ceil(r_max + cfg.side_px[1] / 2.0))
    x1, y1 = max(0, int(cx) - reach), max(0, int(cy) - reach)
    x2, y2 = min(fw, int(cx) + reach + 1), min(fh, int(cy) + reach + 1)
    if x2 <= x1 or y2 <= y1:
        return []
    hsv = cv2.cvtColor(np.ascontiguousarray(frame[y1:y2, x1:x2, :3]), cv2.COLOR_BGR2HSV)
    lo = np.array([0, cfg.sat_min, cfg.val_min], dtype=np.uint8)
    mask = (cv2.inRange(hsv, lo, np.array([cfg.red_hue_max, 255, 255], dtype=np.uint8))
            | cv2.inRange(hsv, np.array([170, cfg.sat_min, cfg.val_min], dtype=np.uint8),
                          np.array([180, 255, 255], dtype=np.uint8))
            | cv2.inRange(hsv, np.array([cfg.orange_hue[0], cfg.sat_min, cfg.val_min], dtype=np.uint8),
                          np.array([cfg.orange_hue[1], 255, 255], dtype=np.uint8)))
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    icons: "list[RingIcon]" = []
    for i in range(1, n):
        area = int(stats[i, cv2.CC_STAT_AREA])
        w = int(stats[i, cv2.CC_STAT_WIDTH])
        h = int(stats[i, cv2.CC_STAT_HEIGHT])
        if not (cfg.area_px[0] <= area <= cfg.area_px[1]):
            continue
        if not (cfg.side_px[0] <= w <= cfg.side_px[1] and cfg.side_px[0] <= h <= cfg.side_px[1]):
            continue
        x = x1 + float(centroids[i][0])
        y = y1 + float(centroids[i][1])
        if not (r_min <= math.hypot(x - cx, y - cy) <= r_max):
            continue
        px = hsv[labels == i]
        hue = int(np.median(px[:, 0]))
        if cfg.orange_hue[0] <= hue <= cfg.orange_hue[1]:
            sat = np.median(px[:, 1])
            uniform = np.mean((np.abs(px[:, 1].astype(int) - sat) <= 10) & (px[:, 2] >= 240))
            if uniform < cfg.orange_min_uniform:
                continue
        elif cfg.red_hue_max < hue < 170:
            continue    # a red-orange blend: the afterburner, sunset, a flare
        icons.append(RingIcon(x, y, area, w, h, math.degrees(math.atan2(y - cy, x - cx)), hue))
    icons.sort(key=lambda ic: ic.area, reverse=True)
    return icons


def _round_half_away(v: float) -> int:
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def _sign(v: float) -> int:
    return (v > 0) - (v < 0)


def _angle_gap(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


class IconPoints:
    """The two scores, their hysteresis switches and the dominant-intent law.

    `turn_pts` is positive right, `pitch_pts` positive nose down (the tracker's
    `error_norm` / `error_norm_y` signs). One instance per pursuit: points
    never carry over from one engagement to the next.
    """

    def __init__(self, cfg: IconSteeringConfig, clock=time.time) -> None:
        self._cfg = cfg
        self._clock = clock
        self.turn_pts = 0.0
        self.pitch_pts = 0.0
        self.turn_active = 0     # -1, 0 or +1: the sign the switch is on
        self.pitch_active = 0
        self.last_icon_ts: "float | None" = None
        self._last_scan_ts: "float | None" = None

    def reset(self) -> None:
        """Zero both scores and switches (a lock, a dive recovery). The last
        time an icon was seen is kept: it is when, not what."""
        self.turn_pts = self.pitch_pts = 0.0
        self.turn_active = self.pitch_active = 0
        self._last_scan_ts = None

    def contribution(self, icon: RingIcon) -> "tuple[int, int]":
        """(turn, pitch) points one icon adds: round(scale * unit vector), halves
        away from zero. The reference icon gives (-1, +5)."""
        a = math.radians(icon.angle_deg)
        s = self._cfg.points_scale
        return _round_half_away(s * math.cos(a)), _round_half_away(s * math.sin(a))

    def choose(self, icons: "list[RingIcon]") -> "RingIcon | None":
        """One icon, never an average. With a score vector at least
        release_pts long, the icon whose angle is nearest it; otherwise the
        icon nearest 6 or 12 o'clock, the only moves that need no turn."""
        if not icons:
            return None
        if math.hypot(self.turn_pts, self.pitch_pts) >= self._cfg.release_pts:
            ref = math.degrees(math.atan2(self.pitch_pts, self.turn_pts))
            return min(icons, key=lambda ic: _angle_gap(ic.angle_deg, ref))
        return min(icons, key=lambda ic: min(_angle_gap(ic.angle_deg, 90.0),
                                             _angle_gap(ic.angle_deg, -90.0)))

    def scan(self, icons: "list[RingIcon]") -> "tuple[RingIcon | None, tuple[int, int]]":
        """One scan: choose, then (only when there is an icon) decay, crossing
        reset, add, clamp; then the switches. Returns the chosen icon and the
        points it added.

        A scan with no icon changes nothing: the scores hold the last direction
        (operator, 2026-09-26: "it continues flying the direction of the icon
        until target appears on screen even when icon disappears"). Decay runs
        only while icons keep coming, over the time since the previous scan, so
        the scores follow the latest direction instead of piling up."""
        now = self._clock()
        prev_scan_ts = self._last_scan_ts
        self._last_scan_ts = now
        icon = self.choose(icons)
        add = (0, 0)
        if icon is not None:
            if prev_scan_ts is not None and self._cfg.points_half_life_s > 0:
                factor = 0.5 ** (max(0.0, now - prev_scan_ts) / self._cfg.points_half_life_s)
                self.turn_pts *= factor
                self.pitch_pts *= factor
            self.last_icon_ts = now
            add = self.contribution(icon)
            self.turn_pts = self._add(self.turn_pts, add[0])
            self.pitch_pts = self._add(self.pitch_pts, add[1])
            # Cap the length of the (turn, pitch) vector, not each axis: a per-axis
            # cap filled both axes to +-25 on a lower-left icon, so every angle read
            # 135 deg (measured, 2026-09-26 05:27 pursuit). Scaling both together
            # keeps the held direction the icon's direction.
            length = math.hypot(self.turn_pts, self.pitch_pts)
            if length > self._cfg.points_cap > 0:
                scale = self._cfg.points_cap / length
                self.turn_pts *= scale
                self.pitch_pts *= scale
        self.turn_active = self._switch(self.turn_active, self.turn_pts)
        self.pitch_active = self._switch(self.pitch_active, self.pitch_pts)
        return icon, add

    @staticmethod
    def _add(score: float, add: int) -> float:
        if add and _sign(add) == -_sign(score):
            score = 0.0      # the icon crossed the centre line: the nose went past
        return score + add

    def _switch(self, active: int, score: float) -> int:
        if active and (_sign(score) != active or abs(score) < self._cfg.release_pts):
            active = 0
        if not active and abs(score) >= self._cfg.act_pts:
            active = _sign(score)
        return active

    def icon_seen_within(self, seconds: float) -> bool:
        return self.last_icon_ts is not None and self._clock() - self.last_icon_ts <= seconds

    def intent(self) -> "tuple[str, tuple[str, ...]]":
        """What the dominant-intent law would hold now: ("down", ...), ("up",
        ...), ("turn", ...) or ("none", ()). It acts on the scores whether or not
        an icon is on screen this scan; the lock (which zeroes them) is what
        takes over. Ties go to pitch."""
        roll = ROLL_LEFT if self.turn_active < 0 else ROLL_RIGHT
        if self.pitch_active and (not self.turn_active
                                  or abs(self.pitch_pts) >= abs(self.turn_pts)):
            if self.pitch_active > 0:
                return "down", (NOSE_DOWN,)          # wings level: roll released
            return "up", (NOSE_UP, roll) if self.turn_active else (NOSE_UP,)
        if self.turn_active:
            return "turn", (roll, NOSE_UP)           # bank and pull, as BoundaryTurn
        return "none", ()

    def blind_side(self) -> str:
        """The side the blind search should roll toward: where the points last
        pointed, left when they point nowhere."""
        return "right" if self.turn_pts > 0 else "left"

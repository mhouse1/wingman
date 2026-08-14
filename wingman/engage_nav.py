"""Mission-agnostic ring-engage navigation from minimap components (Design 003 / ADR 028).

Pure decision logic: no threads, no locks, no I/O, no game or OS access. The
tick loop feeds it the per-component minimap scan and the telemetry altitude;
it returns an Intent (steer / orbit / none). Actuation stays in the tick
handler and ``Controller``. The J20 mission invokes this today; the Phase 3
behavior tree's engage node (working name GAME_BATTLE_ENGAGE — a tree node,
not an FSM state) invokes the same object later.
"""

import math
from dataclasses import dataclass

RING_SHORT = "short"
RING_MID = "mid"
RING_LONG = "long"

MODE_IDLE = "idle"
MODE_ORBIT = "orbit"
MODE_ENGAGE_MID = "engage-mid"
MODE_ENGAGE_LONG = "engage-long"

# Equal radial widths, deliberately not equal areas: travel distance to the
# contact is the quantity of interest (ADR 028 revision 3).
_SHORT_BOUND = 1.0 / 3.0
_MID_BOUND = 2.0 / 3.0


class MinimapEma:
    """Exponential smoothing of a minimap centroid, in vector space.

    The raw scan's area-weighted centroid teleports tick-to-tick as icons
    enter and leave the mask (dry-run 2026-08-08: radius 0.11 → 0.49 → 0.58
    within three ticks while genuinely overhead), which would thrash the roll
    axis. Smoothing runs on the (x, y) centroid vector — never on the bearing
    angle directly, which cannot be averaged across the ±180° wrap.

    Pure logic: time is an explicit argument. A gap of ``ema_reset_after_s``
    without a sample reseeds from the next raw sample, so stale state never
    bridges a long detection loss.
    """

    def __init__(self, minimap_cfg: dict):
        self.alpha = float(minimap_cfg.get("ema_alpha", 0.4))
        self.reset_after_s = float(minimap_cfg.get("ema_reset_after_s", 5.0))
        self._x: "float | None" = None
        self._y: "float | None" = None
        self._last_sample_ts = 0.0

    def reset(self) -> None:
        self._x = None
        self._y = None
        self._last_sample_ts = 0.0

    def bearing_deg(self) -> "float | None":
        """Current smoothed bearing, or None when the EMA holds no state."""
        if self._x is None:
            return None
        return math.degrees(math.atan2(self._x, self._y))

    def update(
        self,
        bearing_deg: "float | None",
        radius_frac: "float | None",
        now: float,
    ) -> "tuple[float | None, float | None]":
        """Return the smoothed (bearing_deg, radius_frac) for one raw sample.

        A None sample passes through as (None, None) and leaves the state
        untouched — the caller owns gap semantics.
        """
        if bearing_deg is None or radius_frac is None:
            return None, None
        theta = math.radians(bearing_deg)
        x = radius_frac * math.sin(theta)
        y = radius_frac * math.cos(theta)
        if self._x is None or now - self._last_sample_ts > self.reset_after_s:
            self._x = x
            self._y = y
        else:
            self._x = self.alpha * x + (1.0 - self.alpha) * self._x
            self._y = self.alpha * y + (1.0 - self.alpha) * self._y
        self._last_sample_ts = now
        smoothed_bearing = math.degrees(math.atan2(self._x, self._y))
        smoothed_radius = math.hypot(self._x, self._y)
        return smoothed_bearing, smoothed_radius


@dataclass(frozen=True)
class RingSummary:
    """One ring's occupancy: red-icon components binned by normalised radius."""

    count: int
    pixel_count: int
    bearing_deg: "float | None"   # area-weighted centroid of this ring only
    radius_frac: "float | None"


@dataclass(frozen=True)
class Intent:
    """One tick's navigation outcome.

    kind is "steer" (error_norm set), "orbit" (direction set), or "none".
    mode is the policy state the intent was produced under.
    """

    mode: str
    kind: str
    error_norm: "float | None"
    direction: "str | None"
    reason: str


def angle_diff_deg(a: float, b: float) -> float:
    """Smallest absolute angular distance between two bearings, in degrees."""
    return abs((a - b + 180.0) % 360.0 - 180.0)


def ring_of(radius_frac: float) -> str:
    if radius_frac <= _SHORT_BOUND:
        return RING_SHORT
    if radius_frac <= _MID_BOUND:
        return RING_MID
    return RING_LONG


def bin_rings(components) -> "dict[str, RingSummary]":
    """Bin (bearing_deg, radius_frac, area_px) components into the three rings.

    Each ring's bearing is the area-weighted centroid of that ring's
    components only — a long-range straggler cannot capture steering while
    the mid ring is occupied (live-session finding, 2026-08-08).
    """
    acc = {name: [0, 0, 0.0, 0.0] for name in (RING_SHORT, RING_MID, RING_LONG)}
    for bearing_deg, radius_frac, area in components:
        theta = math.radians(bearing_deg)
        entry = acc[ring_of(radius_frac)]
        entry[0] += 1
        entry[1] += area
        entry[2] += area * radius_frac * math.sin(theta)
        entry[3] += area * radius_frac * math.cos(theta)
    rings = {}
    for name, (count, area, sum_x, sum_y) in acc.items():
        if count == 0:
            rings[name] = RingSummary(0, 0, None, None)
        else:
            x = sum_x / area
            y = sum_y / area
            rings[name] = RingSummary(
                count, area, math.degrees(math.atan2(x, y)), math.hypot(x, y),
            )
    return rings


class EngageNavigator:
    """Ring policy: orbit the short ring, else engage mid before long.

    | Priority | Condition                                | Behaviour   |
    |----------|------------------------------------------|-------------|
    | 1        | short count >= short_ring_min_count      | orbit       |
    | 2        | mid ring occupied                        | engage-mid  |
    | 3        | long ring occupied                       | engage-long |
    | 4        | nothing detected                         | idle        |

    Transitions into and out of orbit are debounced by
    ``ring_debounce_ticks`` consecutive ticks of agreement; engage-mid ↔
    engage-long switches are free (both steer). The engaged ring's centroid
    is EMA-smoothed. The EMA reseeds only when the new selection's bearing
    jumps beyond ``ema_reseed_angle_deg`` — a genuinely different target.
    A contact crossing the mid/long boundary keeps its smoothing: reseeding
    on every ring-label change re-admitted raw-sample steering reversals
    during boundary flaps (live session 2026-08-08 10:17).
    """

    def __init__(self, j20_cfg: dict, minimap_cfg: "dict | None" = None):
        self.min_safe_altitude = float(j20_cfg.get("min_safe_altitude", 500))
        self.bearing_deadzone_deg = float(j20_cfg.get("bearing_deadzone_deg", 12.0))
        self.short_ring_min_count = int(j20_cfg.get("short_ring_min_count", 1))
        self.ring_debounce_ticks = int(j20_cfg.get("ring_debounce_ticks", 2))
        self.ema_reseed_angle_deg = float(j20_cfg.get("ema_reseed_angle_deg", 60.0))
        self.rear_commit_deg = float(j20_cfg.get("rear_commit_deg", 150.0))
        # Release only in the true forward semicircle: live 2026-08-08 18:21,
        # a smoothed bearing crossing THROUGH the tail (+150 → −100) satisfied
        # a 120° release while still rear-quarter, re-admitting a reversal.
        self.rear_release_deg = float(j20_cfg.get("rear_release_deg", 90.0))
        self.orbit_direction = str(j20_cfg.get("orbit_direction", "right"))
        self._ema = MinimapEma(minimap_cfg or {})
        self.mode = MODE_IDLE
        self.last_rings: "dict[str, RingSummary] | None" = None
        self._candidate = self.mode
        self._candidate_streak = 0
        self._engaged_ring: "str | None" = None
        self._committed_sign: "float | None" = None

    @property
    def deadband_norm(self) -> float:
        """The bearing deadzone expressed in orient_nose_to_target error units."""
        return self.bearing_deadzone_deg / 90.0

    def reset(self) -> None:
        self.mode = MODE_IDLE
        self.last_rings = None
        self._candidate = self.mode
        self._candidate_streak = 0
        self._engaged_ring = None
        self._committed_sign = None
        self._ema.reset()

    def _classify(self, rings: dict) -> str:
        if rings[RING_SHORT].count >= self.short_ring_min_count:
            return MODE_ORBIT
        if rings[RING_MID].count > 0:
            return MODE_ENGAGE_MID
        if rings[RING_LONG].count > 0:
            return MODE_ENGAGE_LONG
        return MODE_IDLE

    def _advance_mode(self, candidate: str) -> None:
        # Debounce only transitions that enter or leave orbit; the steering
        # modes are interchangeable without stability risk.
        if candidate == self.mode:
            self._candidate = candidate
            self._candidate_streak = 0
            return
        if candidate == MODE_ORBIT or self.mode == MODE_ORBIT:
            if candidate == self._candidate:
                self._candidate_streak += 1
            else:
                self._candidate = candidate
                self._candidate_streak = 1
            if self._candidate_streak >= self.ring_debounce_ticks:
                self.mode = candidate
                self._candidate_streak = 0
        else:
            self.mode = candidate
            self._candidate = candidate
            self._candidate_streak = 0

    def update(
        self,
        components: "list | None",
        altitude: "float | None",
        now: float,
    ) -> Intent:
        """One tick: components from the minimap scan → navigation intent.

        Continuously steering toward detected enemies is what keeps the
        aircraft inside the battle arena — enemies only render inside it.

        @relation(FR-005, scope=function)
        """
        if altitude is None:
            return Intent(self.mode, "none", None, None, "no-telemetry")
        if altitude < self.min_safe_altitude:
            return Intent(self.mode, "none", None, None, "below-safe-floor")
        if components is None:
            return Intent(self.mode, "none", None, None, "scan-failed")
        rings = bin_rings(components)
        self.last_rings = rings
        self._advance_mode(self._classify(rings))

        if self.mode == MODE_ORBIT:
            self._engaged_ring = None
            self._committed_sign = None
            return Intent(self.mode, "orbit", None, self.orbit_direction, "orbit-short-ring")

        if self.mode == MODE_ENGAGE_MID:
            ring_name = RING_MID
        elif self.mode == MODE_ENGAGE_LONG:
            ring_name = RING_LONG
        else:
            self._engaged_ring = None
            self._committed_sign = None
            return Intent(self.mode, "none", None, None, "idle")

        ring = rings[ring_name]
        if ring.count == 0:
            # The engaged ring emptied while a debounced switch is pending —
            # hold quietly rather than steer on stale data.
            return Intent(self.mode, "none", None, None, "ring-empty")
        if ring_name != self._engaged_ring:
            state_bearing = self._ema.bearing_deg()
            if (state_bearing is None
                    or angle_diff_deg(ring.bearing_deg, state_bearing) > self.ema_reseed_angle_deg):
                self._ema.reset()
                self._committed_sign = None   # genuinely new target — free direction choice
            self._engaged_ring = ring_name
        bearing, _radius = self._ema.update(ring.bearing_deg, ring.radius_frac, now)

        # Rear-sector turn commitment (live finding 2026-08-08 15:01): a target
        # near ±180° has an unstable bearing SIGN — flipping the roll direction
        # each sample restarts the turn and never brings the target forward.
        # Commit to one direction while the target is deep astern; release once
        # it swings forward of rear_release_deg.
        abs_bearing = abs(bearing)
        if self._committed_sign is not None:
            if abs_bearing < self.rear_release_deg:
                self._committed_sign = None
            else:
                return Intent(self.mode, "steer", self._committed_sign, None, "steering-rear-commit")
        if abs_bearing >= self.rear_commit_deg:
            self._committed_sign = 1.0 if bearing >= 0 else -1.0
            return Intent(self.mode, "steer", self._committed_sign, None, "steering-rear-commit")

        if abs_bearing <= self.bearing_deadzone_deg:
            return Intent(self.mode, "none", None, None, "on-course")
        error = max(-1.0, min(1.0, bearing / 90.0))
        return Intent(self.mode, "steer", error, None, "steering")

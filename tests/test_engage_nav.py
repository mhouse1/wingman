"""Unit tests for ring-engage navigation (Design 003 / ADR 028, FR-005).

Pure-logic tests: no frames, no analyzer, no controller. Times are explicit
arguments, so no sleeping and no clock mocking.
"""

import pytest

from wingman.engage_nav import (
    EngageNavigator,
    MinimapEma,
    MODE_ENGAGE_LONG,
    MODE_ENGAGE_MID,
    MODE_IDLE,
    MODE_ORBIT,
    RING_LONG,
    RING_MID,
    RING_SHORT,
    bin_rings,
    ring_of,
)

J20_CFG = {
    "min_safe_altitude": 500,
    "bearing_deadzone_deg": 12,
    "short_ring_min_count": 1,
    "ring_debounce_ticks": 2,
    "orbit_direction": "right",
}

MINIMAP_CFG = {"ema_alpha": 0.4, "ema_reset_after_s": 5.0}

ALT = 5000


def make_nav(**overrides) -> EngageNavigator:
    cfg = dict(J20_CFG)
    cfg.update(overrides)
    return EngageNavigator(cfg, dict(MINIMAP_CFG))


def comp(bearing_deg, radius_frac, area=9):
    return (bearing_deg, radius_frac, area)


# ---------------------------------------------------------------------------
# Ring geometry
# ---------------------------------------------------------------------------


def test_ring_of_boundaries():
    assert ring_of(0.0) == RING_SHORT
    assert ring_of(1.0 / 3.0) == RING_SHORT
    assert ring_of(0.5) == RING_MID
    assert ring_of(2.0 / 3.0) == RING_MID
    assert ring_of(0.8) == RING_LONG
    assert ring_of(1.0) == RING_LONG


def test_bin_rings_counts_and_per_ring_bearing():
    rings = bin_rings([comp(10, 0.2), comp(-170, 0.25), comp(60, 0.5), comp(90, 0.9)])
    assert rings[RING_SHORT].count == 2
    assert rings[RING_MID].count == 1
    assert rings[RING_LONG].count == 1
    # Mid ring bearing is that ring's own centroid, untouched by the others.
    assert abs(rings[RING_MID].bearing_deg - 60.0) < 1e-6
    assert abs(rings[RING_LONG].bearing_deg - 90.0) < 1e-6


def test_bin_rings_empty_ring_is_none():
    rings = bin_rings([comp(0, 0.9)])
    assert rings[RING_SHORT].count == 0
    assert rings[RING_SHORT].bearing_deg is None
    assert rings[RING_MID].bearing_deg is None


def test_bin_rings_area_weighting_within_ring():
    # 25 px at east and 9 px at north, both mid ring → atan2(25, 9) ≈ 70.2°
    rings = bin_rings([comp(90, 0.5, area=25), comp(0, 0.5, area=9)])
    assert 65.0 <= rings[RING_MID].bearing_deg <= 75.0


# ---------------------------------------------------------------------------
# Safety gates
# ---------------------------------------------------------------------------


def test_no_telemetry_never_commands():
    intent = make_nav().update([comp(60, 0.5)], None, 0.0)
    assert intent.kind == "none"
    assert intent.reason == "no-telemetry"


def test_below_safe_floor_never_commands():
    intent = make_nav().update([comp(60, 0.5)], 300, 0.0)
    assert intent.kind == "none"
    assert intent.reason == "below-safe-floor"


def test_scan_failure_never_commands():
    intent = make_nav().update(None, ALT, 0.0)
    assert intent.kind == "none"
    assert intent.reason == "scan-failed"


def test_empty_components_is_idle():
    intent = make_nav().update([], ALT, 0.0)
    assert intent.mode == MODE_IDLE
    assert intent.kind == "none"


# ---------------------------------------------------------------------------
# Ring policy
# ---------------------------------------------------------------------------


def test_mid_beats_long():
    nav = make_nav()
    intent = nav.update([comp(60, 0.5), comp(-30, 0.9)], ALT, 0.0)
    assert intent.mode == MODE_ENGAGE_MID
    assert intent.kind == "steer"
    assert abs(intent.error_norm - 60.0 / 90.0) < 1e-6


def test_long_fallback_and_saturation():
    nav = make_nav()
    intent = nav.update([comp(-120, 0.9)], ALT, 0.0)
    assert intent.mode == MODE_ENGAGE_LONG
    assert intent.error_norm == -1.0


def test_deadzone_is_on_course():
    intent = make_nav().update([comp(5, 0.5)], ALT, 0.0)
    assert intent.mode == MODE_ENGAGE_MID
    assert intent.kind == "none"
    assert intent.reason == "on-course"


def test_orbit_entry_is_debounced():
    nav = make_nav()
    first = nav.update([comp(30, 0.2)], ALT, 0.0)
    assert first.mode == MODE_IDLE          # candidate orbit, streak 1 of 2
    assert first.kind == "none"
    second = nav.update([comp(30, 0.2)], ALT, 1.5)
    assert second.mode == MODE_ORBIT
    assert second.kind == "orbit"
    assert second.direction == "right"


def test_orbit_exit_is_debounced():
    nav = make_nav()
    nav.update([comp(30, 0.2)], ALT, 0.0)
    nav.update([comp(30, 0.2)], ALT, 1.5)
    assert nav.mode == MODE_ORBIT
    third = nav.update([comp(60, 0.5)], ALT, 3.0)   # short empty, mid occupied
    assert third.mode == MODE_ORBIT                 # still debouncing
    assert third.kind == "orbit"
    fourth = nav.update([comp(60, 0.5)], ALT, 4.5)
    assert fourth.mode == MODE_ENGAGE_MID
    assert fourth.kind == "steer"


def test_short_ring_min_count_threshold():
    nav = make_nav(short_ring_min_count=2)
    intent = nav.update([comp(30, 0.2), comp(60, 0.5)], ALT, 0.0)
    # One short straggler is below the threshold — engage the mid ring.
    assert intent.mode == MODE_ENGAGE_MID


def test_orbit_direction_configurable():
    nav = make_nav(orbit_direction="left")
    nav.update([comp(30, 0.2)], ALT, 0.0)
    intent = nav.update([comp(30, 0.2)], ALT, 1.5)
    assert intent.direction == "left"


def test_ema_reseeds_on_large_target_jump():
    nav = make_nav()
    first = nav.update([comp(60, 0.5)], ALT, 0.0)          # engage-mid seeds EMA at 60°
    assert abs(first.error_norm - 60.0 / 90.0) < 1e-6
    # Ring switch AND a 90° bearing jump (beyond ema_reseed_angle_deg=60):
    # a genuinely different target → reseed.
    second = nav.update([comp(-30, 0.8)], ALT, 1.5)
    assert second.mode == MODE_ENGAGE_LONG
    # Reseeded: exactly −30° → −0.333. Averaged across the switch it would be
    # a small positive error (≈ +0.15) — the wrong steering direction.
    assert abs(second.error_norm - (-30.0 / 90.0)) < 1e-6


def test_boundary_crossing_contact_keeps_smoothing():
    """Live 2026-08-08 10:17 regression: one contact flapping across the
    mid/long boundary must not discard the EMA on every ring-label change."""
    nav = make_nav()
    nav.update([comp(20, 0.64)], ALT, 0.0)                 # engage-mid, EMA seeds at 20°
    crossed = nav.update([comp(40, 0.68)], ALT, 1.5)       # same contact, now long ring
    assert crossed.mode == MODE_ENGAGE_LONG
    # Smoothed, not reseeded: strictly between the seed (20°) and the raw
    # sample (40°). A reseed would output exactly 40/90.
    assert 20.0 / 90.0 < crossed.error_norm < 40.0 / 90.0


def test_boundary_flap_steering_direction_is_stable():
    """Same contact oscillating across the boundary with drifting bearing:
    steering direction must never reverse (run-2 finding: reversals of
    ±1.0 → −0.85 within seconds under reseed-on-every-switch)."""
    nav = make_nav()
    sequence = [
        comp(30, 0.64), comp(34, 0.69), comp(28, 0.63),
        comp(35, 0.70), comp(31, 0.65),
    ]
    errors = []
    for i, contact in enumerate(sequence):
        intent = nav.update([contact], ALT, i * 1.5)
        if intent.error_norm is not None:
            errors.append(intent.error_norm)
    assert errors, "expected steering commands"
    assert all(error > 0 for error in errors), errors


def test_engaged_ring_empty_while_debouncing_holds_quietly():
    nav = make_nav()
    nav.update([comp(60, 0.5)], ALT, 0.0)                  # engage-mid
    intent = nav.update([comp(30, 0.2)], ALT, 1.5)         # mid emptied, short occupied
    assert intent.mode == MODE_ENGAGE_MID                  # orbit still debouncing
    assert intent.kind == "none"
    assert intent.reason == "ring-empty"


def test_rear_commit_no_reversal_on_astern_sign_flip():
    """Live 2026-08-08 15:01 regression: a target dead astern flips bearing
    sign between samples (+178 ↔ −179); each flip used to command a full
    opposite roll. Committed: the direction must hold."""
    nav = make_nav()
    first = nav.update([comp(178, 0.9)], ALT, 0.0)
    assert first.reason == "steering-rear-commit"
    assert first.error_norm == 1.0
    second = nav.update([comp(-179, 0.9)], ALT, 1.5)
    assert second.error_norm == 1.0            # no reversal
    third = nav.update([comp(177, 0.9)], ALT, 3.0)
    assert third.error_norm == 1.0


def test_rear_commit_releases_as_target_sweeps_forward():
    nav = make_nav()
    bearings = [170, 135, 95, 60, 30]
    errors = []
    reasons = []
    for i, bearing in enumerate(bearings):
        intent = nav.update([comp(bearing, 0.9)], ALT, i * 1.5)
        errors.append(intent.error_norm)
        reasons.append(intent.reason)
    # Sustained same-direction turn: never a sign reversal while the target
    # sweeps from astern to ahead-right.
    assert all(error > 0 for error in errors), errors
    assert reasons[0] == "steering-rear-commit"
    assert reasons[-1] == "steering"           # released, proportional again
    assert errors[-1] < 1.0


def test_rear_commit_holds_through_tail_crossing():
    """Live 2026-08-08 18:21 regression: the smoothed bearing crossed through
    the tail (+150-ish → −100-ish, still rear-quarter) and a 120° release
    band let the commitment go — commanding a full reversal. The commitment
    must hold anywhere outside the forward semicircle."""
    nav = make_nav()
    committed = nav.update([comp(178, 0.5)], ALT, 0.0)
    assert committed.error_norm == 1.0
    crossed = nav.update([comp(-100, 0.5)], ALT, 1.5)     # smoothed ≈ −150
    assert crossed.error_norm == 1.0                       # held, no reversal
    deeper = nav.update([comp(-100, 0.5)], ALT, 3.0)      # smoothed ≈ −127
    assert deeper.error_norm == 1.0


def test_rear_commit_cleared_by_target_reseed():
    nav = make_nav()
    committed = nav.update([comp(178, 0.9)], ALT, 0.0)
    assert committed.error_norm == 1.0
    # A genuinely different target (ring switch + >60° jump) reseeds the EMA
    # and frees the direction choice — reversal is correct here.
    switched = nav.update([comp(-40, 0.5)], ALT, 1.5)
    assert switched.mode == MODE_ENGAGE_MID
    assert switched.error_norm == pytest.approx(-40.0 / 90.0)


def test_reset_returns_to_idle():
    nav = make_nav()
    nav.update([comp(30, 0.2)], ALT, 0.0)
    nav.update([comp(30, 0.2)], ALT, 1.5)
    assert nav.mode == MODE_ORBIT
    nav.reset()
    assert nav.mode == MODE_IDLE
    assert nav.last_rings is None


def test_deadband_norm_matches_deadzone():
    assert make_nav().deadband_norm == 12.0 / 90.0


def test_logged_radius_sequence_is_mode_stable():
    """Regression from the 2026-08-08 sessions' radius jitter.

    The raw whole-map radius flapped short→mid→short within four ticks
    (0.113, 0.488, 0.585, 0.209 …). Ring classification would flip modes on
    every flap; the orbit debounce must reduce it to at most two mode
    changes: idle → engage-mid → orbit.
    """
    logged = [
        (-68.84, 0.113), (4.02, 0.488), (124.85, 0.585), (100.64, 0.209),
        (-52.81, 0.206), (-63.24, 0.206), (-78.26, 0.198),
    ]
    nav = make_nav()
    modes = []
    for i, (bearing, radius) in enumerate(logged):
        modes.append(nav.update([comp(bearing, radius)], ALT, i * 1.5).mode)
    assert modes == [
        MODE_IDLE, MODE_ENGAGE_MID, MODE_ENGAGE_MID, MODE_ENGAGE_MID,
        MODE_ORBIT, MODE_ORBIT, MODE_ORBIT,
    ]


# ---------------------------------------------------------------------------
# MinimapEma — unchanged behaviour, carried from revision 2
# ---------------------------------------------------------------------------


def make_ema() -> MinimapEma:
    return MinimapEma(dict(MINIMAP_CFG))


def test_ema_first_sample_passes_through():
    bearing, radius = make_ema().update(45.0, 0.5, 0.0)
    assert abs(bearing - 45.0) < 1e-9
    assert abs(radius - 0.5) < 1e-9


def test_ema_none_passes_through_and_keeps_state():
    ema = make_ema()
    ema.update(45.0, 0.5, 0.0)
    assert ema.update(None, None, 1.5) == (None, None)
    bearing, _ = ema.update(45.0, 0.5, 3.0)
    assert abs(bearing - 45.0) < 1e-6


def test_ema_reseeds_after_long_gap():
    ema = make_ema()
    ema.update(45.0, 0.5, 0.0)
    bearing, radius = ema.update(-135.0, 0.9, 6.1)
    assert abs(bearing - (-135.0)) < 1e-9
    assert abs(radius - 0.9) < 1e-9


def test_ema_damps_single_tick_swing():
    ema = make_ema()
    ema.update(0.0, 0.2, 0.0)
    bearing, radius = ema.update(90.0, 0.6, 1.5)
    assert 0.0 < bearing < 90.0
    assert radius < 0.4


def test_ema_reset_clears_state():
    ema = make_ema()
    ema.update(0.0, 0.2, 0.0)
    ema.reset()
    bearing, radius = ema.update(90.0, 0.6, 1.5)
    assert abs(bearing - 90.0) < 1e-9
    assert abs(radius - 0.6) < 1e-9


# --- ADR 028 revision 4: regroup when no enemy is on the minimap -------------
#
# Bearings below are measured from the Design 010 frames, not invented:
#   Step0 (safe, flying away from the edge): friendly centroid +4.6 deg, 0.46R
#   Step1 (at the boundary, about to cross): friendly centroid +179.8 deg, 0.75R
# The enemy scan returns nothing in both, which is why the navigator issued no
# command and the aircraft flew out of the map.

from wingman.engage_nav import MODE_REGROUP, MODE_IDLE, aggregate_bearing


def _nav_regroup(**minimap):
    cfg = {"regroup_enabled": True}
    cfg.update(minimap)
    return EngageNavigator({}, cfg)


def test_regroup_turns_around_when_the_battle_is_astern():
    """Step1, the frame that mattered. Friendlies dead astern means the fight
    is behind and the aircraft is pointed out of the map: command the turn."""
    nav = _nav_regroup()
    intent = nav.update([], 5000.0, 0.0, friendly_components=[(179.8, 0.75, 30)])
    assert intent.mode == MODE_REGROUP
    assert intent.kind == "steer"
    assert intent.reason == "steering-rear-commit"


def test_regroup_is_quiet_when_already_pointed_at_the_battle():
    """Step0. Friendlies ahead — do not manufacture a command."""
    nav = _nav_regroup()
    intent = nav.update([], 5000.0, 0.0, friendly_components=[(4.6, 0.46, 30)])
    assert intent.mode == MODE_REGROUP
    assert intent.kind == "none"
    assert intent.reason == "on-course"


def test_regroup_is_off_unless_configured():
    """House pattern: a change to what the aircraft does on 57% of ticks does
    not arrive silently through a code default."""
    nav = EngageNavigator({}, {})
    intent = nav.update([], 5000.0, 0.0, friendly_components=[(179.8, 0.75, 30)])
    assert intent.mode == MODE_IDLE
    assert intent.kind == "none"


def test_an_enemy_contact_always_outranks_regroup():
    """Regroup fills the silence; it must never displace a real target."""
    nav = _nav_regroup()
    intent = nav.update([(30.0, 0.5, 20)], 5000.0, 0.0,
                        friendly_components=[(179.8, 0.75, 30)])
    assert intent.mode != MODE_REGROUP


def test_no_friendlies_leaves_the_old_idle_behaviour():
    nav = _nav_regroup()
    intent = nav.update([], 5000.0, 0.0, friendly_components=[])
    assert intent.mode == MODE_IDLE
    assert intent.reason == "idle"


def test_regroup_respects_the_safety_floors():
    nav = _nav_regroup()
    f = [(179.8, 0.75, 30)]
    assert nav.update([], None, 0.0, friendly_components=f).reason == "no-telemetry"
    assert nav.update([], 10.0, 0.0, friendly_components=f).reason == "below-safe-floor"


def test_aggregate_bearing_is_area_weighted():
    """A large icon cluster should dominate a single stray marker."""
    got = aggregate_bearing([(0.0, 0.5, 100), (180.0, 0.5, 1)])
    assert got is not None and abs(got[0]) < 5.0


def test_aggregate_bearing_handles_no_area():
    assert aggregate_bearing([]) is None


def test_regroup_smoothing_does_not_share_the_enemy_ema():
    """Blending an enemy bearing with a friendly one across a mode change would
    steer at neither."""
    nav = _nav_regroup()
    nav.update([(0.0, 0.5, 20)], 5000.0, 0.0)          # enemy dead ahead
    intent = nav.update([], 5000.0, 1.0, friendly_components=[(179.8, 0.75, 30)])
    assert intent.mode == MODE_REGROUP
    assert intent.kind == "steer", "friendly bearing must not be dragged forward by enemy state"


# --- Design 010: the boundary must be a LINE, not amber terrain --------------

def _minimap_with(shapes, size=200):
    """Synthetic minimap: amber `shapes` drawn on a neutral disc."""
    import numpy as np, cv2
    img = np.full((size, size, 3), 90, dtype=np.uint8)
    for (x1, y1, x2, y2) in shapes:
        cv2.line(img, (x1, y1), (x2, y2), (20, 150, 230), 2)   # BGR amber
    return img


def _boundary_analyzer():
    import numpy as np
    from wingman.analyzer import GameStateAnalyzer
    a = GameStateAnalyzer.__new__(GameStateAnalyzer)
    a.crops = {"MINIMAP": (0.0, 0.0, 1.0, 1.0)}
    a._minimap_circle_cache = None
    a._minimap_mask_radius_frac = 0.93
    a._boundary_hsv_lower = np.array([8, 120, 120], np.uint8)
    a._boundary_hsv_upper = np.array([28, 255, 255], np.uint8)
    a._boundary_min_px = 20
    a._boundary_min_span_frac = 0.5
    return a


def test_a_long_line_is_read_as_the_boundary():
    a = _boundary_analyzer()
    assert a.detect_map_boundary(_minimap_with([(10, 60, 190, 60)])) is not None


def test_scattered_amber_terrain_is_not_a_boundary():
    """Live 2026-08-30: a count-only mask read a median 0.23R with 'ahead' on a
    52% coin flip — amber-hued terrain, not a map edge. Speckle must not
    produce a reading at all."""
    speckle = [(x, y, x + 3, y + 3) for x in range(20, 180, 12)
               for y in range(20, 180, 12)]
    assert _boundary_analyzer().detect_map_boundary(_minimap_with(speckle)) is None


def test_a_short_amber_streak_is_rejected():
    a = _boundary_analyzer()
    assert a.detect_map_boundary(_minimap_with([(95, 95, 115, 95)])) is None


def test_the_span_threshold_is_configurable():
    a = _boundary_analyzer()
    short = _minimap_with([(60, 100, 140, 100)])    # 80px of a 200px disc
    a._boundary_min_span_frac = 0.9
    assert a.detect_map_boundary(short) is None
    a._boundary_min_span_frac = 0.2
    assert a.detect_map_boundary(short) is not None

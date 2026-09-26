"""HLDD 015 Icon-Directed Search (2026-09-26): the ring-icon detector, the
points model and the dominant-intent law, shadow stage.

The detector is checked on four real frames, curated and enumerated below (the
ring band of each archived frame, the rest blacked out so the files stay small
and the geometry stays exact), then on synthetic shapes for the rules the real
frames do not exercise. The points model is checked against the operator's own
example: the reference icon adds nose down 5, left 1.
"""

import math
from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.controller import _EngagementTally
from wingman.icon_steering import (
    NOSE_DOWN, NOSE_UP, ROLL_LEFT, ROLL_RIGHT,
    IconPoints, IconSteeringConfig, RingIcon, find_ring_icons,
)

FIXTURES = Path(__file__).parent / "fixtures"
CFG = IconSteeringConfig(enabled=True)

# name -> (angle in degrees, median hue), measured on the full archived frame.
REAL_ICONS = {
    # test_screenshots/GAME_BATTLE_ENEMY_AT_NOSE_DOWN.png, the operator's reference
    "icon_ring_nose_down.png": (97.6, 3),
    # pursuit_mode_20260926_034355_110.png: the orange icon
    "icon_ring_orange_left.png": (171.9, 13),
    # pursuit_mode_20260926_031832_24.png
    "icon_ring_lower_right.png": (23.8, 3),
    # pursuit_mode_20260926_031826_23.png: on the ring beside the own jet
    "icon_ring_near_jet.png": (70.6, 3),
}


def _frame(name):
    img = cv2.imread(str(FIXTURES / name))
    assert img is not None, name
    return img


# ---------------------------------------------------------------------------
# Detector, real frames
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", sorted(REAL_ICONS))
def test_each_real_frame_yields_exactly_its_one_icon(name):
    angle, hue = REAL_ICONS[name]
    icons = find_ring_icons(_frame(name), CFG)
    assert len(icons) == 1, icons
    assert abs(icons[0].angle_deg - angle) <= 2.0
    assert icons[0].hue == hue


def test_the_reference_icon_sits_on_the_measured_ring():
    icon = find_ring_icons(_frame("icon_ring_nose_down.png"), CFG)[0]
    assert abs(icon.x - 934.6) <= 3 and abs(icon.y - 789.4) <= 3
    assert 190 <= math.hypot(icon.x - 960, icon.y - 600) <= 198


def test_the_reference_icon_adds_nose_down_5_and_left_1():
    """The operator's example, from the real frame."""
    icon = find_ring_icons(_frame("icon_ring_nose_down.png"), CFG)[0]
    assert IconPoints(CFG).contribution(icon) == (-1, 5)


# ---------------------------------------------------------------------------
# Detector, synthetic shapes
# ---------------------------------------------------------------------------

def _bgr(h, s, v):
    return tuple(int(c) for c in cv2.cvtColor(np.uint8([[[h, s, v]]]), cv2.COLOR_HSV2BGR)[0, 0])


def _blank():
    return np.zeros((1200, 1920, 3), np.uint8)


def _blob(img, angle_deg, radius=194, size=30, colour=(3, 165, 255)):
    x = int(round(960 + radius * math.cos(math.radians(angle_deg))))
    y = int(round(600 + radius * math.sin(math.radians(angle_deg))))
    cv2.rectangle(img, (x - size // 2, y - size // 2), (x + size // 2, y + size // 2),
                  _bgr(*colour), -1)
    return img


def test_a_red_blob_on_the_ring_is_found_at_its_angle():
    icons = find_ring_icons(_blob(_blank(), -60), CFG)
    assert len(icons) == 1 and abs(icons[0].angle_deg + 60) <= 1.5


def test_off_the_ring_is_ignored():
    """The kill feed, banner and other chrome all lie off the band."""
    assert find_ring_icons(_blob(_blank(), 90, radius=300), CFG) == []
    assert find_ring_icons(_blob(_blank(), 90, radius=120), CFG) == []


def test_too_small_is_ignored():
    assert find_ring_icons(_blob(_blank(), 0, size=8), CFG) == []


def test_afterburner_hue_is_ignored():
    """Hue 5-10 was the own afterburner, sunset or a flare on every archived
    detection there."""
    assert find_ring_icons(_blob(_blank(), 90, colour=(8, 165, 255)), CFG) == []


def test_a_solid_orange_icon_counts():
    icons = find_ring_icons(_blob(_blank(), 180, colour=(13, 164, 255)), CFG)
    assert len(icons) == 1 and icons[0].hue == 13


def test_a_dim_orange_patch_does_not():
    """Sunset and glare share the orange hue but not the flat, full-value fill."""
    assert find_ring_icons(_blob(_blank(), 180, colour=(13, 164, 215)), CFG) == []


def test_two_icons_are_both_returned_largest_first():
    img = _blob(_blob(_blank(), 80, size=24), 100, size=36)
    icons = find_ring_icons(img, CFG)
    assert len(icons) == 2 and icons[0].area > icons[1].area


def test_anything_but_a_colour_frame_yields_nothing():
    assert find_ring_icons(object(), CFG) == []
    assert find_ring_icons(np.zeros((1200, 1920), np.uint8), CFG) == []


# ---------------------------------------------------------------------------
# Points model
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _icon(angle_deg):
    a = math.radians(angle_deg)
    return RingIcon(960 + 194 * math.cos(a), 600 + 194 * math.sin(a), 400, 30, 30, angle_deg, 3)


REF = _icon(97.6)        # the reference frame's angle: down 5, left 1


def _run(points, clock, icons, n, dt=0.1):
    for _ in range(n):
        clock.t += dt
        points.scan(icons)


def test_the_operator_example_scan_by_scan():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    seen = []
    for _ in range(16):
        clock.t += 0.1
        p.scan([REF])
        seen.append((round(p.pitch_pts, 2), round(p.turn_pts, 2), p.pitch_active, p.turn_active))
    assert seen[0][:2] == (5.0, -1.0)
    assert seen[1][0] == pytest.approx(9.67, abs=0.01) and seen[1][2] == 0
    assert seen[2][0] == pytest.approx(14.02, abs=0.01) and seen[2][2] == 1   # acts at 0.3 s
    # The length cap keeps the scores on the icon's direction, so the slight
    # left settles near -3 and never switches on (the per-axis cap let it grow
    # to -10 by 1.6 s).
    assert all(row[3] == 0 for row in seen)
    assert p.intent() == ("down", (NOSE_DOWN,))     # pitch dominates: wings level, push


def test_the_score_vector_is_capped_by_length():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 40)
    assert math.hypot(p.turn_pts, p.pitch_pts) == pytest.approx(25.0)


@pytest.mark.parametrize("angle", [100.0, 130.0, 144.0, 159.0])
def test_a_held_direction_keeps_the_icons_angle(angle):
    """Measured 2026-09-26 05:27: a per-axis cap read every lower-left icon as
    (-25, +25), i.e. 135 deg. The length cap keeps each angle apart."""
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [_icon(angle)], 60)
    held = math.degrees(math.atan2(p.pitch_pts, p.turn_pts))
    assert abs(held - angle) <= 6.0     # the per-scan points are whole numbers


def test_an_icon_crossing_the_centre_line_zeroes_that_axis_first():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 3)
    turn_before = p.turn_pts
    clock.t += 0.1
    p.scan([_icon(-90)])          # straight up: pitch -5, turn 0
    assert p.pitch_pts == -5.0
    assert p.turn_pts == pytest.approx(turn_before * 0.5 ** 0.1)   # untouched, decayed


def test_the_direction_is_still_flown_after_the_icon_disappears():
    """Operator, 2026-09-26: the points exist so the aircraft keeps flying the
    icon's direction until the target appears, even when the icon is gone."""
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 10)
    held = (p.turn_pts, p.pitch_pts)
    _run(p, clock, [], 100)                # 10 s with no icon
    assert (p.turn_pts, p.pitch_pts) == held
    assert p.intent() == ("down", (NOSE_DOWN,))
    assert p.blind_side() == "left"


def test_a_lock_is_what_ends_it():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 10)
    _run(p, clock, [], 20)
    p.reset()                              # the tracker locked
    assert p.intent() == ("none", ())


def test_the_first_icon_after_a_gap_decays_by_one_scan_not_by_the_gap():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 3)
    before = p.pitch_pts
    _run(p, clock, [], 50)                 # 5 s gap
    _run(p, clock, [REF], 1)
    assert p.pitch_pts == pytest.approx(min(25.0, before * 0.5 ** 0.1 + 5))


def test_hysteresis_holds_between_the_release_and_act_thresholds():
    """Only icons now move the scores down: an icon on the far side of the
    horizontal axis zeroes pitch (crossing reset)."""
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 3)               # pitch +14.0, active
    _run(p, clock, [_icon(0)], 6)          # straight right: pitch adds 0, decays
    assert CFG.release_pts <= p.pitch_pts < CFG.act_pts
    assert p.pitch_active == 1             # still held between the thresholds
    _run(p, clock, [_icon(0)], 13)         # 9.2 fades below 4 after 13 more scans
    assert p.pitch_pts < CFG.release_pts and p.pitch_active == 0


def test_a_turn_dominant_icon_banks_and_pulls():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [_icon(180)], 4)
    assert p.intent() == ("turn", (ROLL_LEFT, NOSE_UP))


def test_up_with_a_side_component_pulls_and_rolls_toward_it():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [_icon(-60)], 6)        # up 4, right 3 per scan
    assert p.intent() == ("up", (NOSE_UP, ROLL_RIGHT))


def test_down_never_rolls_even_with_the_side_axis_active():
    """A bank plus a push turns away from the bank (the review's finding)."""
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [_icon(45)], 6)         # down 4, right 4: a tie
    assert p.turn_active == 1 and p.pitch_active == 1
    assert p.intent() == ("down", (NOSE_DOWN,))


def test_choose_prefers_the_icon_nearest_the_score_vector():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 3)
    assert p.choose([_icon(-10), _icon(100)]).angle_deg == 100


def test_choose_without_a_score_prefers_a_vertical_icon():
    p = IconPoints(CFG, clock=_Clock())
    assert p.choose([_icon(10), _icon(-80)]).angle_deg == -80


def test_reset_zeroes_scores_and_switches():
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [REF], 5)
    p.reset()
    assert (p.turn_pts, p.pitch_pts, p.turn_active, p.pitch_active) == (0.0, 0.0, 0, 0)


# ---------------------------------------------------------------------------
# Config and summary line
# ---------------------------------------------------------------------------

def test_config_defaults_to_off():
    assert IconSteeringConfig.from_dict(None).enabled is False


def test_config_reads_the_shipped_keys():
    cfg = IconSteeringConfig.from_dict({"enabled": True, "icon_min_path_deg": None,
                                        "orange_hue": [11, 14]})
    assert cfg.enabled and cfg.icon_min_path_deg is None and cfg.orange_hue == (11, 14)


def test_summary_line_is_unchanged_without_the_icon_shadow():
    tally = _EngagementTally(clock=lambda: 0.0)
    assert "icon=" not in tally.line("PURSUIT", "cap", False)


def test_summary_line_reports_icon_scans_and_steer_time():
    clock = _Clock()
    tally = _EngagementTally(clock=clock)
    for rung, has_icon in (("icon", True), ("icon", True), ("hold", False), ("track", False)):
        tally.icon_tick(unlocked=rung != "track", has_icon=has_icon, rung=rung)
        clock.t += 0.1
    line = tally.line("PURSUIT", "cap", False)
    assert "icon=2/3" in line
    assert "icon_steer=0.2s" in line


# ---------------------------------------------------------------------------
# The blind search's side (HLDD 015, 2026-09-26): the side the lock or the icon
# was last seen on, not always left.
# ---------------------------------------------------------------------------

def test_last_known_side_rules():
    from wingman.controller import _last_known_side, _side_of
    assert _side_of(None) is None and _side_of(-0.2) == "left" and _side_of(0.3) == "right"
    assert _last_known_side(None, None, None) is None               # nothing seen: caller searches left
    assert _last_known_side(100.0, 0.4, None) == "right"            # the lock's side
    clock = _Clock()
    p = IconPoints(CFG, clock=clock)
    _run(p, clock, [_icon(180)], 3)                                  # icon on the left, newer than the lock
    assert _last_known_side(clock.t - 5.0, 0.4, p) == "left"
    assert _last_known_side(clock.t + 5.0, 0.4, p) == "right"       # a lock newer than the icon wins

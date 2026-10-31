"""The priority target's marker and direction pin (operator, 2026-10-09).

"similar to the resupply icon ... implement steering towards prioritytarget
defined by the yellow icon, where the small icon near the center of the screen
indicates direction to steer towards."

The fixtures are crops of the operator's screenshots of that day
(tests/test-output/priority-target), put back where they were, and the crown
crops the resupply detector was taught to refuse on 2026-10-02.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.priority_target import (
    PRIORITY_PIN_MAX_CORNER_SHARE,
    find_priority_marker,
    find_priority_ring_icons,
)
from wingman.resupply import (
    RESUPPLY_PIN_MIN_CORNER_SHARE,
    find_resupply_marker,
    find_resupply_ring_icons,
)

FIXTURES = Path(__file__).parent / "fixtures"
# What the pursuit passes: the tracker's acquisition region.
ACQUISITION = (0.0, 0.09, 1.0, 0.95)


def _live_crop_frame(name, origin):
    """A 1920x1200 frame holding one crop at its own place."""
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None, name
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    left, top = origin
    frame[top:top + crop.shape[0], left:left + crop.shape[1]] = crop
    return frame


# 15 to 39 px across. The first is drawn over the own aircraft's yellow nose;
# the second has an outline one pixel wide that is in two pieces.
@pytest.mark.parametrize("name, origin, expected", [
    ("priority_marker_over_airframe.png", (920, 616), (960, 656)),
    ("priority_marker_broken_outline.png", (856, 1045), (896, 1085)),
    ("priority_marker_far.png", (870, 418), (910, 458)),
    ("priority_marker_mid.png", (752, 578), (792, 618)),
    ("priority_marker_near.png", (1056, 579), (1136, 659)),
    ("resupply_neg_crown_icon.png", (277, 780), (358, 860)),      # 2026-10-02
])
def test_the_crown_marker_is_found_at_its_disc(name, origin, expected):
    marker = find_priority_marker(_live_crop_frame(name, origin), region_pct=ACQUISITION)

    assert marker is not None
    assert marker.x == pytest.approx(expected[0], abs=3)
    assert marker.y == pytest.approx(expected[1], abs=3)


def test_the_marker_carries_its_direction_from_the_screen_centre():
    marker = find_priority_marker(
        _live_crop_frame("priority_marker_mid.png", (752, 578)), region_pct=ACQUISITION)

    assert marker.angle_deg == pytest.approx(174, abs=2)      # left of centre


@pytest.mark.parametrize("name, origin", [
    ("resupply_pos_ring_near.png", (827, 760)),
    ("resupply_pos_ring_wide.png", (840, 360)),
    ("resupply_pos_haze.png", (561, 208)),
    ("resupply_pos_far.png", (1093, 536)),
])
def test_the_resupply_icon_is_not_the_priority_target(name, origin):
    """Same disc and outline. The crossed missiles are a third of the crown's
    share of the box and reach the corners."""
    frame = _live_crop_frame(name, origin)

    assert find_resupply_marker(frame) is not None
    assert find_priority_marker(frame, region_pct=ACQUISITION) is None


@pytest.mark.parametrize("name", ["MISSILE_EMPTY_RESUPPLY.png", "MISSILE_EMPTY_RESUPPLY2.png"])
def test_a_whole_frame_with_a_resupply_icon_has_no_priority_target(name):
    frame = cv2.imread(str(FIXTURES / name))

    assert find_priority_marker(frame, region_pct=ACQUISITION) is None
    assert find_priority_ring_icons(frame) == []


@pytest.mark.parametrize("name, origin", [
    ("resupply_neg_terrain.png", (205, 370)),
    ("resupply_neg_terrain_blur.png", (484, 112)),
    ("resupply_neg_grass.png", (1482, 542)),
    ("resupply_neg_nozzle.png", (828, 829)),
    ("resupply_neg_afterburner.png", (883, 676)),
    ("resupply_neg_explosion.png", (862, 939)),
    ("resupply_neg_flares.png", (797, 277)),
    ("resupply_neg_exhaust_indicator.png", (890, 715)),
    ("resupply_neg_glare.png", (816, 811)),
    ("resupply_neg_squad_arrow.png", (1143, 131)),
])
def test_yellow_lookalikes_are_not_the_priority_target(name, origin):
    frame = _live_crop_frame(name, origin)

    assert find_priority_marker(frame, region_pct=ACQUISITION) is None
    assert find_priority_ring_icons(frame) == []


def test_the_minimaps_own_crown_is_not_the_marker():
    """The minimap draws the crown at the far marker's size, so only its place
    tells them apart."""
    on_the_minimap = _live_crop_frame("priority_marker_far.png", (1700, 60))

    assert find_priority_marker(on_the_minimap, region_pct=ACQUISITION) is None


def test_the_marker_counts_only_inside_the_region_it_is_given():
    frame = _live_crop_frame("priority_marker_far.png", (870, 30))     # the score bar's rows

    assert find_priority_marker(frame, region_pct=ACQUISITION) is None
    assert find_priority_marker(frame, region_pct=(0.0, 0.0, 1.0, 1.0)) is not None


def test_what_is_not_a_color_frame_has_no_priority_target():
    assert find_priority_marker(object()) is None
    assert find_priority_marker(np.zeros((1200, 1920), dtype=np.uint8)) is None
    assert find_priority_ring_icons(object()) == []


# ---------------------------------------------------------------------------
# Behind terrain (operator, 2026-10-30): "in this case the priority target was
# behind a mountain". The pursuit flew at a crown drawn behind terrain on
# Crucible and into the mountain. In view the disc is its own dark olive fill;
# behind terrain the ground shows through it, and dark rock passed the old
# "darker than 110" test.
#
# That crown survives only in a half-size black-box frame
# (priority_marker_behind_terrain_halfsize.png, 03:45:43), too soft for the
# finder to find whole. What showed through its disc is carried into a crown in
# view, so the test meets what the finder met live: an inside as dark as the
# fill and not the fill. A full-size capture of a crown behind terrain replaces
# it when one is taken.
# ---------------------------------------------------------------------------

_YELLOW = (np.array([22, 140, 190], dtype=np.uint8), np.array([32, 255, 255], dtype=np.uint8))


def _disc_inside(frame, centre, half):
    """The square around a disc, and its inside as the finder reads it: within
    0.70 of the radius and not yellow."""
    x, y = centre
    box = frame[y - half:y + half, x - half:x + half]
    rows, cols = np.mgrid[0:2 * half, 0:2 * half]
    radius = np.hypot((cols - (2 * half - 1) / 2) / half, (rows - (2 * half - 1) / 2) / half)
    yellow = cv2.inRange(cv2.cvtColor(box, cv2.COLOR_BGR2HSV), *_YELLOW) > 0
    return box, ~yellow & (radius < 0.70)


def _crown_drawn_behind_terrain():
    """priority_marker_near.png with its disc at (380, 280) and the inside of
    the crown behind the mountain in place of its fill."""
    seen = cv2.imread(str(FIXTURES / "priority_marker_behind_terrain_halfsize.png"))
    seen_box, seen_inside = _disc_inside(seen, (20, 20), 9)
    frame = _live_crop_frame("priority_marker_near.png", (300, 200))
    box, inside = _disc_inside(frame, (380, 280), 19)
    box[inside] = np.resize(seen_box[seen_inside], (int(inside.sum()), 3))
    return frame


@pytest.mark.parametrize("name, origin", [
    ("priority_marker_over_airframe.png", (920, 616)),
    ("priority_marker_broken_outline.png", (856, 1045)),
    ("priority_marker_far.png", (870, 418)),
    ("priority_marker_mid.png", (752, 578)),
    ("priority_marker_near.png", (1056, 579)),
    ("resupply_neg_crown_icon.png", (277, 780)),
])
def test_a_crown_in_view_is_its_dark_fill_inside(name, origin):
    marker = find_priority_marker(_live_crop_frame(name, origin), region_pct=ACQUISITION)
    assert marker.fill >= 0.8


def test_a_crown_behind_terrain_is_not_returned():
    behind = []
    assert find_priority_marker(_crown_drawn_behind_terrain(), region_pct=ACQUISITION,
                                occluded=behind) is None
    # It met every other test, the old darkness one too: only its fill refuses it.
    assert len(behind) == 1 and behind[0].fill < 0.5
    assert (behind[0].x, behind[0].y) == (pytest.approx(380, abs=3), pytest.approx(280, abs=3))


def test_a_crown_in_view_wins_over_one_behind_terrain():
    frame = _crown_drawn_behind_terrain()
    in_view = cv2.imread(str(FIXTURES / "priority_marker_mid.png"))
    frame[578:578 + in_view.shape[0], 1200:1200 + in_view.shape[1]] = in_view
    behind = []
    marker = find_priority_marker(frame, region_pct=ACQUISITION, occluded=behind)
    assert marker is not None and marker.x == pytest.approx(1240, abs=3)
    assert len(behind) == 1


def test_the_pursuit_logs_a_crown_behind_terrain_and_flies_on(monkeypatch, caplog):
    from test_pursuit_mode import _make_ctrl
    ctrl = _make_ctrl(monkeypatch, priority_target={"enabled": True, "actuate": True})
    frame = _crown_drawn_behind_terrain()
    with caplog.at_level("INFO", logger="wingman.controller"):
        kind, marker, pins = ctrl._scan_marked_objective(frame)
        ctrl._scan_marked_objective(frame)            # rate-limited: one line
    assert (kind, marker, pins) == (None, None, [])
    lines = [r.getMessage() for r in caplog.records if "behind terrain" in r.getMessage()]
    assert len(lines) == 1 and lines[0].startswith("PRIORITY: priority target at (380,280)")


# The crown pin on the indicator ring. Against a bright cloud its pointer falls
# out of the color band and the outline is 53 px; beside the resupply pin the
# two outlines are the same shape, 24 px apart.
@pytest.mark.parametrize("name, origin, angle", [
    ("priority_pin_cloud.png", (852, 748), 110),
    ("priority_pin_beside_resupply_pin.png", (916, 360), -95),
    ("resupply_pin_crown.png", (903, 763), 95),                    # 2026-10-02
])
def test_the_crown_pin_on_the_ring_gives_the_direction(name, origin, angle):
    icons = find_priority_ring_icons(_live_crop_frame(name, origin))

    assert len(icons) == 1
    assert icons[0].angle_deg == pytest.approx(angle, abs=2)


@pytest.mark.parametrize("name, origin", [
    ("resupply_pin_upper_right.png", (1104, 473)),
    ("resupply_pin_below.png", (923, 764)),
    ("resupply_pin_left.png", (716, 557)),
    ("resupply_pin_occluded.png", (1127, 554)),
])
def test_the_resupply_pin_is_not_the_priority_targets(name, origin):
    assert find_priority_ring_icons(_live_crop_frame(name, origin)) == []


def test_each_pin_finder_takes_its_own_pin_of_two_side_by_side():
    """One frame with both pins, as the game drew them at 03:41 on 2026-10-09.
    The resupply pin's outline is 59 px there, a pixel under that finder's
    floor, so it reads none; it must not read the crown."""
    frame = _live_crop_frame("priority_pin_beside_resupply_pin.png", (916, 360))

    assert [round(icon.x) for icon in find_priority_ring_icons(frame)] == [944]
    assert find_resupply_ring_icons(frame) == []


def test_the_crown_pin_off_the_ring_is_not_a_direction():
    # The same crop 120 px further out: the pin's shape, not on the ring.
    assert find_priority_ring_icons(
        _live_crop_frame("priority_pin_cloud.png", (812, 868))) == []


def test_the_two_glyph_limits_leave_a_gap_between_them():
    """A pin is one or the other, never both."""
    assert PRIORITY_PIN_MAX_CORNER_SHARE < RESUPPLY_PIN_MIN_CORNER_SHARE

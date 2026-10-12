"""Air superiority: the red control points A, B and C (operator, 2026-10-09).

"similar to prioritytarget ... implement steering towards air superiority
icons A, B, or C, if the icons are red, the small icons indicates the direction
to steer towards ... it flies towards the A mark because it is a target on
screen and closer rather than steer towards B, it should fly through A then
proceed with B."

The fixtures are crops of the operator's screenshots of that day
(tests/test-output/air-superiority), put back where they were, and two
look-alikes the first version found on the archived frames.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.air_superiority import (
    find_control_point_marker,
    find_control_point_ring_icons,
)
from wingman.priority_target import find_priority_marker, find_priority_ring_icons
from wingman.resupply import find_resupply_marker, find_resupply_ring_icons

FIXTURES = Path(__file__).parent / "fixtures"
# What the pursuit passes: the tracker's acquisition region.
ACQUISITION = (0.0, 0.09, 1.0, 0.95)


def _crop(name):
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None, name
    return crop


def _frame(*placed):
    """A 1920x1200 frame holding each (name, origin) crop at its own place."""
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    for name, (left, top) in placed:
        crop = _crop(name)
        frame[top:top + crop.shape[0], left:left + crop.shape[1]] = crop
    return frame


# 22 to 56 px across. The first is prioritize_direct_target.png: the A disc
# inside its ring of arcs, with the crosshair drawn across its outline. In the
# third the letter touches the outline, so the two are one red component.
@pytest.mark.parametrize("name, origin, expected", [
    ("airsup_marker_reticle.png", (836, 502), (936, 602)),
    ("airsup_marker_far.png", (1064, 406), (1104, 446)),
    ("airsup_marker_letter_touching.png", (600, 410), (660, 470)),
    ("airsup_marker_mid.png", (804, 432), (874, 502)),
])
def test_a_red_control_point_in_view_is_found_at_its_disc(name, origin, expected):
    marker = find_control_point_marker(_frame((name, origin)), region_pct=ACQUISITION)

    assert marker is not None
    assert marker.x == pytest.approx(expected[0], abs=3)
    assert marker.y == pytest.approx(expected[1], abs=3)


def test_of_two_points_in_view_the_larger_is_the_one_to_fly_at():
    """The nearer point is drawn larger (operator: "a target on screen and
    closer")."""
    frame = _frame(("airsup_marker_far.png", (1064, 406)),
                   ("airsup_marker_mid.png", (504, 432)))

    marker = find_control_point_marker(frame, region_pct=ACQUISITION)

    assert (round(marker.x), round(marker.y)) == (574, 502)
    assert marker.width == 45


def test_the_arcs_around_the_disc_are_not_markers_of_their_own():
    """Each arc is as red as the disc and has a square box; it is one corner
    of a circle."""
    frame = _frame(("airsup_marker_reticle.png", (836, 502)))
    frame[572:632, 906:966] = 0                # the disc gone, the four arcs left

    assert find_control_point_marker(frame, region_pct=ACQUISITION) is None


@pytest.mark.parametrize("name, origin", [
    ("airsup_neg_squad_tag.png", (1102, 920)),    # a squad tag's boxed letter
    ("airsup_neg_red_crown.png", (0, 75)),        # an enemy's crown, drawn in the same red
])
def test_red_lookalikes_are_not_control_points(name, origin):
    frame = _frame((name, origin))

    assert find_control_point_marker(frame, region_pct=ACQUISITION) is None
    assert find_control_point_ring_icons(frame) == []


def test_the_score_bars_circled_letters_are_not_markers():
    """The score bar shows A, B and C in the same circles, above the region."""
    frame = _frame(("airsup_marker_far.png", (920, 0)))

    assert find_control_point_marker(frame, region_pct=ACQUISITION) is None


def test_the_minimaps_circled_letters_are_not_markers():
    frame = _frame(("airsup_marker_far.png", (1700, 150)))

    assert find_control_point_marker(frame, region_pct=ACQUISITION) is None


def test_what_is_not_a_color_frame_has_no_control_point():
    assert find_control_point_marker(object()) is None
    assert find_control_point_marker(np.zeros((1200, 1920), dtype=np.uint8)) is None
    assert find_control_point_ring_icons(object()) == []


# The red pin on the indicator ring: a circle around the letter and a pointer.
@pytest.mark.parametrize("name, origin, angle", [
    ("airsup_pin_right.png", (1126, 625), 16),
    ("airsup_pin_lower_right.png", (1115, 653), 24),
    ("airsup_pin_below_beside_blue.png", (865, 763), 108),
    ("airsup_pin_left.png", (728, 536), -170),
])
def test_a_red_pin_on_the_ring_gives_the_direction(name, origin, angle):
    icons = find_control_point_ring_icons(_frame((name, origin)))

    assert len(icons) == 1
    assert icons[0].angle_deg == pytest.approx(angle, abs=2)


def test_every_red_pin_on_the_ring_is_returned():
    """Two points the enemy holds, two pins: the icon law picks one and keeps
    to it."""
    frame = _frame(("airsup_pin_lower_right.png", (1115, 653)),
                   ("airsup_pin_below_beside_blue.png", (865, 763)))

    assert sorted(round(icon.angle_deg) for icon in find_control_point_ring_icons(frame)) == [
        24, 108]


def test_a_blue_pin_is_not_a_point_to_take():
    """Blue is a point the own team holds (operator: "if the icons are red")."""
    assert find_control_point_ring_icons(_frame(("airsup_pin_blue.png", (1129, 553)))) == []


@pytest.mark.parametrize("name, origin", [
    ("airsup_pin_under_blue_pin.png", (728, 586)),   # prioritize_direct_target.png: B under C
    ("airsup_pin_touching_jet.png", (724, 612)),     # B merged with an aircraft icon
])
def test_a_pin_another_icon_covers_is_not_read(name, origin):
    """Known limit: the circle is no longer closed, or no longer its own
    component. The aircraft icon over it points the same way."""
    assert find_control_point_ring_icons(_frame((name, origin))) == []


def test_a_red_pin_off_the_ring_is_not_a_direction():
    # The same crop 120 px further out: the pin's shape, not on the ring.
    assert find_control_point_ring_icons(_frame(("airsup_pin_left.png", (608, 536)))) == []


def test_control_points_are_neither_the_crown_nor_the_resupply_point():
    frame = _frame(("airsup_marker_mid.png", (804, 432)),
                   ("airsup_pin_left.png", (728, 536)))

    assert find_priority_marker(frame, region_pct=ACQUISITION) is None
    assert find_priority_ring_icons(frame) == []
    assert find_resupply_marker(frame, region_pct=ACQUISITION) is None
    assert find_resupply_ring_icons(frame) == []


@pytest.mark.parametrize("name, origin", [
    ("priority_marker_near.png", (1056, 579)),
    ("priority_marker_mid.png", (752, 578)),
    ("priority_pin_cloud.png", (852, 748)),
    ("priority_pin_beside_resupply_pin.png", (916, 360)),
    ("resupply_pos_ring_near.png", (827, 760)),
    ("resupply_pin_left.png", (716, 557)),
    ("icon_ring_nose_down.png", (0, 0)),          # the reference red aircraft icon
    ("icon_ring_near_jet.png", (0, 0)),
    ("icon_ring_lower_right.png", (0, 0)),
])
def test_the_yellow_objectives_and_the_red_aircraft_icons_are_not_control_points(name, origin):
    frame = _frame((name, origin))

    assert find_control_point_marker(frame, region_pct=ACQUISITION) is None
    assert find_control_point_ring_icons(frame) == []

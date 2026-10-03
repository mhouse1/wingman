from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.resupply import (
    MissilePriority,
    ResupplyMarker,
    ResupplyMarkerMemory,
    MissileUrgency,
    find_resupply_marker,
    find_resupply_ring_icons,
    marker_nearer_than_target,
    resupply_preempts,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name, expected", [
    ("MISSILE_EMPTY_RESUPPLY.png", (752, 627)),
    ("MISSILE_EMPTY_RESUPPLY2.png", (1395, 460)),
])
def test_real_resupply_capture_finds_the_yellow_marker(name, expected):
    frame = cv2.imread(str(FIXTURES / name))
    assert frame is not None

    marker = find_resupply_marker(frame)

    assert marker is not None
    assert marker.x == pytest.approx(expected[0], abs=4)
    assert marker.y == pytest.approx(expected[1], abs=4)


def _live_crop_frame(name, origin):
    """A 1920x1200 frame holding one crop of the 2026-10-02 session at its own place."""
    crop = cv2.imread(str(FIXTURES / name))
    assert crop is not None
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    left, top = origin
    frame[top:top + crop.shape[0], left:left + crop.shape[1]] = crop
    return frame


# The ring is about 150 and 190 px across in the first two, so its arcs are
# separate pieces that are each as large as the disc; the third is seen through
# haze; the fourth is a 27 px disc, too far for the ring-sized gate it replaced.
@pytest.mark.parametrize("name, origin, expected", [
    ("resupply_pos_ring_near.png", (827, 760), (957, 890)),
    ("resupply_pos_ring_wide.png", (840, 360), (959, 526)),
    ("resupply_pos_haze.png", (561, 208), (661, 308)),
    ("resupply_pos_far.png", (1093, 536), (1173, 616)),
])
def test_live_resupply_icon_is_found_at_its_disc(name, origin, expected):
    marker = find_resupply_marker(_live_crop_frame(name, origin))

    assert marker is not None
    assert marker.x == pytest.approx(expected[0], abs=3)
    assert marker.y == pytest.approx(expected[1], abs=3)


# What the resupply focus steered at on 2026-10-02: 38 of the 41 frames it
# saved held none of the icon where it pointed (docs/anomaly/011).
@pytest.mark.parametrize("name, origin", [
    ("resupply_neg_terrain.png", (205, 370)),
    ("resupply_neg_terrain_blur.png", (484, 112)),
    ("resupply_neg_grass.png", (1482, 542)),
    ("resupply_neg_nozzle.png", (828, 829)),
    ("resupply_neg_afterburner.png", (883, 676)),
    ("resupply_neg_explosion.png", (862, 939)),
    ("resupply_neg_flares.png", (797, 277)),
    ("resupply_neg_minimap_rim.png", (1585, 120)),
    ("resupply_neg_minimap_icon.png", (1654, 97)),
    # Icon-colored, from the first session on the disc detector: the
    # crown indicator made the focus dive from 2270 m.
    ("resupply_neg_exhaust_indicator.png", (890, 715)),
    ("resupply_neg_crown_indicator.png", (809, 709)),
    ("resupply_neg_glare.png", (816, 811)),
    ("resupply_neg_squad_arrow.png", (1143, 131)),
    ("resupply_neg_crown_icon.png", (277, 780)),
])
def test_live_yellow_lookalikes_are_not_the_resupply_icon(name, origin):
    assert find_resupply_marker(_live_crop_frame(name, origin)) is None


def _yellow_bgr():
    return tuple(int(channel) for channel in cv2.cvtColor(
        np.uint8([[[28, 220, 255]]]), cv2.COLOR_HSV2BGR)[0, 0])


def test_solid_yellow_hud_patch_and_small_yellow_glyph_are_rejected():
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    yellow = _yellow_bgr()
    cv2.rectangle(frame, (700, 500), (770, 570), yellow, -1)
    cv2.rectangle(frame, (900, 600), (909, 609), yellow, -1)

    assert find_resupply_marker(frame) is None


# The resupply direction pin on the indicator ring, cut from pursuit frames
# of 2026-10-02 and put back where it was.
@pytest.mark.parametrize("name, origin, angle", [
    ("resupply_pin_upper_right.png", (1104, 473), -25),
    ("resupply_pin_below.png", (923, 764), 89),
    ("resupply_pin_left.png", (716, 557), -179),
])
def test_resupply_pin_on_the_ring_gives_the_direction(name, origin, angle):
    icons = find_resupply_ring_icons(_live_crop_frame(name, origin))

    assert len(icons) == 1
    assert icons[0].angle_deg == pytest.approx(angle, abs=2)


@pytest.mark.parametrize("name, origin", [
    ("resupply_pin_crown.png", (903, 763)),       # same pin, the crown objective's
    ("resupply_pin_occluded.png", (1127, 554)),   # the pin under a repair icon
])
def test_other_pins_on_the_ring_are_not_the_resupply_direction(name, origin):
    assert find_resupply_ring_icons(_live_crop_frame(name, origin)) == []


def test_resupply_pin_off_the_ring_is_not_a_direction():
    # The same crop 120 px further out: the icon's shape, not on the ring.
    assert find_resupply_ring_icons(_live_crop_frame("resupply_pin_left.png", (596, 557))) == []
    assert find_resupply_ring_icons(object()) == []


def test_marker_counts_only_inside_the_region_it_is_given():
    """The pursuit passes the tracker's acquisition region (operator, 2026-10-02)."""
    acquisition = (0.0, 0.09, 1.0, 0.95)
    # The far icon moved to x 1830, right of the default region's 92% edge.
    at_edge = _live_crop_frame("resupply_pos_far.png", (1750, 536))

    assert find_resupply_marker(at_edge) is None
    marker = find_resupply_marker(at_edge, region_pct=acquisition)
    assert marker is not None
    assert marker.x == pytest.approx(1830, abs=3)

    # The same icon under the scoreboard, above the acquisition region's top.
    at_top = _live_crop_frame("resupply_pos_far.png", (880, 0))
    assert find_resupply_marker(at_top, region_pct=(0.0, 0.0, 1.0, 1.0)) is not None
    assert find_resupply_marker(at_top, region_pct=acquisition) is None


@pytest.mark.parametrize("frame", [object(), np.zeros((100, 100), dtype=np.uint8)])
def test_non_color_frames_have_no_resupply_marker(frame):
    assert find_resupply_marker(frame) is None


def _observe(urgency, ammo, rack="secondary", *, terminal_zero=False,
            resupply_seeking=False, count=2):
    state = urgency.snapshot()
    for _ in range(count):
        state = urgency.observe(ammo, rack, terminal_zero=terminal_zero,
                                resupply_seeking=resupply_seeking)
    return state


def test_spent_missiles_increase_urgency_and_ocr_increases_do_not_reduce_it():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 4)
    assert _observe(urgency, 3).missiles_spent == 1
    assert _observe(urgency, 2).missiles_spent == 2
    assert _observe(urgency, 4).missiles_spent == 2


def test_rack_switch_preserves_spent_count_and_does_not_fake_terminal_zero():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 4, "primary")
    _observe(urgency, 3, "primary")

    primary_empty = _observe(urgency, 0, "primary")
    secondary_loaded = _observe(urgency, 2, "secondary")

    assert primary_empty.missiles_spent == 4
    assert not primary_empty.empty
    assert secondary_loaded.missiles_spent == 4
    assert not secondary_loaded.empty


def test_only_confirmed_zero_on_the_final_rack_marks_empty():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 2)

    unconfirmed = _observe(urgency, 0, "secondary", terminal_zero=False)
    confirmed = urgency.observe(0, "secondary", terminal_zero=True)

    assert not unconfirmed.empty
    assert confirmed.empty
    assert confirmed.missiles_spent == 2


def test_confirmed_rearm_resets_urgency():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 2)
    _observe(urgency, 0, terminal_zero=True)
    rearmed = _observe(urgency, 3)

    assert rearmed.rearmed
    assert not rearmed.empty
    assert rearmed.missiles_spent == 0


def test_confirmed_ammo_increase_while_seeking_resets_before_zero():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 4)
    assert _observe(urgency, 2).missiles_spent == 2

    not_seeking = _observe(urgency, 4, resupply_seeking=False)
    assert not_seeking.missiles_spent == 2
    assert not_seeking.rearmed is False
    rearmed = _observe(urgency, 4, resupply_seeking=True, count=1)

    assert rearmed.rearmed
    assert not rearmed.empty
    assert rearmed.missiles_spent == 0


def test_unreadable_ammo_breaks_a_confirmation_run_without_losing_priority():
    urgency = MissileUrgency(confirm_reads=3)
    _observe(urgency, 3, count=3)
    urgency.observe(2, "secondary")
    urgency.observe(None, "secondary")
    state = urgency.observe(2, "secondary")

    assert state.missiles_spent == 0
    assert not state.empty
    state = _observe(urgency, 2, count=2)
    assert state.missiles_spent == 1


def test_one_ocr_read_polled_repeatedly_does_not_confirm_a_rearm():
    """2026-10-01 15:47: one misread of 44 was polled three times and reset urgency."""
    urgency = MissileUrgency(confirm_reads=3)
    for read_id in (1, 2, 3):
        urgency.observe(2, "secondary", read_id=read_id)
    for read_id in (4, 5, 6):
        empty = urgency.observe(0, "secondary", terminal_zero=True, read_id=read_id)
    assert empty.empty

    for _ in range(5):
        polled = urgency.observe(44, "secondary", read_id=7)
    assert not polled.rearmed
    assert polled.empty

    urgency.observe(44, "secondary", read_id=8)
    rearmed = urgency.observe(44, "secondary", read_id=9)
    assert rearmed.rearmed
    assert not rearmed.empty


def test_spent_missiles_need_distinct_reads_to_confirm():
    urgency = MissileUrgency(confirm_reads=2)
    urgency.observe(4, "secondary", read_id=1)
    urgency.observe(4, "secondary", read_id=2)

    for _ in range(4):
        polled = urgency.observe(3, "secondary", read_id=3)
    assert polled.missiles_spent == 0

    assert urgency.observe(3, "secondary", read_id=4).missiles_spent == 1


def test_resupply_priority_requires_two_missiles_spent():
    assert not resupply_preempts(
        priority=MissilePriority(1, False), marker_visible=True)
    assert resupply_preempts(
        priority=MissilePriority(2, False), marker_visible=True)


def test_resupply_priority_starts_after_two_missiles_and_overrides_opponents():
    assert not resupply_preempts(
        priority=MissilePriority(0, False), marker_visible=True)
    assert not resupply_preempts(
        priority=MissilePriority(1, False), marker_visible=True)
    assert resupply_preempts(
        priority=MissilePriority(2, False), marker_visible=True)

    assert not resupply_preempts(
        priority=MissilePriority(5, True), marker_visible=False)
    assert resupply_preempts(
        priority=MissilePriority(0, True), marker_visible=True)


def test_resupply_marker_memory_holds_only_during_a_brief_seek_dropout():
    frame = cv2.imread(str(FIXTURES / "MISSILE_EMPTY_RESUPPLY.png"))
    marker = find_resupply_marker(frame)
    memory = ResupplyMarkerMemory(hold_s=0.5)

    assert marker is not None
    assert memory.resolve(marker, 10.0, seeking=False) == (marker, False)
    assert memory.resolve(None, 10.49, seeking=True) == (marker, True)
    assert memory.resolve(None, 10.51, seeking=True) == (None, False)
    assert memory.resolve(None, 10.49, seeking=False) == (None, False)

    memory.resolve(marker, 20.0, seeking=True)
    memory.clear()
    assert memory.resolve(None, 20.1, seeking=True) == (None, False)


def test_emptied_rack_credits_its_confirmed_count():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 2, "primary")
    _observe(urgency, 1, "primary")
    assert urgency.snapshot().missiles_spent == 1

    state = urgency.rack_emptied("primary")

    assert state.missiles_spent == 2
    assert not state.empty
    assert urgency.rack_emptied("primary").missiles_spent == 2, "credited once"


def test_emptied_rack_without_a_confirmed_count_credits_the_last_positive_read():
    urgency = MissileUrgency(confirm_reads=3)
    urgency.observe(2, "primary", read_id=1)
    urgency.observe(0, "primary", read_id=2)

    assert urgency.rack_emptied("primary").missiles_spent == 2


def test_emptied_rack_that_was_never_read_credits_nothing():
    urgency = MissileUrgency(confirm_reads=3)
    urgency.observe(0, "primary", read_id=1)

    assert urgency.rack_emptied("primary").missiles_spent == 0


def test_emptied_primary_then_secondary_decrease_keeps_adding():
    urgency = MissileUrgency(confirm_reads=2)
    _observe(urgency, 2, "primary")
    urgency.rack_emptied("primary")
    _observe(urgency, 2, "secondary")

    state = _observe(urgency, 1, "secondary")

    assert state.missiles_spent == 3
    assert resupply_preempts(priority=state, marker_visible=True)


@pytest.mark.parametrize("target_error, nearer", [
    ((None, None), True),        # no target at all
    ((-0.5, 0.0), True),         # target 480 px out, marker 440
    ((-0.2, 0.0), False),        # target 192 px out
    ((0.0, 0.8), True),          # target 480 px below centre on a 1200 px frame
    ((0.0, 0.7), False),         # 420 px
    ((0.45, None), False),       # 432 px, no vertical error
])
def test_marker_nearer_than_target_compares_distance_from_the_screen_centre(target_error, nearer):
    marker = ResupplyMarker(1400, 600, 40, 40, 300, 0)      # 440 px right of centre
    assert marker_nearer_than_target(marker, target_error[0], target_error[1], 1920, 1200) is nearer

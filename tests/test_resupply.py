from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.resupply import (
    ControlledDescent,
    MissilePriority,
    ResupplyMarker,
    ResupplyMarkerMemory,
    ResupplyPassWatch,
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


# ---------------------------------------------------------------------------
# ResupplyPassWatch (Action Item 003): the climb-out starts at the pass
# ---------------------------------------------------------------------------

def _fly_marker(watch, offsets, *, start=100.0, step=0.17, steering=True, active=True,
                height=1200, then_missing_s=0.0, size=55):
    """Feed sightings `offsets` px right of the centre, one per `step`, then the
    marker missing tick by tick for `then_missing_s`. Returns every pass signed.
    `size` is the marker's px across at 1200 px of frame height."""
    signed = []
    t = start
    width = int(height * 1.6)
    across = round(size * height / 1200)
    for off in offsets:
        marker = None if off is None else ResupplyMarker(
            width / 2 + off * height / 1200, height / 2, across, across, 900, 0)
        got = watch.update(t, marker, steering=steering, active=active,
                           frame_width=width, frame_height=height)
        if got is not None:
            signed.append(got)
        t += step
    end = t + then_missing_s
    while t < end:
        got = watch.update(t, None, steering=steering, active=active,
                           frame_width=width, frame_height=height)
        if got is not None:
            signed.append(got)
        t += step
    return signed


# Twenty ticks steady near the centre, then four moving out: a pass.
_APPROACH = [20] * 20
_SWEEP = [40, 70, 110, 160]


def test_a_marker_lost_while_moving_outward_is_a_pass_signed_once():
    signed = _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, then_missing_s=3.0)
    assert len(signed) == 1
    assert signed[0].off_centre_px == pytest.approx(160)
    assert signed[0].outward_px > 100
    assert 0.5 <= signed[0].since_seen_s < 0.5 + 0.2


def test_a_marker_lost_from_the_centre_without_moving_is_not_a_pass():
    """The dropout: 73 of the 162 times the steered marker went missing on
    2026-10-13 it was back within 4 s, having sat a median 20 px out."""
    assert _fly_marker(ResupplyPassWatch(), _APPROACH + [20] * 4, then_missing_s=3.0) == []


def test_a_marker_that_was_never_lined_up_is_not_a_pass():
    """First flight, 2026-10-13 19:51:52: the marker had just taken the steering
    597 px from the centre and blinked out for 0.6 s, 5 px further out. The
    climb-out started; the marker was back 0.9 s later, 100 px higher."""
    turning_in = [640, 630, 620, 612, 606, 600, 596] + [592] * 8 + [594, 597]
    # Without the condition this is the flight's failure: a pass is signed.
    assert len(_fly_marker(ResupplyPassWatch(lined_up_px=10_000), turning_in,
                           then_missing_s=3.0)) == 1
    assert _fly_marker(ResupplyPassWatch(), turning_in, then_missing_s=3.0) == []
    # Lined up first and then swept as far out, it is a pass.
    assert len(_fly_marker(ResupplyPassWatch(), _APPROACH + [80, 200, 400, 597],
                           then_missing_s=3.0)) == 1


def test_a_marker_far_out_but_not_moving_is_not_a_pass():
    assert _fly_marker(ResupplyPassWatch(), [80] * 24, then_missing_s=3.0) == []


def test_nothing_is_signed_before_the_marker_has_been_missing_long_enough():
    watch = ResupplyPassWatch(missing_s=0.5)
    assert _fly_marker(watch, _APPROACH + _SWEEP, then_missing_s=0.3) == []


def test_a_marker_that_comes_back_in_time_is_not_a_pass_and_a_later_pass_still_is():
    watch = ResupplyPassWatch(missing_s=0.5)
    blink = _APPROACH + _SWEEP[:2] + [None, None] + [20] * 8      # two missing ticks: 0.34 s
    assert _fly_marker(watch, blink) == []
    carry_on = 100.0 + len(blink) * 0.17
    assert len(_fly_marker(watch, _SWEEP, start=carry_on, then_missing_s=1.0)) == 1


def test_a_short_approach_is_not_a_pass():
    assert _fly_marker(ResupplyPassWatch(), [20] * 4 + _SWEEP, then_missing_s=3.0) == []


def test_a_marker_that_did_not_have_the_steering_is_not_a_pass():
    assert _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, steering=False,
                       then_missing_s=3.0) == []


def test_no_pass_while_the_pursuit_is_not_steering():
    """The dive recovery has the airframe, or a climb-out is already flying."""
    assert _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, active=False,
                       then_missing_s=3.0) == []


def test_the_pixel_limits_are_at_1200_px_of_frame_height():
    small = _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, height=600, then_missing_s=3.0)
    assert len(small) == 1 and small[0].off_centre_px == pytest.approx(160)


def test_a_marker_still_too_small_to_fly_through_is_not_a_pass():
    """2026-10-15 12:58:24: the marker dropped out 55 px from the centre, 40 px
    across; a pass was signed, no rearm came, and the climb-out broke the
    approach. The passes a rearm followed had it 53 to 67 px across."""
    assert _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, size=40,
                       then_missing_s=3.0) == []
    assert len(_fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, size=53,
                           then_missing_s=3.0)) == 1
    # The floor is at 1200 px of frame height, as the other limits are.
    assert _fly_marker(ResupplyPassWatch(), _APPROACH + _SWEEP, size=40, height=600,
                       then_missing_s=3.0) == []
    # At 0 the floor is off, the rule as it was.
    assert len(_fly_marker(ResupplyPassWatch(min_size_px=0), _APPROACH + _SWEEP, size=40,
                           then_missing_s=3.0)) == 1


def test_the_next_pass_needs_its_own_approach():
    watch = ResupplyPassWatch()
    assert len(_fly_marker(watch, _APPROACH + _SWEEP, then_missing_s=1.0)) == 1
    assert _fly_marker(watch, _SWEEP, start=103.0, then_missing_s=3.0) == []


def _emptied(confirm_reads=3):
    urgency = MissileUrgency(confirm_reads=confirm_reads)
    _observe(urgency, 2, count=confirm_reads)
    assert _observe(urgency, 0, terminal_zero=True, count=confirm_reads).empty
    return urgency


def test_the_first_count_above_zero_after_empty_shows_the_rearm_before_it_is_confirmed():
    """Operator, 2026-10-13: "immediate nose up ... after resupply". Measured
    over 52 rearms: the confirmation came a median 4.6 s after the marker was
    last seen, the first count above zero 1.5 s after it."""
    urgency = _emptied()
    first = _observe(urgency, 4, count=1)
    assert first.rearm_seen and not first.rearmed and first.empty
    second = _observe(urgency, 4, count=1)
    assert second.rearm_seen and not second.rearmed
    third = _observe(urgency, 4, count=1)
    assert third.rearmed and not third.empty
    after = _observe(urgency, 4, count=1)
    assert not after.rearm_seen and not after.rearmed


@pytest.mark.parametrize("misread", [9, 10, 14, 18, 19, 47])
def test_a_count_no_rack_carries_does_not_show_a_rearm(misread):
    """The single reads that jumped up and straight back on 2026-10-11 and 10-13."""
    assert not _observe(_emptied(), misread, count=1).rearm_seen


def test_a_count_that_goes_back_to_zero_stops_showing_a_rearm():
    urgency = _emptied()
    assert _observe(urgency, 4, count=1).rearm_seen
    back = _observe(urgency, 0, terminal_zero=True, count=1)
    assert not back.rearm_seen and not back.rearmed and back.empty


def test_a_count_above_the_confirmed_one_shows_a_rearm_only_while_seeking():
    urgency = MissileUrgency(confirm_reads=3)
    _observe(urgency, 4, count=3)
    assert _observe(urgency, 2, count=3).missiles_spent == 2
    assert not _observe(urgency, 4, resupply_seeking=False, count=1).rearm_seen
    seeking = _observe(urgency, 4, resupply_seeking=True, count=1)
    assert seeking.rearm_seen and not seeking.rearmed
    # The count it already had is no rearm.
    urgency = MissileUrgency(confirm_reads=3)
    _observe(urgency, 4, count=3)
    assert not _observe(urgency, 4, resupply_seeking=True, count=1).rearm_seen


def _fired_down_after_a_switch_away():
    """2026-10-25 18:17:41-58, acct3: the rocket pod switched away on false zeros
    (rack_emptied records its 0), then fired down one read per value on the
    approach to a resupply it could see."""
    urgency = MissileUrgency(confirm_reads=3)
    _observe(urgency, 4, "primary", count=3)
    _observe(urgency, 18, "secondary", count=1)
    urgency.rack_emptied("secondary")
    states = [urgency.observe(ammo, "secondary", resupply_seeking=True, read_id=read_id)
              for read_id, ammo in enumerate((28, 25, 21, 17, 13, 9, 5), start=1)]
    return urgency, states


def test_a_rack_being_fired_down_is_not_a_rearm():
    """18:17:58: the read of 5 after 9 started the climb-out with the marker 48 px
    across, a second from the pad, and the aircraft flew off it with 0/38 and 0/4."""
    _urgency, states = _fired_down_after_a_switch_away()
    assert not any(s.rearm_seen or s.rearmed for s in states)


def test_a_fired_down_count_that_holds_does_not_confirm_a_rearm():
    urgency, _states = _fired_down_after_a_switch_away()
    held = [urgency.observe(5, "secondary", resupply_seeking=True, read_id=read_id)
            for read_id in (8, 9, 10)]
    assert not any(s.rearm_seen or s.rearmed for s in held)


def test_a_stray_zero_between_fired_counts_does_not_make_the_next_one_a_rearm():
    urgency = MissileUrgency(confirm_reads=3)
    _observe(urgency, 0, terminal_zero=True, count=3)
    for read_id, ammo in enumerate((13, 9, 0, 5), start=1):
        state = urgency.observe(ammo, "secondary", resupply_seeking=True, read_id=read_id)
    assert not state.rearm_seen


def test_a_misread_that_jumps_up_and_back_to_zero_does_not_hold_off_the_rearm():
    """The empty-rack spikes of 2026-10-11 and 10-13 (9 to 47) went straight back to 0."""
    urgency = _emptied()
    _observe(urgency, 47, count=1)
    _observe(urgency, 0, terminal_zero=True, count=3)
    first = _observe(urgency, 4, count=1)
    assert first.rearm_seen
    assert _observe(urgency, 4, count=2).rearmed


def test_an_unreadable_read_inside_a_rearm_does_not_hold_off_its_confirmation():
    urgency = _emptied()
    assert _observe(urgency, 4, count=1).rearm_seen
    urgency.observe(None, "secondary")
    assert _observe(urgency, 4, count=3).rearmed


def test_an_unreadable_count_shows_nothing():
    urgency = _emptied()
    assert not urgency.observe(None, "secondary").rearm_seen


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


# ---------------------------------------------------------------------------
# Behind terrain (operator, 2026-10-16): "when resupply is that color it means its
# behind terrain, in that case it should ignore that target". In view the icon's
# disc is filled dark; behind terrain it is a duller outline and glyph with the
# ground showing through. Cut from the operator's screenshots of 23:34:49 and
# 23:34:50, the aircraft flying at a mountain face it was drawn on.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["resupply_neg_behind_terrain.png",
                                  "resupply_neg_behind_terrain_near_ring.png"])
def test_a_resupply_icon_behind_terrain_is_not_returned(name):
    behind = []
    assert find_resupply_marker(_live_crop_frame(name, (900, 600)), occluded=behind) is None
    if name == "resupply_neg_behind_terrain.png":
        assert len(behind) == 1 and behind[0].fill < 0.5


@pytest.mark.parametrize("name, origin", [
    ("resupply_pos_ring_near.png", (827, 760)), ("resupply_pos_ring_wide.png", (840, 360)),
    ("resupply_pos_haze.png", (561, 208)), ("resupply_pos_far.png", (1093, 536))])
def test_a_resupply_icon_in_view_is_its_dark_fill_inside(name, origin):
    # 0.88 through haze, which lightens part of the disc; 0.96 and more otherwise.
    assert find_resupply_marker(_live_crop_frame(name, origin)).fill >= 0.8


def test_an_icon_in_view_wins_over_a_larger_one_behind_terrain():
    frame = _live_crop_frame("resupply_neg_behind_terrain.png", (300, 300))
    far = cv2.imread(str(FIXTURES / "resupply_pos_far.png"))
    frame[536:536 + far.shape[0], 1093:1093 + far.shape[1]] = far
    behind = []
    marker = find_resupply_marker(frame, occluded=behind)
    assert marker is not None and marker.x == pytest.approx(1173, abs=3)
    assert len(behind) == 1


def test_the_pursuit_logs_a_resupply_icon_behind_terrain(monkeypatch, caplog):
    from test_pursuit_mode import _make_ctrl
    ctrl = _make_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        behind = []
        find_resupply_marker(_live_crop_frame("resupply_neg_behind_terrain.png", (900, 600)),
                             occluded=behind)
        ctrl._note_objective_behind_terrain(behind, "RESUPPLY", "resupply marker")
        ctrl._note_objective_behind_terrain(behind, "RESUPPLY", "resupply marker")
        ctrl._note_objective_behind_terrain(behind, "AIRSUP", "control point")
    lines = [r.getMessage() for r in caplog.records if "behind terrain" in r.getMessage()]
    assert [m.split(":")[0] for m in lines] == ["RESUPPLY", "AIRSUP"]   # one each, rate-limited


def test_the_pursuit_passes_the_occluded_list_to_the_resupply_scan():
    import inspect
    from wingman.controller import Controller
    src = inspect.getsource(Controller.pursue_and_engage)
    assert "occluded=resupply_behind)" in src
    assert '"RESUPPLY", "resupply marker")' in src


# ADR 171: mission_j20's way down to where ACS flies.

def _descent(max_s=90.0):
    return ControlledDescent(target_alt_m=3000, angle_deg=-20, tolerance_deg=5,
                             level_angle_deg=-10, max_s=max_s, now=100.0, min_pull_kph=450)


def test_a_shallow_nose_gets_a_pulse_down():
    assert _descent().update(100.0, 6000.0, -2.0, 1.0) == "down"


def test_the_nose_in_the_band_gets_nothing():
    assert _descent().update(100.0, 6000.0, -22.0, 1.0) == "hold"


def test_a_nose_past_the_tolerance_gets_a_pulse_up():
    assert _descent().update(100.0, 6000.0, -26.0, 1.0) == "up"


def test_one_pulse_per_new_sample():
    """The angle lands about every 3 s; a second pulse on one sample is how the
    first live pursuit went to -36 deg against a -20 deg limit (2026-09-26 01:58)."""
    descent = _descent()
    assert descent.update(100.0, 6000.0, -2.0, 1.0) == "down"
    assert descent.update(100.2, 6000.0, -2.0, 1.0) == "hold"
    assert descent.update(103.0, 5900.0, -3.0, 2.0) == "down"


def test_at_the_altitude_it_levels_off_before_it_ends():
    """Operator, 2026-11-01: "descend altitude level out and attack". Down at -20
    deg with the altitude reached, the chase has not been handed anything yet."""
    descent = _descent()
    assert descent.update(100.0, 2990.0, -20.0, 1.0) == "up"
    assert descent.ended is None
    assert descent.update(101.0, 2900.0, -14.0, 2.0) == "up"
    assert descent.update(102.0, 2850.0, -9.0, 3.0) is None
    assert descent.ended == "level at 2850 m"


def test_the_level_off_is_not_undone_by_coming_back_through_the_altitude():
    descent = _descent()
    descent.update(100.0, 2950.0, -20.0, 1.0)
    assert descent.update(101.0, 3050.0, -15.0, 2.0) == "up"


def test_no_fresh_altitude_presses_nothing_and_holds_the_chase_off():
    """The search it used to hand to is safe from here; the chase is not, and an
    aircraft that cannot read its height at 6000 m must not start one."""
    descent = _descent()
    assert descent.update(100.0, None, None, None) == "hold"
    assert descent.ended is None


def test_no_angle_presses_nothing_but_the_descent_goes_on():
    assert _descent().update(100.0, 6000.0, None, 1.0) == "hold"


def test_the_descent_ends_on_its_bound():
    descent = _descent(max_s=90.0)
    assert descent.update(189.9, 3500.0, -20.0, 1.0) == "hold"
    assert descent.update(190.0, 3500.0, -20.0, 2.0) is None
    assert descent.ended == "time limit at 3500 m"


def test_the_bound_holds_with_no_altitude_too():
    descent = _descent(max_s=90.0)
    assert descent.update(190.0, None, None, None) is None
    assert descent.ended == "time limit"


def test_a_descent_that_ended_stays_ended():
    descent = _descent()
    descent.update(100.0, 2900.0, -5.0, 1.0)
    assert descent.update(101.0, 6000.0, -2.0, 2.0) is None
    assert descent.ended == "level at 2900 m"



def test_a_slow_aircraft_is_not_pulled_up():
    """2026-11-01 17:49:33: UP pulses under about 330 KPH, and the path ran from -7 to
    -77 deg. Slow, a steep path is a stalling wing and a pull adds drag."""
    descent = _descent()
    assert descent.update(100.0, 6000.0, -60.0, 1.0, 240.0) == "hold"
    assert descent.pulls_withheld == 1


def test_the_pull_comes_back_with_the_speed():
    descent = _descent()
    assert descent.update(100.0, 6000.0, -60.0, 1.0, 240.0) == "hold"
    assert descent.update(102.0, 5800.0, -60.0, 2.0, 600.0) == "up"


def test_a_slow_aircraft_may_still_be_put_nose_down():
    """Down is how it gets its speed back."""
    assert _descent().update(100.0, 6000.0, 0.0, 1.0, 240.0) == "down"


def test_the_level_off_waits_for_the_speed_too():
    descent = _descent()
    assert descent.update(100.0, 2900.0, -20.0, 1.0, 300.0) == "hold"
    assert descent.ended is None
    assert descent.update(102.0, 2800.0, -20.0, 2.0, 700.0) == "up"


def test_an_unknown_speed_does_not_hold_the_pull_back():
    assert _descent().update(100.0, 6000.0, -60.0, 1.0, None) == "up"

from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.resupply import (
    MissilePriority,
    ResupplyMarkerMemory,
    MissileUrgency,
    find_resupply_marker,
    resupply_preempts,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("name, expected", [
    ("MISSILE_EMPTY_RESUPPLY.png", (752, 624)),
    ("MISSILE_EMPTY_RESUPPLY2.png", (1395, 459)),
])
def test_real_resupply_capture_finds_the_yellow_marker(name, expected):
    frame = cv2.imread(str(FIXTURES / name))
    assert frame is not None

    marker = find_resupply_marker(frame)

    assert marker is not None
    assert marker.x == pytest.approx(expected[0], abs=4)
    assert marker.y == pytest.approx(expected[1], abs=4)


def _yellow_bgr():
    return tuple(int(channel) for channel in cv2.cvtColor(
        np.uint8([[[28, 220, 255]]]), cv2.COLOR_HSV2BGR)[0, 0])


def test_solid_yellow_hud_patch_and_small_yellow_glyph_are_rejected():
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    yellow = _yellow_bgr()
    cv2.rectangle(frame, (700, 500), (770, 570), yellow, -1)
    cv2.rectangle(frame, (900, 600), (909, 609), yellow, -1)

    assert find_resupply_marker(frame) is None


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

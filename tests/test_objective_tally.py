"""How many objectives wingman flew through in a round (operator, 2026-10-09).

"at round end it prints how many air superiority targets, resupply, and
priority targets are captured ... it should not read the score bar, only track
when wingman flies through the targets."

The game draws a marker larger the nearer it is. An air superiority point was
flown through when its marker reached a large size and then disappeared
(operator, the same day). The priority target was when its marker was last
seen at close range and ahead of the nose, and then gone. The resupply point
was when the pursuit confirms a rearm, and its marker decides nothing.
"""

import logging

import pytest

from wingman.objective_tally import (
    AIR_SUPERIORITY,
    CENTRE_PX,
    LOST_S,
    NEAR_PX,
    PRIORITY_TARGET,
    REFRACTORY_S,
    RESUPPLY,
    ObjectiveTally,
)


def _approach(tally, kind, sizes, start=100.0, step=0.15, off_centre=None):
    """One sighting per scan, the disc `sizes` px across in turn and, when
    given, `off_centre` px from the screen centre in turn. Returns the time of
    the last sighting."""
    now = start
    for index, size in enumerate(sizes):
        tally.see(kind, size, now, 0.0 if off_centre is None else off_centre[index])
        assert tally.tick(now) == [], "nothing is lost while the marker is in view"
        now += step
    return now - step


def test_a_marker_followed_to_close_range_and_then_gone_was_flown_through(caplog):
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [22, 25, 30, 38, 47, 58])

        assert tally.tick(last + LOST_S - 0.1) == [], "not gone long enough yet"
        assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]

    assert tally.counts() == {AIR_SUPERIORITY: 1, RESUPPLY: 0, PRIORITY_TARGET: 0}
    assert caplog.messages == [
        "OBJECTIVE: flew through an air superiority point: its marker reached 58 px "
        "across and was gone, last seen at 58 px and 0 px off the centre (1 this round)"]


def test_a_marker_lost_while_still_small_was_not_flown_through(caplog):
    """Turned away from, hidden by terrain, or taken by someone else far off."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.DEBUG, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [20, 21, 22, 22, 23])

        assert tally.tick(last + LOST_S) == []

    assert tally.counts()[AIR_SUPERIORITY] == 0
    assert "never reached close range (40 px)" in caplog.text and "largest 23 px" in caplog.text


def test_an_air_superiority_point_counts_wherever_on_the_screen_its_marker_went(caplog):
    """Operator, 2026-10-09: "the evidence for airsuperiority marker fly through
    should be based on if the marker reached large size then disappeared." A
    point flown through leaves by the edge of the screen, and one that is taken
    turns blue and stops being found wherever it is."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60],
                         off_centre=[40, 150, 300, 480, 560])

        assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]

    assert caplog.messages == [
        "OBJECTIVE: flew through an air superiority point: its marker reached 60 px "
        "across and was gone, last seen at 60 px and 560 px off the centre (1 this round)"]


def test_an_air_superiority_marker_that_reached_large_size_counts_whatever_it_was_last(caplog):
    """Reached is the largest it was seen at, not the size it went at."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [30, 45, 55, 40, 30, 26])

        assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]

    assert "reached 55 px across and was gone, last seen at 26 px" in caplog.text


def test_a_two_scan_control_point_read_in_a_deathmatch_round_is_not_counted(caplog):
    """2026-10-09 06:30:47, a round with no control points: a red disc was read
    for two scans in 0.2 s, 32 px across and 40 px off the centre."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.DEBUG, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [32, 32], step=0.2, off_centre=[40, 40])

        assert tally.tick(last + LOST_S) == []

    assert tally.counts()[AIR_SUPERIORITY] == 0
    assert "never reached close range" in caplog.text


def test_a_marker_passed_beside_at_close_range_was_not_flown_through(caplog):
    """2026-10-09 05:56:42, the first live session: the crown was 51 px across
    when last seen and was counted, and it had slid to the left edge as the
    aircraft went past it: 440, 500, 548 and 562 px from the screen centre on
    its last four sightings."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.DEBUG, logger="wingman.objective_tally"):
        last = _approach(tally, PRIORITY_TARGET, [30, 36, 42, 45, 48, 51],
                         off_centre=[150, 260, 440, 500, 548, 562])

        assert tally.tick(last + LOST_S) == []

    assert tally.counts()[PRIORITY_TARGET] == 0
    assert "passed beside it" in caplog.text and "562 px off the centre" in caplog.text


def test_a_marker_ahead_of_the_nose_to_the_end_was(caplog):
    """The same session, 06:00:28: the crown the aircraft flew at was 35 px
    across and 55 px from the centre when last seen."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, PRIORITY_TARGET, [22, 26, 30, 33, 35],
                         off_centre=[140, 120, 90, 70, 55])

        assert tally.tick(last + LOST_S) == [PRIORITY_TARGET]

    assert "35 px across and 55 px off the centre at last sight" in caplog.text


def test_where_the_crown_was_earlier_in_the_approach_does_not_matter():
    """A turn onto the crown starts with it at the edge."""
    tally = ObjectiveTally()
    last = _approach(tally, PRIORITY_TARGET, [16, 20, 26, 32, 36],
                     off_centre=[700, 520, 300, 120, 40])

    assert tally.tick(last + LOST_S) == [PRIORITY_TARGET]


def test_ahead_ends_at_centre_px_for_the_crown():
    for off, expected in ((CENTRE_PX, [PRIORITY_TARGET]), (CENTRE_PX + 1, [])):
        tally = ObjectiveTally()
        last = _approach(tally, PRIORITY_TARGET, [60] * 5, off_centre=[off] * 5)
        assert tally.tick(last + LOST_S) == expected, off


def test_for_the_crown_the_last_sighting_decides_not_the_largest():
    """Close, then far again, then gone: the aircraft went past and away from
    it, and the marker it lost was the far one."""
    tally = ObjectiveTally()
    last = _approach(tally, PRIORITY_TARGET, [20, 32, 38, 30, 22, 18])

    assert tally.tick(last + LOST_S) == []


@pytest.mark.parametrize("kind", [AIR_SUPERIORITY, PRIORITY_TARGET])
def test_one_large_blob_for_one_scan_is_not_an_approach(kind):
    tally = ObjectiveTally()
    tally.see(kind, 60, 100.0)

    assert tally.tick(100.0 + LOST_S) == []
    assert tally.counts()[kind] == 0


def test_nor_is_a_marker_seen_for_under_half_a_second():
    """Four scans, 0.45 s: a flicker at close range, not a marker followed in."""
    tally = ObjectiveTally()
    last = _approach(tally, PRIORITY_TARGET, [60, 60, 60, 60])

    assert tally.tick(last + LOST_S) == []


def test_a_missed_scan_or_two_does_not_end_the_approach():
    """The gap is shorter than LOST_S, so it is one approach and one fly-through."""
    tally = ObjectiveTally()
    _approach(tally, PRIORITY_TARGET, [16, 20, 24], start=100.0)
    last = _approach(tally, PRIORITY_TARGET, [28, 33, 38], start=100.9)

    assert tally.tick(last + LOST_S) == [PRIORITY_TARGET]
    assert tally.counts()[PRIORITY_TARGET] == 1


def test_the_disc_showing_again_as_the_aircraft_passes_is_not_a_second_one():
    tally = ObjectiveTally()
    last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60], start=100.0)
    assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]
    again = _approach(tally, AIR_SUPERIORITY, [52, 55, 58, 60, 62], start=last + LOST_S + 0.5)

    assert tally.tick(again + LOST_S) == []
    assert tally.counts()[AIR_SUPERIORITY] == 1


def test_the_next_point_counts_once_the_first_is_behind():
    """"It should fly through A then proceed with B.\""""
    tally = ObjectiveTally()
    last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60], start=100.0)
    tally.tick(last + LOST_S)
    later = last + LOST_S + REFRACTORY_S
    last = _approach(tally, AIR_SUPERIORITY, [20, 25, 30, 42, 55], start=later)

    assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]
    assert tally.counts()[AIR_SUPERIORITY] == 2


def test_each_kind_has_its_own_close_range():
    """The crown's disc is smaller than a control point's, so one size cannot
    serve both. The resupply point has none: its marker decides nothing."""
    assert NEAR_PX[PRIORITY_TARGET] < NEAR_PX[AIR_SUPERIORITY]
    assert set(NEAR_PX) == {AIR_SUPERIORITY, PRIORITY_TARGET}
    for kind in (AIR_SUPERIORITY, PRIORITY_TARGET):
        tally = ObjectiveTally()
        below = NEAR_PX[kind] - 1
        last = _approach(tally, kind, [below] * 5)
        assert tally.tick(last + LOST_S) == [], kind
        last = _approach(tally, kind, [NEAR_PX[kind]] * 5, start=200.0)
        assert tally.tick(last + LOST_S) == [kind], kind


def test_a_marker_in_view_when_the_pursuit_ends_was_not_flown_through():
    """A death at the point is not a capture."""
    tally = ObjectiveTally()
    last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60])
    tally.drop_approaches()

    assert tally.tick(last + LOST_S) == []
    assert tally.counts()[AIR_SUPERIORITY] == 0


def test_the_round_line_names_the_three_in_the_operators_order():
    tally = ObjectiveTally()
    for start in (100.0, 120.0):
        last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60], start=start)
        tally.tick(last + LOST_S)
    tally.note_rearm(140.0)

    assert tally.round_line() == (
        "ROUND OBJECTIVES — flown through: air superiority points 2, "
        "resupply 1, priority targets 0")


def test_a_rearm_is_the_resupply_points_fly_through(caplog):
    """2026-10-09 06:04:01, the first live session: the resupply marker was
    followed for 8.5 s and was last seen 60 px off the centre and 25 px across,
    too small for the marker's rule of that morning, which counted nothing. The
    rearm was confirmed 3.7 s later."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, RESUPPLY, [24] * 54 + [25], step=8.5 / 54,
                         off_centre=[90] * 54 + [60])
        assert tally.tick(last + LOST_S) == []

        tally.note_rearm(last + 3.7)

    assert tally.counts() == {AIR_SUPERIORITY: 0, RESUPPLY: 1, PRIORITY_TARGET: 0}
    assert caplog.messages == [
        "OBJECTIVE: flew through the resupply point, rearm confirmed, its marker last "
        "seen 3.7 s before, 25 px across and 60 px off the centre (1 this round)"]


@pytest.mark.parametrize("size, off", [(56, 46), (51, 8)])
def test_the_resupply_marker_lost_close_and_ahead_is_not_a_fly_through(size, off):
    """2026-10-09 06:13:20 and 06:15:10, the session on `centre_px`: the marker
    was last seen 56 px across and 46 px off the centre, then 51 px and 8 px.
    The marker's rule counted both, no rearm followed either, and after the
    first the pursuit was following the marker again 6 s later. The round line
    read `resupply 2 (0 rearms confirmed)`."""
    tally = ObjectiveTally()
    last = _approach(tally, RESUPPLY, [30, 38, 46, 52, size], off_centre=[120, 90, 70, 50, off])

    assert tally.tick(last + LOST_S) == []
    assert tally.tick(last + 60.0) == []
    assert tally.counts()[RESUPPLY] == 0
    assert "resupply 0," in tally.round_line()


def test_each_rearm_is_one_fly_through():
    tally = ObjectiveTally()
    tally.note_rearm(100.0)
    tally.note_rearm(160.0)

    assert tally.counts()[RESUPPLY] == 2
    assert "resupply 2," in tally.round_line()


def test_a_rearm_with_no_marker_seen_says_so(caplog):
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        tally.note_rearm(100.0)

    assert caplog.messages == [
        "OBJECTIVE: flew through the resupply point, rearm confirmed, its marker not "
        "seen this round (1 this round)"]


def test_the_rearm_count_does_not_carry_into_the_next_round(caplog):
    tally = ObjectiveTally()
    _approach(tally, RESUPPLY, [30, 40, 50])
    tally.note_rearm(103.0)
    assert "resupply 1," in tally.end_round()

    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        tally.note_rearm(200.0)

    assert tally.counts()[RESUPPLY] == 1
    assert "its marker not seen this round (1 this round)" in caplog.text


def test_a_rearm_makes_the_round_one_with_a_line_to_print():
    tally = ObjectiveTally()
    tally.note_rearm(100.0)

    assert tally.end_round() == (
        "ROUND OBJECTIVES — flown through: air superiority points 0, "
        "resupply 1, priority targets 0")


def test_the_end_of_the_round_gives_the_line_and_starts_the_next_count():
    tally = ObjectiveTally()
    last = _approach(tally, PRIORITY_TARGET, [16, 19, 22, 30, 36])
    tally.tick(last + LOST_S)

    assert tally.end_round().endswith("priority targets 1")
    assert tally.counts() == {AIR_SUPERIORITY: 0, RESUPPLY: 0, PRIORITY_TARGET: 0}
    assert tally.end_round() is None, "the lobby after the end screen prints nothing"


def test_a_round_with_a_pursuit_and_no_objective_still_prints_its_zeros():
    tally = ObjectiveTally()
    tally.note_round_activity()

    assert tally.end_round() == (
        "ROUND OBJECTIVES — flown through: air superiority points 0, "
        "resupply 0, priority targets 0")


def test_a_round_with_no_pursuit_prints_nothing():
    assert ObjectiveTally().end_round() is None


@pytest.mark.parametrize("kind", [AIR_SUPERIORITY, PRIORITY_TARGET])
def test_an_approach_cut_short_by_the_round_does_not_carry_into_the_next(kind):
    tally = ObjectiveTally()
    last = _approach(tally, kind, [70, 70, 70, 70, 70])
    tally.end_round()

    assert tally.tick(last + LOST_S) == []
    assert tally.counts()[kind] == 0


def test_the_main_loop_prints_the_line_at_the_rounds_end():
    """The end screen is the round's end; the lobby is the second chance for a
    round whose end screen was never read (`end_round` then prints nothing
    twice). The replay lanes run this pass with a fake controller."""
    from pathlib import Path

    src = Path("wingman/main.py").read_text(encoding="utf-8")
    block = src[src.index("for prev_game_state, new_game_state in state_changes.drain():"):]
    block = block[:block.index("waiting_fallback.on_state_change")]

    assert "if new_game_state in (GameState.GAME_END_B, GameState.GAME_LOBBY):" in block
    assert "ctrl.log_round_objectives()" in block


def test_the_close_range_sizes_come_from_the_config():
    """`pursuit_mode.objective_tally.near_px`: a kind left out keeps its default,
    and the setting survives the end of a round."""
    tally = ObjectiveTally(near_px={AIR_SUPERIORITY: 80})
    for start in (100.0, 200.0):
        last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60], start=start)
        assert tally.tick(last + LOST_S) == [], "60 px is not close when 80 is asked for"
        tally.note_round_activity()
        tally.end_round()
    last = _approach(tally, PRIORITY_TARGET, [16, 19, 22, 30, 36], start=300.0)
    assert tally.tick(last + LOST_S) == [PRIORITY_TARGET]


def test_how_far_ahead_reaches_comes_from_the_config():
    tally = ObjectiveTally(centre_px=600)
    last = _approach(tally, PRIORITY_TARGET, [30, 36, 42, 45, 48, 51],
                     off_centre=[150, 260, 440, 500, 548, 562])

    assert tally.tick(last + LOST_S) == [PRIORITY_TARGET]


def test_the_shipped_config_sets_the_two_sizes():
    from pathlib import Path

    import yaml

    from wingman.config_schema import schema_default

    shipped = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))
    near = shipped["pursuit_mode"]["objective_tally"]["near_px"]
    assert set(near) == {AIR_SUPERIORITY, PRIORITY_TARGET}
    for kind, value in near.items():
        assert value == schema_default("pursuit_mode.objective_tally.near_px." + kind)
        assert NEAR_PX[kind] == value
    centre = shipped["pursuit_mode"]["objective_tally"]["centre_px"]
    assert centre == schema_default("pursuit_mode.objective_tally.centre_px") == CENTRE_PX

"""How many objectives wingman flew through in a round (operator, 2026-10-09).

"at round end it prints how many air superiority targets, resupply, and
priority targets are captured ... it should not read the score bar, only track
when wingman flies through the targets."

The rule: a marker followed until it was last seen at close range, and then
gone, was flown through. The game draws a marker larger the nearer it is.
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
        "OBJECTIVE: flew through an air superiority point, 58 px across and 0 px "
        "off the centre at last sight (1 this round)"]


def test_a_marker_lost_while_still_small_was_not_flown_through(caplog):
    """Turned away from, hidden by terrain, or taken by someone else far off."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.DEBUG, logger="wingman.objective_tally"):
        last = _approach(tally, AIR_SUPERIORITY, [20, 21, 22, 22, 23])

        assert tally.tick(last + LOST_S) == []

    assert tally.counts()[AIR_SUPERIORITY] == 0
    assert "not at close range" in caplog.text


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
    """The same session, 05:55:43: the resupply point a rearm followed was 53 px
    across and 61 px from the centre when last seen, 61 to 110 px over its last
    second."""
    tally = ObjectiveTally()
    with caplog.at_level(logging.INFO, logger="wingman.objective_tally"):
        last = _approach(tally, RESUPPLY, [40, 44, 48, 50, 52, 53],
                         off_centre=[66, 41, 107, 110, 47, 61])

        assert tally.tick(last + LOST_S) == [RESUPPLY]

    assert "53 px across and 61 px off the centre at last sight" in caplog.text


def test_where_the_marker_was_earlier_in_the_approach_does_not_matter():
    """A turn onto the objective starts with it at the edge."""
    tally = ObjectiveTally()
    last = _approach(tally, AIR_SUPERIORITY, [24, 30, 40, 50, 60],
                     off_centre=[700, 520, 300, 120, 40])

    assert tally.tick(last + LOST_S) == [AIR_SUPERIORITY]


def test_ahead_ends_at_centre_px():
    for off, expected in ((CENTRE_PX, [AIR_SUPERIORITY]), (CENTRE_PX + 1, [])):
        tally = ObjectiveTally()
        last = _approach(tally, AIR_SUPERIORITY, [60] * 5, off_centre=[off] * 5)
        assert tally.tick(last + LOST_S) == expected, off


def test_the_last_sighting_decides_not_the_largest():
    """Close, then far again, then gone: the aircraft went past and away from
    it, and the marker it lost was the far one."""
    tally = ObjectiveTally()
    last = _approach(tally, RESUPPLY, [30, 45, 55, 40, 30, 26])

    assert tally.tick(last + LOST_S) == []


def test_one_large_blob_for_one_scan_is_not_an_approach():
    tally = ObjectiveTally()
    tally.see(PRIORITY_TARGET, 60, 100.0)

    assert tally.tick(100.0 + LOST_S) == []
    assert tally.counts()[PRIORITY_TARGET] == 0


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
    """The crown's disc is the smallest of the three and the resupply point's
    the largest, so one size cannot serve all."""
    assert NEAR_PX[PRIORITY_TARGET] < NEAR_PX[AIR_SUPERIORITY] < NEAR_PX[RESUPPLY]
    for kind in (AIR_SUPERIORITY, RESUPPLY, PRIORITY_TARGET):
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
    last = _approach(tally, RESUPPLY, [24, 30, 40, 50, 60], start=140.0)
    tally.tick(last + LOST_S)
    tally.note_rearm()

    assert tally.round_line() == (
        "ROUND OBJECTIVES — flown through: air superiority points 2, "
        "resupply 1 (1 rearm confirmed), priority targets 0")


def test_a_confirmed_rearm_is_shown_beside_the_resupply_count_not_added_to_it():
    """The ammo count going up is the check on the fly-through rule, so the two
    are kept apart."""
    tally = ObjectiveTally()
    tally.note_rearm()
    tally.note_rearm()

    assert tally.counts()[RESUPPLY] == 0
    assert tally.rearms() == 2
    assert "resupply 0 (2 rearms confirmed)" in tally.round_line()


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
        "resupply 0 (0 rearms confirmed), priority targets 0")


def test_a_round_with_no_pursuit_prints_nothing():
    assert ObjectiveTally().end_round() is None


@pytest.mark.parametrize("kind", [AIR_SUPERIORITY, RESUPPLY, PRIORITY_TARGET])
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


def test_the_shipped_config_sets_the_three_sizes():
    from pathlib import Path

    import yaml

    from wingman.config_schema import schema_default

    shipped = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))
    near = shipped["pursuit_mode"]["objective_tally"]["near_px"]
    assert set(near) == {AIR_SUPERIORITY, RESUPPLY, PRIORITY_TARGET}
    for kind, value in near.items():
        assert value == schema_default("pursuit_mode.objective_tally.near_px." + kind)
        assert NEAR_PX[kind] == value
    centre = shipped["pursuit_mode"]["objective_tally"]["centre_px"]
    assert centre == schema_default("pursuit_mode.objective_tally.centre_px") == CENTRE_PX

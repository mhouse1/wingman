"""ADR 163 / ADR 165: the pursuit trial's arm, drawn once per round.

The round's first engagement inside a pursuit draws the arm, and every later one
in the round takes it, so each round carries one arm. The engagement is the
session summary's own (alerts no more than 3 s apart), so each engagement the
summary counts carries the round's arm.
"""

import pytest

from wingman import mission_stats
from wingman.evade_trial import VOLLEY_GROUP_S, EvadeTrial


class _Draws:
    """random.Random's `random()`, returning the values given, in order. Raises
    once they run out, so a draw nobody expected fails the test."""

    def __init__(self, *values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0)


def test_the_grouping_is_the_session_summarys():
    assert VOLLEY_GROUP_S == mission_stats._VOLLEY_GROUP_S


def test_a_round_draws_its_arm_on_its_first_engagement_and_keeps_it():
    trial = EvadeTrial(0.5, rng=_Draws(0.2))
    trial.new_round()
    assert trial.on_alert(100.0, in_pursuit=True) == ("evade", True)       # 0.2 < 0.5
    assert trial.on_alert(101.5, in_pursuit=True) == ("evade", False)      # same volley
    assert trial.on_alert(160.0, in_pursuit=True) == ("evade", True)       # same round: no draw
    assert (trial.engagements, trial.rounds) == (2, 1)


def test_the_next_round_draws_again():
    trial = EvadeTrial(0.5, rng=_Draws(0.2, 0.9))
    trial.new_round()
    assert trial.on_alert(100.0, in_pursuit=True) == ("evade", True)
    trial.new_round()
    assert trial.on_alert(400.0, in_pursuit=True) == ("control", True)     # 0.9 >= 0.5
    assert trial.on_alert(460.0, in_pursuit=True) == ("control", True)
    assert (trial.engagements, trial.rounds) == (3, 2)


def test_a_new_round_closes_the_open_engagement():
    """An alert just after a round starts opens an engagement of its own."""
    trial = EvadeTrial(1.0)
    trial.on_alert(100.0, in_pursuit=True)
    trial.new_round()
    assert trial.on_alert(101.0, in_pursuit=True) == ("evade", True)
    assert trial.rounds == 2


def test_a_gap_of_exactly_the_volley_window_is_still_one_engagement():
    trial = EvadeTrial(1.0)
    trial.on_alert(100.0, in_pursuit=True)
    assert trial.on_alert(100.0 + VOLLEY_GROUP_S, in_pursuit=True) == ("evade", False)
    assert trial.on_alert(100.0 + 2 * VOLLEY_GROUP_S + 0.01, in_pursuit=True) == ("evade", True)


def test_an_engagement_that_opens_outside_a_pursuit_has_no_arm_and_draws_none():
    trial = EvadeTrial(0.5, rng=_Draws(0.9))
    trial.new_round()
    assert trial.on_alert(100.0, in_pursuit=False) == (None, True)
    # The pursuit starts before its next alert: still the same engagement, still no arm.
    assert trial.on_alert(102.0, in_pursuit=True) == (None, False)
    assert (trial.engagements, trial.rounds) == (0, 0)
    # The round's first engagement inside a pursuit draws it.
    assert trial.on_alert(160.0, in_pursuit=True) == ("control", True)
    # And one outside a pursuit later in the round still has none.
    assert trial.on_alert(220.0, in_pursuit=False) == (None, True)
    assert (trial.engagements, trial.rounds) == (1, 1)


@pytest.mark.parametrize("share, arm", [(0.0, None), (1.0, "evade")])
def test_the_ends_of_the_share(share, arm):
    trial = EvadeTrial(share, rng=_Draws(0.999999))
    assert trial.on_alert(100.0, in_pursuit=True) == (arm, True)
    assert trial.enabled is (share > 0)


@pytest.mark.parametrize("share", [-0.1, 1.5])
def test_a_share_outside_zero_to_one_is_refused(share):
    with pytest.raises(ValueError):
        EvadeTrial(share)


def test_a_real_draw_gives_both_arms_across_rounds():
    trial = EvadeTrial(0.5)
    arms = set()
    for i in range(200):
        trial.new_round()
        arms.add(trial.on_alert(100.0 + 10 * i, in_pursuit=True)[0])
    assert arms == {"evade", "control"}

"""ADR 172: in a share of rounds, two agreeing terrain warnings do not break off
the approach to the resupply marker."""

import pathlib

import pytest
import yaml

from wingman.agreement_trial import RESUPPLY, AgreementTrial
from wingman.config_schema import schema_default


class _Draws:
    def __init__(self, *values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0)


def test_each_round_draws_its_arm():
    trial = AgreementTrial(0.5, rng=_Draws(0.2, 0.7, 0.49))
    assert [trial.new_round() for _ in range(3)] == ["holdoff", "override", "holdoff"]
    assert trial.rounds == 3


def test_share_zero_draws_nothing_and_breaks_every_holdoff():
    trial = AgreementTrial(0.0)
    assert trial.new_round() is None and trial.rounds == 0
    assert trial.agreement_breaks_holdoff(RESUPPLY)


def test_a_holdoff_round_keeps_only_the_resupply_holdoff():
    trial = AgreementTrial(1.0)
    trial.new_round()
    assert not trial.agreement_breaks_holdoff(RESUPPLY)
    for other in ("target", "control point", "priority target", None):
        assert trial.agreement_breaks_holdoff(other)


def test_an_override_round_is_cycle_25_as_it_was():
    trial = AgreementTrial(0.5, rng=_Draws(0.9))
    trial.new_round()
    assert trial.agreement_breaks_holdoff(RESUPPLY)


def test_before_the_first_round_nothing_is_held_off():
    """A battle joined before any round start (a restart mid-round) flies cycle 25."""
    assert AgreementTrial(1.0).agreement_breaks_holdoff(RESUPPLY)


@pytest.mark.parametrize("share", [-0.1, 1.5])
def test_a_share_outside_0_to_1_is_refused(share):
    with pytest.raises(ValueError):
        AgreementTrial(share)


def test_the_resupply_name_is_the_pursuits():
    """The pursuit reports the marker under this name (_note_pursuit_objective)."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "wingman" / "controller.py").read_text(
        encoding="utf-8")
    assert '"resupply marker"' in src


def test_the_schema_keeps_the_trial_off_and_the_shipped_config_runs_it():
    assert schema_default("behavior_tree.climb.warning_agreement.resupply_holdoff_share") == 0.0
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "wingman" / "config.yaml")
                         .read_text(encoding="utf-8"))
    assert cfg["behavior_tree"]["climb"]["warning_agreement"]["resupply_holdoff_share"] == 0.5

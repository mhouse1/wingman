"""Code Review 019 cycle 25 (operator, 2026-10-18: "yes"): two terrain warnings
that agree start the emergency climb.

On 2026-10-17 (130 missions, 145 kill-card crashes) two of the planner's ESCAPE,
LOOM and SHAPE within 1 s of each other came 1 to 6 s before 34% of the crashes,
about twice a mission, a crash following 16% of the alarms; most failed
recoveries had their climb start under 2 s before the impact.
"""

import logging

import pytest

from tests.test_behavior_tree import FakeClock, make_snap
from wingman.behavior_tree import make_climb_condition
from wingman.warning_agreement import WarningAgreement


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# -- the tracker ------------------------------------------------------------------

def test_two_kinds_within_the_window_agree_and_hold():
    clock = _Clock()
    a = WarningAgreement(window_s=1.0, hold_s=3.0, clock=clock)
    assert a.note("LOOM") is False
    clock.t += 0.6
    assert a.note("ESCAPE") is True
    assert a.agreeing()
    clock.t += 2.9
    assert a.agreeing(), "held for hold_s"
    clock.t += 0.2
    assert not a.agreeing(), "let go after hold_s"


def test_the_same_kind_twice_does_not_agree():
    clock = _Clock()
    a = WarningAgreement(clock=clock)
    a.note("ESCAPE")
    clock.t += 0.3
    assert a.note("ESCAPE") is False
    assert not a.agreeing()


def test_kinds_further_apart_than_the_window_do_not_agree():
    clock = _Clock()
    a = WarningAgreement(window_s=1.0, clock=clock)
    a.note("SHAPE")
    clock.t += 1.5
    assert a.note("LOOM") is False


def test_one_warning_line_per_agreement_episode(caplog):
    clock = _Clock()
    a = WarningAgreement(window_s=1.0, hold_s=3.0, clock=clock)
    with caplog.at_level(logging.WARNING, logger="wingman.warning_agreement"):
        a.note("LOOM")
        clock.t += 0.2
        a.note("ESCAPE")
        clock.t += 0.2
        a.note("ESCAPE")                  # the same episode
        clock.t += 10.0
        a.note("SHAPE")
        clock.t += 0.5
        a.note("LOOM")                    # a new one
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and a.agreements == 2
    assert lines[0].startswith("TERRAIN WARNINGS AGREE: ESCAPE and LOOM within 1.0 s")


def test_an_unknown_warning_is_refused():
    with pytest.raises(ValueError):
        WarningAgreement().note("SKY")


# -- the climb condition ------------------------------------------------------------

def _cond(**kw):
    opts = dict(confirm_reads=1, clock=FakeClock(), agreement_enabled=True, agreement_shadow=False)
    opts.update(kw)
    return make_climb_condition(500, 1000, **opts)


def test_agreeing_warnings_are_a_hard_emergency_high_above_the_band():
    cond = _cond()
    assert cond(make_snap(altitude=5000.0, terrain_warnings_agree=True)) is True
    assert cond.emergency_active and cond.hard_emergency_active


def test_in_shadow_they_are_logged_and_not_flown(caplog):
    cond = _cond(agreement_shadow=True)
    with caplog.at_level(logging.WARNING, logger="wingman.behavior_tree"):
        assert cond(make_snap(altitude=5000.0, terrain_warnings_agree=True)) is False
    assert not cond.hard_emergency_active
    assert any("TERRAIN WARNINGS AGREE" in r.getMessage() and "SHADOW" in r.getMessage()
               for r in caplog.records)


def test_disabled_they_change_nothing():
    cond = _cond(agreement_enabled=False)
    assert cond(make_snap(altitude=5000.0, terrain_warnings_agree=True)) is False
    assert not cond.hard_emergency_active


def test_the_emergency_ends_when_the_agreement_does():
    cond = _cond()
    cond(make_snap(altitude=5000.0, terrain_warnings_agree=True))
    cond(make_snap(altitude=5000.0, terrain_warnings_agree=False))
    assert not cond.hard_emergency_active


def test_the_shipped_config_flies_it():
    import pathlib
    import yaml
    cfg = yaml.safe_load((pathlib.Path(__file__).resolve().parents[1] / "wingman" / "config.yaml")
                         .read_text(encoding="utf-8"))
    agree = cfg["behavior_tree"]["climb"]["warning_agreement"]
    assert agree["enabled"] is True and agree["shadow"] is False


def test_in_shadow_an_agreement_is_logged_but_not_live(caplog):
    """Live 2026-10-18 07:13:40: with the trigger in shadow the controller still
    set aside its objective hold-off on an agreement."""
    clock = _Clock()
    a = WarningAgreement(clock=clock, shadow=True)
    with caplog.at_level(logging.WARNING, logger="wingman.warning_agreement"):
        a.note("LOOM")
        clock.t += 0.3
        a.note("SHAPE")
    assert a.agreeing() and not a.live()
    assert "SHADOW - not actuating" in caplog.records[0].getMessage()

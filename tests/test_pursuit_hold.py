"""ADR 181: in a `hold` round nothing takes the airframe from a chase that has its
target in view.

Operator, 2026-11-15: "when pursuing a target it stays on pursuit, regardless of
terrain signal or incoming missile". What is pinned here, with a real Controller:

- the round's arm is drawn by `hold_target.share`, logged and tagged;
- "holds its target" means a hold round, a pursuit running, and the target (not a
  resupply marker or an objective) with the steering within `view_s`;
- while it holds: no crash recovery on agreeing warnings or a severe pull-out
  alarm, a running recovery is handed back, no flare roll, no missile evade, no
  terrain turn;
- in an `evade` round, and with the rule off, each of those acts as before.
"""

import logging
import random
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests import test_flare_roll as fr
from tests import test_mission_stats as ms
from tests.test_pursuit_recovery import _Analyzer, _crash_ctrl, _stop, _wait
from wingman.analyzer import GameState
from wingman.config_schema import schema_default, validate_config
from wingman.controller import ROLL_RIGHT_KEY
from wingman.pursuit_hold import EVADE, HOLD, PursuitHoldTrial
from wingman.tick_handlers import BehaviorTreeHandler
from wingman.warning_agreement import WarningAgreement

_CFG = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())


# -- the arm ------------------------------------------------------------------------

def test_the_arm_is_drawn_by_the_share():
    assert PursuitHoldTrial(1.0).new_round() == HOLD
    trial = PursuitHoldTrial(0.5, rng=random.Random(3))
    arms = [trial.new_round() for _ in range(200)]
    assert 70 <= arms.count(HOLD) <= 130 and set(arms) == {HOLD, EVADE}
    assert trial.rounds == 200


def test_share_zero_is_off_and_a_bad_share_is_refused():
    trial = PursuitHoldTrial(0.0)
    assert not trial.enabled and trial.new_round() is None and trial.arm is None
    with pytest.raises(ValueError):
        PursuitHoldTrial(1.5)


def test_the_shipped_config_holds_in_every_round_and_the_default_is_off():
    assert validate_config(_CFG) == []
    assert _CFG["pursuit_mode"]["hold_target"] == {"share": 1.0, "view_s": 1.0}
    assert schema_default("pursuit_mode.hold_target.share") == 0.0


def _hold_ctrl(monkeypatch, share=1.0, analyzer=None, **extra):
    """A controller in a drawn round, inside a pursuit."""
    ctrl = _crash_ctrl(monkeypatch, analyzer or _Analyzer(altitude=900.0, angle=-25.0),
                       hold_target={"share": share, "view_s": 1.0}, **extra)
    ctrl._pursuit_hold_trial._rng = random.Random(1)
    ctrl.pursuit_hold_new_round()
    ctrl._pursuing.set()
    return ctrl


def test_the_round_start_draws_logs_and_tags_the_arm(monkeypatch, caplog):
    ctrl = _crash_ctrl(monkeypatch, _Analyzer(), hold_target={"share": 1.0})
    notes = []
    ctrl.set_round_note(lambda name, ts: notes.append(name))
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.pursuit_hold_new_round()
    assert notes == ["hold_arm_hold"]
    assert "PURSUIT HOLD: round 1 — hold arm (ADR 181, share 1.00)" in [
        r.getMessage() for r in caplog.records]


def test_with_the_rule_off_the_round_start_notes_nothing(monkeypatch):
    ctrl = _crash_ctrl(monkeypatch, _Analyzer())        # no hold_target block: share 0
    notes = []
    ctrl.set_round_note(lambda name, ts: notes.append(name))
    ctrl.pursuit_hold_new_round()
    assert notes == [] and not ctrl.pursuit_holds_target()


def test_the_arm_tags_the_round_for_the_summary(tmp_path):
    t = ms._tracker(tmp_path)
    t._startup_done = True
    ms._enter_battle(t, ts=100.0)
    t.on_event("hold_arm_hold", 100.1)
    ms._leave_battle(t, ts=400.0)
    ms._enter_battle(t, ts=500.0)
    t.on_event("hold_arm_evade", 500.1)
    ms._leave_battle(t, ts=800.0)
    first, second = t.finalize()["missions"]
    assert first["tags"] == {"hold_target": "hold"}
    assert second["tags"] == {"hold_target": "evade"}


# -- what "holds its target" means --------------------------------------------------

def test_it_holds_only_for_a_target_with_the_steering_in_a_hold_round(monkeypatch):
    ctrl = _hold_ctrl(monkeypatch)
    assert not ctrl.pursuit_holds_target()              # nothing in view yet
    ctrl._note_pursuit_objective("target")
    assert ctrl.pursuit_holds_target()
    for other in ("resupply marker", "control point", "priority target"):
        ctrl._note_pursuit_objective(other)
        assert not ctrl.pursuit_holds_target(), other
    ctrl._note_pursuit_objective("target")
    ctrl._pursuit_objective_ts = time.time() - 1.2       # out of view for over view_s
    assert not ctrl.pursuit_holds_target()
    ctrl._note_pursuit_objective("target")
    ctrl._pursuing.clear()                              # no pursuit running
    assert not ctrl.pursuit_holds_target()


def test_an_evade_round_never_holds(monkeypatch):
    ctrl = _hold_ctrl(monkeypatch, share=1e-9)
    assert ctrl._pursuit_hold_trial.arm == EVADE
    ctrl._note_pursuit_objective("target")
    assert not ctrl.pursuit_holds_target()


# -- terrain warnings ---------------------------------------------------------------

def _agree(ctrl, hold_s=None):
    agreement = WarningAgreement(hold_s=hold_s) if hold_s else WarningAgreement()
    agreement.note("LOOM")
    agreement.note("ESCAPE")
    ctrl.set_warning_agreement(agreement)


def _withheld(caplog):
    return [r.getMessage() for r in caplog.records if "PURSUIT HOLD:" in r.getMessage()
            and "withheld" in r.getMessage()]


def test_agreeing_warnings_do_not_start_the_recovery_while_it_holds(monkeypatch, caplog):
    ctrl = _hold_ctrl(monkeypatch)
    _agree(ctrl)
    ctrl._note_pursuit_objective("target")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
        ctrl.climb_mode(emergency=True)                 # the tree asks again every tick
    assert not ctrl.is_climbing()
    assert _withheld(caplog) == [
        "PURSUIT HOLD: crash recovery (two terrain warnings agree) withheld — the chase "
        "stays on its target (ADR 181)"], "logged once, not once per tick"
    assert ctrl._hold_target_withheld == {"crash recovery (two terrain warnings agree)": 1}


def test_a_severe_pull_out_alarm_does_not_start_it_either(monkeypatch, caplog):
    ctrl = _hold_ctrl(monkeypatch)
    ctrl._pullout_bypass_below_m = 300.0
    ctrl.set_pullout_lowest_m(200.0)
    ctrl._note_pursuit_objective("target")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
    assert not ctrl.is_climbing()
    assert _withheld(caplog) == [
        "PURSUIT HOLD: crash recovery (a pull-out would bottom out at 200 m) withheld — "
        "the chase stays on its target (ADR 181)"]


def test_in_an_evade_round_agreeing_warnings_still_start_the_recovery(monkeypatch, caplog):
    ctrl = _hold_ctrl(monkeypatch, share=1e-9)
    _agree(ctrl)
    ctrl._note_pursuit_objective("target")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
    try:
        assert ctrl.is_climbing()
        assert not _withheld(caplog)
    finally:
        _stop(ctrl)


@pytest.mark.parametrize("objective", ["resupply marker", "control point"])
def test_a_resupply_or_objective_run_is_still_broken_off(monkeypatch, objective):
    """The rule is for a target. Everything else keeps its protection."""
    ctrl = _hold_ctrl(monkeypatch)
    _agree(ctrl)
    ctrl._note_pursuit_objective(objective)
    ctrl.climb_mode(emergency=True)
    try:
        assert ctrl.is_climbing()
    finally:
        _stop(ctrl)


def test_with_nothing_in_view_the_recovery_flies_as_before(monkeypatch):
    ctrl = _hold_ctrl(monkeypatch)
    ctrl.climb_mode(emergency=True)
    try:
        assert _wait(ctrl.pursuit_recovery_active)
    finally:
        _stop(ctrl)


def test_a_target_coming_into_view_takes_a_running_recovery_back(monkeypatch, caplog):
    """Without the rule the agreement keeps the recovery when a goal comes into
    view (Code Review 019 cycle 25). In a hold round the target takes it back."""
    ctrl = _hold_ctrl(monkeypatch)
    _agree(ctrl, hold_s=30.0)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
        try:
            assert _wait(ctrl.pursuit_recovery_active)
            ctrl._note_pursuit_objective("target")
            assert _wait(lambda: not ctrl.is_climbing(), timeout=3.0), \
                "the recovery kept the airframe from a target in view"
        finally:
            _stop(ctrl)
    assert any("the pursuit has the target in view after" in r.getMessage()
               for r in caplog.records)


def test_a_terrain_turn_is_not_started_while_it_holds():
    calls, notes = [], []
    ctrl = SimpleNamespace(
        pursuit_holds_target=lambda: True,
        note_hold_target_withheld=notes.append,
        boundary_turn_mode=lambda **kw: calls.append(kw))
    handler = SimpleNamespace(_terrain_turn_side="left", _terrain=None, _ctrl=ctrl)
    BehaviorTreeHandler._start_terrain_turn(handler)
    assert calls == [] and notes == ["terrain turn"]
    ctrl.pursuit_holds_target = lambda: False
    BehaviorTreeHandler._start_terrain_turn(handler)
    assert calls == [{"max_s": 3.0, "lateral": 1.0}]


# -- incoming missiles --------------------------------------------------------------

def _roll_ctrl(monkeypatch, share=1.0):
    ctrl = fr._ctrl(monkeypatch, state=GameState.GAME_BATTLE_EJECT)
    ctrl._pursuit_hold_trial = PursuitHoldTrial(share, rng=random.Random(1))
    ctrl.pursuit_hold_new_round()
    ctrl._pursuing.set()
    return ctrl


def test_no_flare_roll_starts_while_it_holds(monkeypatch, caplog):
    ctrl = _roll_ctrl(monkeypatch)
    ctrl._note_pursuit_objective("target")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        assert ctrl.flare_roll(threading.Event()) is False
    assert not ctrl.is_flare_rolling() and fr._roll_intents(ctrl) == []
    assert any("flare roll not started — the chase stays on its target (ADR 181)"
               in r.getMessage() for r in caplog.records)


def test_a_flare_roll_ends_when_the_target_comes_into_view(monkeypatch):
    ctrl = _roll_ctrl(monkeypatch)
    try:
        assert ctrl.flare_roll(threading.Event()) is True      # nothing in view: it rolls
        assert ctrl.is_flare_rolling()
        ctrl._note_pursuit_objective("target")
        assert fr._wait(lambda: not ctrl.is_flare_rolling(), timeout=2.0)
        assert [(a, k) for a, k, _ in fr._roll_intents(ctrl)] == [
            ("key_press", ROLL_RIGHT_KEY), ("key_release", ROLL_RIGHT_KEY)]
    finally:
        ctrl._flare_roll.stop()


def test_the_flare_roll_still_flies_in_an_evade_round_and_on_a_resupply_run(monkeypatch):
    ctrl = _roll_ctrl(monkeypatch, share=1e-9)
    ctrl._note_pursuit_objective("target")
    try:
        assert ctrl.flare_roll(threading.Event()) is True
    finally:
        fr._stop(ctrl)
    ctrl = _roll_ctrl(monkeypatch)
    ctrl._note_pursuit_objective("resupply marker")
    try:
        assert ctrl.flare_roll(threading.Event()) is True
    finally:
        fr._stop(ctrl)


def test_no_missile_evade_starts_while_it_holds(monkeypatch, caplog):
    ctrl = _roll_ctrl(monkeypatch)
    ctrl._note_pursuit_objective("target")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.missile_evade_mode()
    assert not ctrl.is_missile_evading()
    assert _withheld(caplog) == ["PURSUIT HOLD: missile evade withheld — the chase stays "
                                 "on its target (ADR 181)"]

"""Operator, 2026-11-21: in a share of rounds the chase holds the airbrake while
the aim point is far off the crosshair at gun range, to turn tighter onto it.

The decision and the measure on the real TurnBrake; the airbrake on a real
Controller with real TelemetrySnapshots, as tests/test_closing_brake.py does.
"""

import random

import pytest
import yaml
from pathlib import Path

from wingman.turn_brake import TurnBrake
from wingman.target_range import RangeReading, TargetRangeReader

from test_approach_brake import _airbrake, _ctrl, clock  # noqa: F401  (clock is a fixture)


def _brake(arm="brake", **kw):
    args = dict(share=1.0, on_err=0.25, off_err=0.12, max_km=1.5, max_hold_s=2.5,
                cooldown_s=1.5, on_target_err=0.1)
    args.update(kw)
    b = TurnBrake(**args, rng=random.Random(1))
    b.new_round()
    b.arm = arm
    return b


def test_the_arm_is_drawn_per_round_and_the_share_is_checked():
    b = TurnBrake(0.5, 0.25, 0.12, 1.5, 2.5, 1.5, rng=random.Random(3))
    arms = {b.new_round() for _ in range(40)}
    assert arms == {"brake", "control"} and b.rounds == 40
    assert TurnBrake(0.0, 0.25, 0.12, 1.5, 2.5, 1.5).new_round() is None
    with pytest.raises(ValueError):
        TurnBrake(1.5, 0.25, 0.12, 1.5, 2.5, 1.5)
    with pytest.raises(ValueError):
        TurnBrake(0.5, 0.1, 0.2, 1.5, 2.5, 1.5)


def test_a_brake_round_brakes_on_a_far_aim_point_until_it_comes_in():
    b = _brake()
    assert not b.update(0.2, 0.1, 0.9, 10.0)          # under on_err
    assert b.update(-0.3, 0.05, 0.9, 10.2)            # far off on x
    assert b.update(0.15, 0.13, 0.9, 10.4)            # still over off_err
    assert not b.update(0.05, 0.11, 0.9, 10.6)        # in
    assert b.why_off == "aim within 0.11"
    assert not _brake("control").update(-0.6, 0.0, 0.9, 10.0)


@pytest.mark.parametrize("err,err_y,km,why", [
    (0.5, 0.0, 1.6, "no target near"),                # past max_km
    (0.5, 0.0, None, "no target near"),               # no fresh range
    (None, None, 0.9, "no target"),                   # the target does not steer
])
def test_a_far_target_or_none_ends_the_hold(err, err_y, km, why):
    b = _brake()
    assert b.update(0.5, 0.0, 0.9, 10.0)
    assert not b.update(err, err_y, km, 10.2)
    assert b.why_off == why


def test_a_hold_is_capped_and_waits_before_the_next():
    b = _brake()
    assert b.update(0.5, 0.0, 0.9, 10.0)
    assert b.update(0.5, 0.0, 0.9, 12.4)
    assert not b.update(0.5, 0.0, 0.9, 12.5)          # 2.5 s
    assert b.why_off == "held 2.5 s"
    assert not b.update(0.5, 0.0, 0.9, 13.9)          # cooling down
    assert b.update(0.5, 0.0, 0.9, 14.1)


def test_the_measure_counts_each_picture_once_and_times_the_catch_by_arm():
    for arm in ("brake", "control"):
        b = _brake(arm)
        b.update(0.4, 0.0, 0.9, 10.0, picture_ts=1.0)     # off: the catch starts
        b.update(0.4, 0.0, 0.9, 10.1, picture_ts=1.0)     # the same picture
        b.update(0.2, 0.0, 0.9, 10.5, picture_ts=1.5)
        b.update(0.05, 0.0, 0.9, 11.0, picture_ts=2.0)    # in: caught after 1.0 s
        assert b.pictures[arm] == [3, 1]
        assert b.catches[arm] == [pytest.approx(1.0)]
        assert ("%s arm 1 of 3 pictures on target (33%%), 1 catches, median 1.0 s" % arm
                in b.summary())
    lost = _brake()
    lost.update(0.4, 0.0, 0.9, 10.0)
    lost.update(None, None, 0.9, 10.5)                   # lost before it came in
    lost.update(0.05, 0.0, 0.9, 11.0)
    assert lost.catches == {}


# -- the chase ----------------------------------------------------------------------

def _turn_ctrl(monkeypatch, speed=800, arm="brake"):
    import time
    ctrl, telemetry = _ctrl(monkeypatch, speed=speed)
    ctrl._turn_brake = _brake(arm)
    ctrl._turn_brake_min_kph = 800.0
    ctrl._turn_burner_below_kph = 600.0
    ctrl._target_range = TargetRangeReader()
    ctrl._gun_range_max_age_s = 1.5
    ctrl._gun_cluster = (900.0, 500.0)
    ctrl._target_range._latest = RangeReading(km=0.9, text="0.90km", cluster=(900.0, 500.0),
                                              ts=time.monotonic(), cost_ms=20.0)
    return ctrl, telemetry


def test_the_goal_needs_the_target_steering_a_far_aim_and_the_speed(monkeypatch):
    ctrl, telemetry = _turn_ctrl(monkeypatch, speed=800)
    assert ctrl._turn_brake_goal(True, 0.1, 0.0, 1.0) is None
    assert ctrl._turn_brake_goal(True, 0.4, 0.0, 1.1) == "tight turn"
    assert ctrl._turn_brake_goal(False, 0.4, 0.0, 1.2) is None      # an objective steers
    assert ctrl._turn_brake.why_off == "no target"
    ctrl._turn_brake._no_hold_before = 0.0
    import time
    telemetry.set(700, time.time())                    # between the two: neither
    ctrl._turn_burn_wanted = False
    assert ctrl._turn_brake_goal(True, 0.4, 0.0, 1.3) is None
    assert not ctrl._turn_burn_wanted
    telemetry.set(450, time.time())                    # slow: the afterburner
    assert ctrl._turn_brake_goal(True, 0.4, 0.0, 1.4) is None
    assert ctrl._turn_burn_wanted
    ctrl._target_range.stop()


def test_the_afterburner_is_leased_for_a_slow_turn_and_given_back(monkeypatch, caplog):
    from wingman.keybindings import AFTERBURNER_KEY
    ctrl, _ = _turn_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._turn_burner(True)
        ctrl._turn_burner(True)
        ctrl._turn_burner(False, why="aim within 0.10")
    burner = [(r["action_type"], r["action"]) for r in ctrl.get_action_intents()
              if r.get("key") == AFTERBURNER_KEY]
    assert burner == [("key_press", "turn_burner"), ("key_press", "turn_burner"),
                      ("key_release", "turn_burner")]
    assert ctrl._turn_burner_count == 1 and not ctrl._turn_burner_held
    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("TURNBRAKE: afterburner on") for m in lines)
    assert "TURNBRAKE: afterburner released (aim within 0.10)" in lines
    ctrl._target_range.stop()


def test_an_airbrake_holder_or_a_takeover_outranks_the_turn_burner(monkeypatch):
    ctrl, _ = _turn_ctrl(monkeypatch)
    ctrl._approach_brake_held = True
    from wingman.keybindings import AFTERBURNER_KEY
    assert not ctrl._may_hold_key(AFTERBURNER_KEY, requester="turn_burner")
    ctrl._approach_brake_held = False
    assert ctrl._may_hold_key(AFTERBURNER_KEY, requester="turn_burner")
    monkeypatch.setattr(ctrl, "_manual_takeover_active", lambda: True)
    assert not ctrl._may_hold_key(AFTERBURNER_KEY, requester="turn_burner")
    ctrl._target_range.stop()


def test_the_airbrake_is_held_for_the_turn_and_let_go_after(monkeypatch, clock, caplog):  # noqa: F811
    ctrl, _ = _turn_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick("tight turn")
        clock[0] += 0.4
        ctrl._approach_brake_tick("tight turn")
        assert _airbrake(ctrl) == [("key_press", "approach_brake")]
        assert ctrl._approach_brake_held and ctrl._turn_brake_holds
        clock[0] += 0.6
        ctrl._turn_brake.why_off = "aim within 0.08"
        ctrl._approach_brake_tick(None)
    assert _airbrake(ctrl) == [("key_press", "approach_brake"), ("key_release", "approach_brake")]
    assert not ctrl._turn_brake_holds
    assert ctrl._turn_brake_count == 1 and ctrl._turn_brake_held_s == pytest.approx(1.0)
    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("TURNBRAKE: airbrake on") for m in lines)
    assert "SPEEDCAP: airbrake released (turn brake: aim within 0.08)" in lines
    ctrl._target_range.stop()


def test_the_turn_brake_hands_the_key_over_held(monkeypatch, clock, caplog):  # noqa: F811
    ctrl, _ = _turn_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick("tight turn")
        clock[0] += 0.3
        ctrl._approach_brake_tick("closing target")
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    assert ctrl._approach_brake_held and ctrl._closing_brake_holds
    assert not ctrl._turn_brake_holds
    assert "TURNBRAKE: airbrake handed to the closing target brake" in [
        r.getMessage() for r in caplog.records]
    ctrl._target_range.stop()


def test_a_manual_takeover_vetoes_the_turn_brake(monkeypatch, clock):  # noqa: F811
    ctrl, _ = _turn_ctrl(monkeypatch)
    monkeypatch.setattr(ctrl, "_manual_takeover_active", lambda: True)
    ctrl._approach_brake_tick("tight turn")
    assert _airbrake(ctrl) == [] and not ctrl._approach_brake_held
    ctrl._target_range.stop()


def test_the_round_draw_logs_and_tags_the_arm(monkeypatch, caplog):
    ctrl, _ = _turn_ctrl(monkeypatch)
    notes = []
    ctrl._round_note = lambda name, ts: notes.append(name)
    ctrl._turn_brake = TurnBrake(0.5, 0.25, 0.12, 1.5, 2.5, 1.5, rng=random.Random(3))
    with caplog.at_level("INFO", logger="wingman.controller"):
        for _ in range(40):
            ctrl.turn_brake_new_round()
    assert set(notes) == {"turn_arm_brake", "turn_arm_control"} and len(notes) == 40
    assert any("TURNBRAKE TRIAL: round 40" in r.getMessage() for r in caplog.records)
    ctrl._target_range.stop()


def test_the_shared_config_brakes_the_turn_in_half_the_rounds():
    from wingman.config_schema import schema_default
    assert schema_default("pursuit_mode.turn_brake.share") == 0.0
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    tb = cfg["pursuit_mode"]["turn_brake"]
    assert tb["share"] == 0.5
    assert tb["min_kph"] == 800 and tb["burner_below_kph"] == 600
    for key in ("on_err", "off_err", "max_km", "max_hold_s", "cooldown_s", "min_kph",
                "burner_below_kph", "on_target_err"):
        assert tb[key] == schema_default("pursuit_mode.turn_brake." + key)

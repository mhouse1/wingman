"""ADR 179: in a share of rounds the chase brakes when it closes fast on a
target that is near.

The decision and the tally on the real ClosingBrake; the airbrake on a real
Controller with real TelemetrySnapshots, as tests/test_approach_brake.py does.
"""

import random

import pytest
import yaml
from pathlib import Path

from wingman.closing_brake import ClosingBrake
from wingman.keybindings import AIRBRAKE_KEY
from wingman.target_range import RangeReading, TargetRangeReader

from test_approach_brake import _airbrake, _ctrl, clock  # noqa: F401  (clock is a fixture)


def _brake(arm="brake", **kw):
    args = dict(share=1.0 if arm == "brake" else 1e-9, on_km=1.5, on_mps=150.0, off_mps=50.0,
                max_hold_s=4.0, cooldown_s=3.0)
    args.update(kw)
    b = ClosingBrake(rng=random.Random(1), **args)
    assert b.new_round() == arm
    return b


def _feed(b, start_km, mps, t0=10.0, seconds=1.0, step=0.25):
    """Readings every `step` s of a range falling at `mps`; returns the last time."""
    t = t0
    while t <= t0 + seconds + 1e-9:
        b.note(start_km - mps * (t - t0) / 1000.0, t)
        t += step
    return t - step


# -- the trial ---------------------------------------------------------------------

def test_the_arm_is_drawn_per_round_and_the_share_is_checked():
    b = ClosingBrake(0.5, 1.5, 150, 50, 4, 3, rng=random.Random(3))
    arms = {b.new_round() for _ in range(40)}
    assert arms == {"brake", "control"} and b.rounds == 40
    off = ClosingBrake(0.0, 1.5, 150, 50, 4, 3)
    assert off.new_round() is None and not off.enabled
    with pytest.raises(ValueError):
        ClosingBrake(1.5, 1.5, 150, 50, 4, 3)


# -- the closing speed --------------------------------------------------------------

def test_the_closing_speed_is_the_ranges_fall():
    b = _brake()
    _feed(b, 1.4, 250.0)
    assert b.closing_mps() == pytest.approx(250.0, abs=1.0)
    opening = _brake()
    _feed(opening, 0.6, -100.0)
    assert opening.closing_mps() == pytest.approx(-100.0, abs=1.0)


def test_two_readings_or_a_short_span_give_no_closing_speed():
    b = _brake()
    b.note(1.2, 10.0)
    b.note(1.1, 10.2)
    assert b.closing_mps() is None
    b.note(1.0, 10.4)                   # three readings over 0.4 s: too short
    assert b.closing_mps() is None
    b.note(0.9, 10.6)
    assert b.closing_mps() is not None


# -- the decision -------------------------------------------------------------------

def test_a_brake_round_brakes_on_a_fast_closure_and_a_control_round_never():
    b = _brake("brake")
    t = _feed(b, 1.4, 250.0)
    assert b.wants(t + 0.1) is True
    c = _brake("control")
    t = _feed(c, 1.4, 250.0)
    assert c.wants(t + 0.1) is False


def test_a_slow_closure_and_a_far_target_do_not_brake():
    slow = _brake()
    t = _feed(slow, 1.2, 100.0)
    assert slow.wants(t + 0.1) is False
    far = _brake()
    t = _feed(far, 2.6, 300.0)          # 2.6 to 2.3 km: past on_km
    assert far.wants(t + 0.1) is False


def test_the_hold_ends_when_the_closure_slows_and_says_why():
    b = _brake()
    t = _feed(b, 1.4, 250.0)
    assert b.wants(t + 0.1)
    t = _feed(b, 1.15, 20.0, t0=t + 0.25, seconds=1.5)     # the range now barely falls
    assert b.wants(t + 0.1) is False
    assert b.why_off.startswith("closing at ")


def test_a_lost_target_ends_the_hold():
    b = _brake()
    t = _feed(b, 1.4, 250.0)
    assert b.wants(t + 0.1)
    assert b.wants(t + 1.2) is False
    assert b.why_off == "no fresh range"


def test_a_hold_is_capped_and_waits_before_the_next():
    b = _brake()
    t = _feed(b, 1.45, 200.0, seconds=0.75)
    start = t + 0.05
    assert b.wants(start)
    t = _feed(b, 1.30, 200.0, t0=t + 0.25, seconds=3.75)   # still closing fast, 4 s on
    assert b.wants(start + 4.0) is False
    assert b.why_off.startswith("held ")
    b.note(0.45, start + 4.2)
    b.note(0.40, start + 4.45)
    assert b.wants(start + 4.5) is False                    # the wait
    t = _feed(b, 0.9, 200.0, t0=start + 7.5, seconds=1.0)
    assert b.wants(t + 0.05) is True


def test_stop_ends_a_hold_in_hand():
    b = _brake()
    t = _feed(b, 1.4, 250.0)
    assert b.wants(t + 0.1)
    b.stop("the target no longer has the steering")
    assert b.why_off == "the target no longer has the steering"


# -- the measure --------------------------------------------------------------------

def test_encounters_under_1p2_km_are_timed_by_arm():
    b = _brake("brake")
    for t, km in ((10.0, 1.3), (10.5, 1.1), (11.0, 0.9), (11.5, 0.7), (12.0, 0.6), (13.0, 0.5)):
        b.note(km, t)                                       # 10.5 to 13.0: 2.5 s
    for t, km in ((20.0, 1.0), (20.5, 0.9)):                # two readings: not one
        b.note(km, t)
    for t, km in ((30.0, 1.1), (31.0, 0.8), (32.0, 0.6), (33.5, 0.4), (34.0, 0.5)):
        b.note(km, t)                                       # 4.0 s
    assert "brake arm 2 encounters under 1.2 km, median 4.0 s, 1 of 3 s or more" in b.summary()
    assert b.encounters == {"brake": [2.5, 4.0]}


def test_a_new_round_closes_the_encounter_under_the_old_arm():
    b = ClosingBrake(0.5, 1.5, 150, 50, 4, 3, rng=random.Random(0))
    first = b.new_round()
    for t, km in ((10.0, 1.0), (10.5, 0.9), (11.0, 0.8)):
        b.note(km, t)
    b.new_round()
    assert b.encounters == {first: [1.0]}


# -- the chase ----------------------------------------------------------------------

def _closing_ctrl(monkeypatch, speed=1100, arm="brake"):
    ctrl, telemetry = _ctrl(monkeypatch, speed=speed)
    ctrl._closing_brake = _brake(arm)
    ctrl._closing_brake_min_kph = 650.0
    return ctrl, telemetry


def test_the_goal_needs_the_target_steering_a_fast_closure_and_the_speed(monkeypatch):
    import wingman.controller as controller_module
    ctrl, telemetry = _closing_ctrl(monkeypatch, speed=1100)
    now = [100.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: now[0])
    assert ctrl._closing_brake_goal(True) is None                    # no readings yet
    now[0] = _feed(ctrl._closing_brake, 1.4, 250.0, t0=99.0) + 0.1
    assert ctrl._closing_brake_goal(True) == "closing target"
    assert ctrl._closing_brake_goal(False) is None                   # an objective steers
    assert ctrl._closing_brake.why_off == "the target no longer has the steering"
    now[0] = _feed(ctrl._closing_brake, 1.1, 250.0, t0=now[0] + 4.0) + 0.1
    telemetry.set(600, 105.0)                                        # under min_kph
    assert ctrl._closing_brake_goal(True) is None
    assert ctrl._closing_brake.why_off == "600 KPH"


def test_the_chase_notes_each_new_range_reading_once(monkeypatch):
    ctrl, _ = _closing_ctrl(monkeypatch)
    ctrl._target_range = TargetRangeReader()
    ctrl._gun_range_max_age_s = 1.5
    ctrl._gun_cluster = (900.0, 500.0)
    import time
    ts = time.monotonic()
    ctrl._target_range._latest = RangeReading(km=0.9, text="0.90km", cluster=(900.0, 500.0),
                                              ts=ts, cost_ms=20.0)
    ctrl._closing_brake_note()
    ctrl._closing_brake_note()
    assert list(ctrl._closing_brake._readings) == [(ts, 0.9)]
    ctrl._target_range.stop()


def test_the_airbrake_is_held_for_the_goal_and_let_go_after(monkeypatch, clock, caplog):  # noqa: F811
    ctrl, _ = _closing_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick("closing target")
        clock[0] += 0.2
        ctrl._approach_brake_tick("closing target")
        assert _airbrake(ctrl) == [("key_press", "approach_brake")]
        assert ctrl._approach_brake_held and ctrl._closing_brake_holds
        clock[0] += 0.8
        ctrl._closing_brake.why_off = "closing at 20 m/s"
        ctrl._approach_brake_tick(None)
    assert _airbrake(ctrl) == [("key_press", "approach_brake"), ("key_release", "approach_brake")]
    assert not ctrl._closing_brake_holds
    assert ctrl._closing_brake_count == 1 and ctrl._closing_brake_held_s == pytest.approx(1.0)
    lines = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("CLOSEBRAKE: airbrake on") for m in lines)
    assert "SPEEDCAP: airbrake released (closing at 20 m/s)" in lines


def test_the_closing_brake_works_with_the_approach_brake_switched_off(monkeypatch, clock):  # noqa: F811
    ctrl, _ = _closing_ctrl(monkeypatch)
    ctrl._approach_brake_cfg = dict(ctrl._approach_brake_cfg, enabled=False)
    ctrl._approach_brake_tick("closing target")
    assert ctrl._approach_brake_held
    ctrl._approach_brake_tick(None)
    assert not ctrl._approach_brake_held
    assert [a for a, _ in _airbrake(ctrl)] == ["key_press", "key_release"]


def test_a_manual_takeover_vetoes_the_closing_brake(monkeypatch, clock):  # noqa: F811
    ctrl, _ = _closing_ctrl(monkeypatch)
    monkeypatch.setattr(ctrl, "_manual_takeover_active", lambda: True)
    ctrl._approach_brake_tick("closing target")
    assert _airbrake(ctrl) == [] and not ctrl._approach_brake_held


def test_the_round_draw_logs_and_tags_the_arm(monkeypatch, caplog):
    ctrl, _ = _closing_ctrl(monkeypatch)
    notes = []
    ctrl._round_note = lambda name, ts: notes.append(name)
    ctrl._closing_brake = ClosingBrake(0.5, 1.5, 150, 50, 4, 3, rng=random.Random(3))
    with caplog.at_level("INFO", logger="wingman.controller"):
        for _ in range(40):
            ctrl.closing_brake_new_round()
    assert set(notes) == {"brake_arm_brake", "brake_arm_control"} and len(notes) == 40
    assert any("CLOSEBRAKE TRIAL: round 40" in r.getMessage() for r in caplog.records)


def test_the_shared_config_brakes_in_half_the_rounds():
    from wingman.config_schema import schema_default
    assert schema_default("pursuit_mode.closing_brake.share") == 0.0
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    cb = cfg["pursuit_mode"]["closing_brake"]
    assert cb["share"] == 0.5
    for key in ("on_km", "on_mps", "off_mps", "max_hold_s", "cooldown_s", "min_kph"):
        assert cb[key] == schema_default("pursuit_mode.closing_brake." + key)
    assert AIRBRAKE_KEY


def test_another_brake_takes_the_airbrake_over_without_letting_it_go(monkeypatch, clock, caplog):  # noqa: F811
    """Live 04:24:37: the closing brake held 0.2 s, then a fast dive's brake took
    the key. It was released, logged as "the target no longer has the steering",
    and pressed again in the same millisecond. Now it is handed over held. (The
    resupply brake stands in for the dive's here: the shared test settings arm
    it, and the handover does not depend on which brake takes the key.)"""
    ctrl, _ = _closing_ctrl(monkeypatch, speed=1159)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick("closing target")
        clock[0] += 0.2
        ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    assert ctrl._approach_brake_held and not ctrl._closing_brake_holds
    assert ctrl._closing_brake_held_s == pytest.approx(0.2)
    lines = [r.getMessage() for r in caplog.records]
    assert "CLOSEBRAKE: airbrake handed to the resupply brake" in lines
    assert not any("the target no longer has the steering" in m for m in lines)


def test_a_yield_names_itself_on_the_release(monkeypatch, clock, caplog):  # noqa: F811
    """Live 04:24:52: the flare roll took the aircraft and the closing brake let
    go, logged as "the target no longer has the steering". The pursuit loop now
    tells the brake why when it yields; the release says so."""
    import inspect
    from wingman.controller import Controller
    src = inspect.getsource(Controller.pursue_and_engage)
    i = src.index('self.release_tracking_holds(why=f"yield to {owner}")')
    assert 'self._closing_brake.stop(f"yield to {owner}")' in src[i:i + 300]
    ctrl, _ = _closing_ctrl(monkeypatch)
    import wingman.controller as controller_module
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: 100.0)
    _feed(ctrl._closing_brake, 1.4, 250.0, t0=99.0)
    with caplog.at_level("INFO", logger="wingman.controller"):
        assert ctrl._closing_brake_goal(True) == "closing target"
        ctrl._approach_brake_tick("closing target")
        ctrl._closing_brake.stop("yield to flare roll")
        clock[0] += 0.5
        ctrl._approach_brake_tick(None)
    assert "SPEEDCAP: airbrake released (yield to flare roll)" in [r.getMessage() for r in caplog.records]


def test_a_slow_dive_does_not_cut_the_closing_brake(monkeypatch, clock):  # noqa: F811
    """Live 04:37:29: in a -11 deg dive at 726 KPH, under the dive brake's 850 KPH
    cap, the dive goal took the turn and the closing brake let go. The closing
    goal is asked first now; while it wants the brake, the brake holds."""
    import inspect
    import wingman.controller as controller_module
    from wingman.controller import Controller
    src = inspect.getsource(Controller.pursue_and_engage)
    assert ("brake_goal = (self._closing_brake_goal(control_visible)\n"
            "                                              or self._dive_brake_goal())") in src
    ctrl, _ = _closing_ctrl(monkeypatch, speed=726)
    now = [100.0]
    monkeypatch.setattr(controller_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(ctrl, "_dive_brake_goal", lambda: "dive")
    _feed(ctrl._closing_brake, 1.2, 480.0, t0=99.0)
    for _ in range(4):
        goal = ctrl._closing_brake_goal(True) or ctrl._dive_brake_goal()
        ctrl._approach_brake_tick(goal)
        clock[0] += 0.2
    assert goal == "closing target"
    assert ctrl._approach_brake_held and ctrl._closing_brake_holds
    assert [a for a, _ in _airbrake(ctrl)] == ["key_press"]

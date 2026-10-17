"""Code Review 019 cycle 9: the airbrake on the approach to the resupply marker.

Operator, 2026-10-15: "it is also not making use of AIRBRAKE_KEY which in some
instances could have avoided flying too fast". Four of the five climb-out deaths
seen that day passed the marker at 1,134 to 1,299 KPH with cruise holding the
afterburner. The speed is read about every 3 s and the airbrake takes 500 to
600 KPH off in that time (ADR 159), so each fresh reading over the cap starts a
dose of braking instead of braking to a threshold.

Checked on a real Controller with real TelemetrySnapshots; the lease half on the
real Actuator with a mock keyboard, as tests/test_actuator.py does.
"""

import inspect
import unittest.mock as mock

import pytest

from wingman import controller as controller_module
from wingman.actuator import Actuator
from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY, AIRBRAKE_KEY
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

from test_pursuit_mode import _make_ctrl

_CFG = {"enabled": True, "resupply": True, "control_point": True, "crown": True,
        "cap_kph": 850.0, "target_kph": 800.0, "decel_kph_s": 150.0, "max_dose_s": 3.0}


class _Telemetry:
    """An analyzer that reads one speed, accepted at `ts`."""

    def __init__(self, speed, ts=100.0):
        self.set(speed, ts)

    def set(self, speed, ts):
        self._snap = TelemetrySnapshot(
            speed=TelemetrySignal(value=speed, ts=ts),
            altitude=TelemetrySignal(value=1500, ts=ts, rate=-60.0),
            taken_at_s=ts + 0.5)

    def get_telemetry(self):
        return self._snap


@pytest.fixture
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(controller_module.time, "time", lambda: now[0])
    return now


def _ctrl(monkeypatch, speed=1100, cfg=_CFG):
    telemetry = _Telemetry(speed)
    ctrl = _make_ctrl(monkeypatch, analyzer=telemetry)
    ctrl._approach_brake_cfg = dict(cfg)
    return ctrl, telemetry


def _airbrake(ctrl):
    return [(i["action_type"], i.get("action")) for i in ctrl.get_action_intents()
            if i.get("key") == AIRBRAKE_KEY]


def test_a_reading_over_the_cap_brakes_for_a_dose(monkeypatch, clock, caplog):
    ctrl, _ = _ctrl(monkeypatch, speed=1100)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    assert ctrl._approach_brake_until == pytest.approx(1000.0 + (1100 - 800) / 150)
    assert any(r.getMessage().startswith("SPEEDCAP: 1100 KPH over the 850 KPH cap")
               for r in caplog.records)
    clock[0] += 1.9
    ctrl._approach_brake_tick("resupply")                    # the same reading, mid-dose
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    clock[0] += 0.2                                    # 2.1 s: the dose is given
    ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl)[-1] == ("key_release", "approach_brake")
    assert not ctrl._approach_brake_held


def test_a_dose_is_capped_and_each_reading_starts_one_only(monkeypatch, clock):
    ctrl, _ = _ctrl(monkeypatch, speed=1600)
    ctrl._approach_brake_tick("resupply")
    assert ctrl._approach_brake_until == pytest.approx(1003.0)     # max_dose_s
    clock[0] += 3.1
    ctrl._approach_brake_tick("resupply")                    # the dose is over...
    ctrl._approach_brake_tick("resupply")                    # ...and the reading is old
    assert [a for a, _ in _airbrake(ctrl)] == ["key_press", "key_release"]


def test_a_reading_under_the_cap_does_not_brake(monkeypatch, clock):
    ctrl, _ = _ctrl(monkeypatch, speed=840)
    ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl) == [] and not ctrl._approach_brake_held


def test_a_fresh_reading_at_the_target_ends_a_dose_early(monkeypatch, clock):
    ctrl, telemetry = _ctrl(monkeypatch, speed=1250)
    ctrl._approach_brake_tick("resupply")                    # a 3 s dose
    clock[0] += 1.0
    telemetry.set(790, 103.0)                          # the next reading: slow enough
    ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl)[-1] == ("key_release", "approach_brake")


def test_a_fresh_reading_still_over_the_cap_extends_the_dose(monkeypatch, clock):
    ctrl, telemetry = _ctrl(monkeypatch, speed=1000)
    ctrl._approach_brake_tick("resupply")                    # 1.33 s
    clock[0] += 1.0
    telemetry.set(1100, 103.0)                         # a dive gained speed under the brake
    ctrl._approach_brake_tick("resupply")
    assert ctrl._approach_brake_until == pytest.approx(1001.0 + 2.0)
    assert [a for a, _ in _airbrake(ctrl)] == ["key_press"]       # one hold, not two


def test_the_brake_lets_go_when_the_marker_loses_the_steering(monkeypatch, clock):
    ctrl, _ = _ctrl(monkeypatch)
    ctrl._approach_brake_tick("resupply")
    ctrl._approach_brake_tick(None)
    assert _airbrake(ctrl)[-1] == ("key_release", "approach_brake")


@pytest.mark.parametrize("veto", ["takeover", "climb", "eject", "missile_evade", "ab_evade"])
def test_what_the_brake_yields_to(monkeypatch, clock, veto):
    ctrl, _ = _ctrl(monkeypatch)
    ctrl._approach_brake_tick("resupply")
    if veto == "takeover":
        monkeypatch.setattr(ctrl, "_manual_takeover_active", lambda: True)
    elif veto == "climb":
        ctrl._climbing.set()
    elif veto == "eject":
        ctrl._ejecting.set()
    elif veto == "missile_evade":
        ctrl._missile_evading.set()
    else:
        ctrl._ab_evade_until = clock[0] + 5.0         # ADR 128: a missile inbound
    ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl)[-1] == ("key_release", "approach_brake")
    clock[0] += 0.1
    ctrl._approach_brake_tick("resupply")                    # and it stays off
    assert not ctrl._approach_brake_held


def test_an_emergency_climbs_airbrake_is_left_to_the_climb(monkeypatch, clock):
    """ADR 137: the emergency climb holds the airbrake in a steep dive and lets it
    go itself. The key is not leased, so a release here would drop its hold."""
    ctrl, _ = _ctrl(monkeypatch)
    ctrl._approach_brake_tick("resupply")
    ctrl._climb_emergency_active = True
    ctrl._approach_brake_off("climb")
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    assert not ctrl._approach_brake_held


def test_the_climb_out_lets_the_brake_go(monkeypatch, clock):
    ctrl, _ = _ctrl(monkeypatch)
    ctrl._approach_brake_tick("resupply")
    ctrl._start_rearm_climb_out("marker passed", yielding=True)
    assert _airbrake(ctrl)[-1] == ("key_release", "approach_brake")


@pytest.mark.parametrize("goal", ["control point", "crown"])
def test_control_points_and_crowns_are_braked_for_too(monkeypatch, clock, caplog, goal):
    """Cycle 13: overnight 2026-10-15/16, 29 of 68 control-point fly-throughs were
    followed by a death within 10 s, 59 of the 68 over 850 KPH."""
    ctrl, _ = _ctrl(monkeypatch, speed=1100)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl._approach_brake_tick(goal)
    assert _airbrake(ctrl) == [("key_press", "approach_brake")]
    assert any(f"cap on the {goal} approach" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("goal, key", [("resupply", "resupply"),
                                       ("control point", "control_point"), ("crown", "crown")])
def test_each_objective_has_its_own_switch(monkeypatch, clock, goal, key):
    ctrl, _ = _ctrl(monkeypatch, speed=1100, cfg=dict(_CFG, **{key: False}))
    ctrl._approach_brake_tick(goal)
    assert _airbrake(ctrl) == []
    for other in ("resupply", "control point", "crown"):
        if other != goal:
            ctrl._approach_brake_tick(other)
            assert ctrl._approach_brake_held
            break


def test_off_unless_configured(monkeypatch, clock):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Telemetry(1300))
    ctrl._approach_brake_tick("resupply")                    # the schema's default: off
    assert _airbrake(ctrl) == []
    ctrl._approach_brake_cfg = dict(_CFG, enabled=False)
    for goal in ("resupply", "control point", "crown"):
        ctrl._approach_brake_tick(goal)
    assert _airbrake(ctrl) == []


def test_no_reading_no_brake(monkeypatch, clock):
    ctrl = _make_ctrl(monkeypatch, analyzer=None)
    ctrl._approach_brake_cfg = dict(_CFG)
    ctrl._approach_brake_tick("resupply")
    assert _airbrake(ctrl) == []


# ---------------------------------------------------------------------------
# Cruise: paused for the brake, not ended
# ---------------------------------------------------------------------------

def _cruise(fuel=95):
    c = Controller.__new__(Controller)
    c._cruise_ab_enabled = True
    c._cruise_ab_min_fuel_pct = 40.0
    c._cruise_ab_rearm_fuel_pct = 90.0
    c._cruise_ab_confirm_reads = 2
    c._cruise_ab_active = True
    c._cruise_ab_low_streak = 0
    c._climb_emergency_active = False
    c._approach_brake_held = False
    c._climb_key = mock.MagicMock()
    c._manual_takeover_active = mock.MagicMock(return_value=False)
    c._read_fuel_pct = mock.MagicMock(return_value=fuel)
    return c


def _burner_presses(c):
    return sum(1 for k in c._climb_key.call_args_list
               if k.args and k.args[0] == AFTERBURNER_KEY and k.kwargs.get("press"))


def test_cruise_pauses_for_the_brake_and_comes_back_after_it():
    """Yielding the usual way ends cruise until the fuel refills to 90%; a brake of
    a few seconds must not cost the rest of the fuel cycle's burn."""
    c = _cruise(fuel=60)                               # under rearm_fuel_pct
    c._approach_brake_held = True
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert _burner_presses(c) == 0 and c._cruise_ab_active
    c._approach_brake_held = False
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert _burner_presses(c) == 1 and c._cruise_ab_active


def test_cruise_yields_to_the_brake_in_the_policy_table():
    c = _cruise()
    assert c._may_hold_key(AFTERBURNER_KEY, "cruise")
    c._approach_brake_held = True
    assert not c._may_hold_key(AFTERBURNER_KEY, "cruise")
    assert c._may_hold_key(AFTERBURNER_KEY, "afterburner_evade")   # the evade does not


def test_the_brake_lifts_cruises_burner_at_once(monkeypatch):
    """ADR 151 D4: the burner comes up when the brake goes on, not on cruise's
    next tick, up to 1.5 s later, with both keys down cancelling each other."""
    kb = mock.MagicMock()
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    act = Actuator(lambda: controller_module.keyboard_module,
                   lambda what: controller_module._may_inject(what),
                   leased_keys=(AFTERBURNER_KEY,))
    monkeypatch.setattr(controller_module, "_actuator", act)
    c = Controller.__new__(Controller)
    c._simulate_os_input = False
    c._analyzer = None
    c._climb_emergency_active = False
    c._approach_brake_held = False
    act.set_hold_policy(c._owner_may_hold)
    c._climb_key(AFTERBURNER_KEY, press=True, action="cruise")
    c._approach_brake_on()
    kb.release.assert_called_once_with(AFTERBURNER_KEY)
    kb.press.assert_called_with(AIRBRAKE_KEY)


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_the_pursuit_ticks_the_brake_while_the_marker_has_the_steering():
    src = inspect.getsource(Controller.pursue_and_engage)
    assert ("""                        if not yielding and not rearm_climb_until:
                            if resupply_control:
                                brake_goal = "resupply"
                            elif priority_control:
                                brake_goal = ("crown" if priority_kind == "priority_target"
                                              else "control point")
                        self._approach_brake_tick(brake_goal)""") in src
    exit_ = src.index('self.release_tracking_holds(why="pursuit loop exit")')
    assert src.index('self._approach_brake_off("pursuit loop exit")') > exit_


def test_any_climb_takes_the_airframe_from_the_brake():
    src = inspect.getsource(Controller.climb_mode)
    assert src.index('self._approach_brake_off("climb")') > src.index("self._climbing.set()")
    src = inspect.getsource(Controller._start_rearm_climb_out)
    assert 'self._approach_brake_off("rearm climb-out")' in src


def test_a_pursuit_flying_at_a_crown_brakes(monkeypatch):
    """Cycle 13, end to end: the crown's marker has the steering at 1,100 KPH."""
    import time as _time
    from test_pursuit_mode import (_PT_ON, _AnalyzerStub, _BlankCapture, _TrackerStub,
                                   _priority_marker, _wait_for_pursuit_to_settle)

    class _Fast(_AnalyzerStub):
        def get_telemetry(self):
            now = _time.time()
            return TelemetrySnapshot(speed=TelemetrySignal(value=1100, ts=now),
                                     altitude=TelemetrySignal(value=1500, ts=now, rate=-40.0),
                                     taken_at_s=now)

    monkeypatch.setattr(controller_module, "find_priority_marker",
                        lambda _frame, **_kw: _priority_marker(1400, 800))
    monkeypatch.setattr(controller_module, "find_priority_ring_icons", lambda _f, _c=None: [])
    ctrl = _make_ctrl(monkeypatch, analyzer=_Fast(ammo=2), capture=_BlankCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.0, priority_target=_PT_ON)
    ctrl._approach_brake_cfg = dict(_CFG)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    _time.sleep(1.0)
    ctrl.stop_eject_sequence("respawn_detected")
    _wait_for_pursuit_to_settle(ctrl)
    brakes = _airbrake(ctrl)
    assert brakes and brakes[0] == ("key_press", "approach_brake")
    assert brakes[-1] == ("key_release", "approach_brake")

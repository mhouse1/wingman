"""ADR 171: the jet read off the lobby picks the mission, and mission_j20 goes round.

Operator, 2026-11-01: "when J20 is detected it runs mission J20 ... it flies normal
mission but after missiles are exausted it descends and resupplies then climbs back
to higher altitude and resume search and destroy".

The cycle is mostly parts every pursuit already flies, so these tests pin what is
new: which jet flies what, that the descent writes pitch only where the search would
have, that the hand-back waits out the climb-out, that nothing happens to acs, and
that the whole cycle goes round through the real mission, FSM and pursuit loop.
"""

import inspect
import math
import pathlib
import threading
import time

import pytest
import yaml

import wingman.controller as controller_module
from tests.test_mission_acs import _Analyzer, _Tracker, _drain, _wait
from tests.test_mission_acs import _make_ctrl as _make_mission_ctrl
from tests.test_mission_acs import _restart_ctrl
from tests.test_pursuit_mode import (
    _CaptureStub,
    _ScriptedTracker,
    _TelemetryAnalyzer,
    _TrackerStub,
    _keys,
    _make_ctrl,
    _rearm_with_a_target_low_and_left,
    _wait_for_pursuit_to_settle,
)
from wingman.config_schema import schema_default, validate_config
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.engage_nav import Intent
from wingman.keybindings import (
    NOSE_DOWN_KEY,
    NOSE_UP_KEY,
    ROLL_LEFT_KEY,
    ROLL_RIGHT_KEY,
    SWITCH_WEAPON,
)
from wingman.resupply import ResupplyMarker
from wingman.state import GameState
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot
from wingman.tick_handlers import AmmoEventsHandler, BehaviorTreeHandler

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_J20_PROFILES = {"j20": {"has_padlock": True, "match": "J-20", "mission": "j20"}}


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# Which jet flies what
# ---------------------------------------------------------------------------

def test_a_j20_flies_mission_j20_whatever_the_default_says(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)
    ctrl.set_jet_source(lambda: "J-20 MIGHTY DRAGON")

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["j20"])
    assert ctrl._last_mission == "j20"
    assert "Controller: jet J-20 MIGHTY DRAGON flies mission j20 (ADR 171)" in _messages(caplog)


def test_any_other_jet_flies_the_default(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)
    ctrl.set_jet_source(lambda: "F-14 TOMCAT")

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["acs"])
    assert "Controller: jet F-14 TOMCAT flies mission acs (ADR 171)" in _messages(caplog)


def test_a_jet_that_was_not_read_flies_the_default(monkeypatch, caplog):
    """A J-20 that was not read flies acs, which any jet can fly (ADR 160), so a
    failed read costs the J-20's cycle and nothing else."""
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)
    ctrl.set_jet_source(lambda: None)

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["acs"])
    assert "Controller: jet not read flies mission acs (ADR 171)" in _messages(caplog)


def test_a_lane_with_no_jet_source_flies_its_configured_mission(monkeypatch):
    """The replay lanes pin mission.default_mission and never read a jet."""
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["acs"])


def test_a_failing_jet_source_costs_the_jet_and_not_the_launch(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)

    def _broken():
        raise RuntimeError("stats gone")
    ctrl.set_jet_source(_broken)

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["acs"])


def test_the_next_round_flies_the_next_jets_mission(monkeypatch):
    jet = ["J-20 MIGHTY DRAGON"]
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)
    ctrl.set_jet_source(lambda: jet[0])

    ctrl._start_default_mission()
    assert _wait(lambda: launched == ["j20"])
    jet[0] = "SU-27 FLANKER"
    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["j20", "acs"])
    assert ctrl._last_mission == "acs"


def test_a_respawn_keeps_the_mission_flying_when_the_record_changes(monkeypatch):
    """A jet changes only in the lobby. `o` puts a J-20 on acs by hand, and a
    respawn must not take it back off."""
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                   jet_profiles=_J20_PROFILES)
    ctrl.set_jet_source(lambda: "J-20 MIGHTY DRAGON")
    ctrl._set_last_mission("acs")

    assert ctrl.restart_last_mission() is True

    assert _wait(lambda: launched == ["acs"])


def test_the_doctrine_follows_the_jets_mission(monkeypatch):
    """ADR 147's floor is acs's. A J-20 keeps the tree's 4000 m floor and the
    sustain band its climb back is flown on."""
    ctrl, _launched = _restart_ctrl(monkeypatch, default_mission="acs",
                                    jet_profiles=_J20_PROFILES,
                                    acs={"alt_floor_m": 3000})
    ctrl.set_jet_source(lambda: "J-20 MIGHTY DRAGON")

    ctrl._start_default_mission()

    assert ctrl.altitude_floor_override_m() is None
    assert ctrl.sustain_climb_suppressed() is False


def test_main_feeds_the_controller_the_record_the_round_is_tagged_from():
    source = (_ROOT / "wingman" / "main.py").read_text(encoding="utf-8")
    assert "ctrl.set_jet_source(lambda: stats_tracker.jet)" in source


# ---------------------------------------------------------------------------
# The descent, in the real pursuit loop
# ---------------------------------------------------------------------------

_LOW = {"visible": True, "error_norm": 0.0, "error_norm_y": 0.8, "mode": "TRACKING"}


class _Flying(_TelemetryAnalyzer):
    """Altitude and path angle the test sets, on a real TelemetrySnapshot: rate is
    derived from the angle at the stub's 900 KPH, so `pitch_angle_deg()` runs."""

    def __init__(self, alt=6000.0, angle=0.0, ammo=2, speed=900.0):
        super().__init__(alt=alt, rate=(speed / 3.6) * math.sin(math.radians(angle)))
        self.ammo = ammo
        self.speed = speed

    def get_telemetry(self):
        snap = super().get_telemetry()
        fast = snap.speed
        return TelemetrySnapshot(
            speed=TelemetrySignal(value=self.speed, stable_value=self.speed, ts=fast.ts, rate=0.0),
            altitude=snap.altitude, taken_at_s=snap.taken_at_s, stale_after_s=6.0)

    def fly(self, alt, angle=0.0):
        self.alt = self.stable = float(alt)
        self.rate = (900 / 3.6) * math.sin(math.radians(angle))


def _pursuit(monkeypatch, caplog, analyzer, *, mission="j20", marker=None, tracker=None,
             resupply_return=True, descent_max_s=90.0, start=True, **kwargs):
    """A real pursuit on the secondary, with a target low in view unless a test says
    otherwise. Returns the controller, still running: see _end."""
    caplog.set_level("DEBUG", logger="wingman.controller")
    monkeypatch.setattr(Controller, "_search_look_down", lambda self: False)
    monkeypatch.setattr(controller_module, "find_resupply_marker",
                        lambda _frame, **_kw: marker)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, capture=_CaptureStub(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.0,
                      empty_confirm_reads=1, resupply_priority_actuate=True,
                      sustained_hold_enabled=True, **kwargs)
    ctrl._j20_descent_pulse_s = 0.01
    ctrl._j20_resupply_return = resupply_return
    ctrl._j20_descent_max_s = descent_max_s
    ctrl.set_target_tracker(tracker or _ScriptedTracker([_LOW]))
    ctrl._set_last_mission(mission)
    if start:
        ctrl.pursue_and_engage(weapon_already_switched=True)
    return ctrl


def _end(ctrl):
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)


def _fly_for(ctrl, seconds=0.8):
    time.sleep(seconds)
    _end(ctrl)
    return _keys(ctrl)


def _target_steering(messages):
    """The chase's own steering: the holds a target's or a marker's error starts,
    `HOLD[pitch]: None -> down (target err_y=+0.800)` and `-> right/target`. The
    tests that expect none have a twin that expects some, so this cannot go quiet."""
    return [m for m in messages
            if m.startswith("HOLD[") and ("target err" in m or "/target" in m)]


def test_a_target_below_the_nose_is_not_chased_down_from_altitude(monkeypatch, caplog):
    """2026-11-01 16:55:49: pursuit at 7,075 m, a target below, and 12 s later -791
    m/s at 2,978 KPH with the pull-out predicted to end at -9,688 m."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000))
    keys = _fly_for(ctrl)
    messages = _messages(caplog)

    assert ("DESCENT: to 3000 m at -20 deg, wings level, then level at -10 deg (ADR 171)"
            in messages)
    assert ("key_press", NOSE_DOWN_KEY) in keys
    assert _target_steering(messages) == []


def test_the_descent_holds_the_wings_level(monkeypatch, caplog):
    tracker = _ScriptedTracker([dict(_LOW, error_norm=0.8)])
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), tracker=tracker)
    keys = _fly_for(ctrl)

    assert ("key_press", ROLL_LEFT_KEY) not in keys
    assert ("key_press", ROLL_RIGHT_KEY) not in keys


def test_it_keeps_firing_on_the_way_down(monkeypatch, caplog):
    """The secondary is still a weapon: only the steering waits."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), start=False)
    fired = []
    monkeypatch.setattr(ctrl, "fire_active_weapon", lambda **_: fired.append(1))
    ctrl.pursue_and_engage(weapon_already_switched=True)
    _fly_for(ctrl)

    assert fired


def test_level_at_acs_altitude_the_acs_pursuit_takes_over(monkeypatch, caplog):
    life = _Flying(alt=6000)
    ctrl = _pursuit(monkeypatch, caplog, life)
    assert _wait(lambda: "DESCENT: to 3000 m" in caplog.text)

    life.fly(2950, angle=-20)
    assert _wait(lambda: ("key_press", NOSE_UP_KEY) in _keys(ctrl)), "no level-off"
    time.sleep(0.2)
    assert "DESCENT: over" not in caplog.text, "attacked before it was level"

    life.fly(2900, angle=-8)
    assert _wait(lambda: ("DESCENT: over (level at 2900 m), the ACS pursuit has the aircraft, "
                          "0 pull(s) withheld while slow" in caplog.text))
    assert _wait(lambda: _target_steering(_messages(caplog)))
    _end(ctrl)


def test_acs_chases_at_once_as_it_always_has(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), mission="acs")
    time.sleep(0.8)
    messages = _messages(caplog)
    _end(ctrl)

    assert not any(m.startswith("DESCENT:") for m in messages)
    assert _target_steering(messages)


def test_the_cycle_switched_off_chases_at_once(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), resupply_return=False)
    time.sleep(0.8)
    messages = _messages(caplog)
    _end(ctrl)

    assert not any(m.startswith("DESCENT:") for m in messages)
    assert _target_steering(messages)


def test_a_j20_already_at_acs_altitude_has_nothing_to_come_down_from(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=2800))
    _fly_for(ctrl)
    messages = _messages(caplog)

    assert not any(m.startswith("DESCENT:") for m in messages)
    assert _target_steering(messages)


def test_a_nose_past_the_limit_is_brought_back_up(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000, angle=-30))
    keys = _fly_for(ctrl)

    assert ("key_press", NOSE_UP_KEY) in keys
    assert ("key_press", NOSE_DOWN_KEY) not in keys


def test_a_slow_aircraft_past_the_limit_is_not_pulled_up(monkeypatch, caplog):
    """In the real loop, on real telemetry: the -30 deg of the test above, at the
    239 KPH of 2026-11-01 17:49:32."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000, angle=-30, speed=239.0))
    keys = _fly_for(ctrl)

    assert ("key_press", NOSE_UP_KEY) not in keys
    assert ("key_press", NOSE_DOWN_KEY) not in keys


def test_no_fresh_altitude_presses_nothing_and_does_not_start_the_chase(monkeypatch, caplog):
    """Rule 7: never command on stale telemetry. And the chase from an unknown
    height is the failure this exists to stop."""
    life = _Flying(alt=6000)
    life.new_samples = False      # one old sample: the altitude is not fresh
    ctrl = _pursuit(monkeypatch, caplog, life)
    keys = _fly_for(ctrl)
    messages = _messages(caplog)

    assert any(m.startswith("DESCENT: to 3000 m") for m in messages)
    assert ("key_press", NOSE_DOWN_KEY) not in keys
    assert _target_steering(messages) == []


def test_a_descent_out_of_time_hands_to_the_pursuit(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), descent_max_s=0.0)
    time.sleep(0.8)
    messages = _messages(caplog)
    _end(ctrl)

    assert any(m.startswith("DESCENT: over (time limit at 6000 m), the ACS pursuit has the "
                            "aircraft") for m in messages)
    assert _target_steering(messages)


def test_a_marker_below_the_nose_waits_for_the_descent_too(monkeypatch, caplog):
    """2026-11-01 16:57:37: 2,069 KPH on the approach to a crown below. An
    objective's marker draws the nose down as a target does."""
    marker = ResupplyMarker(960, 1100, 70, 70, 1500, 0)
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000, ammo=0), marker=marker)
    time.sleep(0.8)
    messages = _messages(caplog)
    _end(ctrl)

    assert any(m.startswith("DESCENT: to 3000 m") for m in messages)
    assert _target_steering(messages) == []


def test_the_same_marker_draws_the_nose_down_under_acs(monkeypatch, caplog):
    marker = ResupplyMarker(960, 1100, 70, 70, 1500, 0)
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000, ammo=0), marker=marker,
                    mission="acs")
    time.sleep(0.8)
    messages = _messages(caplog)
    _end(ctrl)

    assert _target_steering(messages)


def test_the_crash_recovery_is_not_held_off_by_a_target_it_is_not_chasing(monkeypatch, caplog):
    """The recovery waits while the chase has its objective in view (2026-10-08).
    The descent has none, so the recovery is free to take a dive that gets away."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000))
    time.sleep(0.5)
    objective_ts = ctrl._pursuit_objective_ts
    _end(ctrl)

    assert objective_ts == 0.0


def test_a_recovery_is_not_ended_by_a_target_in_view_during_the_descent(monkeypatch, caplog):
    """2026-11-01 17:49:48 to :52: the recovery fired three times and each lasted
    0.5 s, `climb complete (objective_in_view)`. During a yield the descent does not
    run, and the chase came back and reported its target."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), start=False)
    monkeypatch.setattr(ctrl, "pursuit_recovery_active", lambda: True)
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    objective_ts = ctrl._pursuit_objective_ts
    _end(ctrl)

    assert objective_ts == 0.0


def test_acs_reports_its_target_through_a_recovery_as_it_always_has(monkeypatch, caplog):
    """The twin, so the test above cannot pass on a chase that reports nothing."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), start=False, mission="acs")
    monkeypatch.setattr(ctrl, "pursuit_recovery_active", lambda: True)
    ctrl.pursue_and_engage(weapon_already_switched=True)
    time.sleep(0.5)
    objective_ts = ctrl._pursuit_objective_ts
    _end(ctrl)

    assert objective_ts > 0.0


def test_a_recovery_outranks_the_descent(monkeypatch, caplog):
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), start=False)
    monkeypatch.setattr(ctrl, "pursuit_recovery_active", lambda: True)
    ctrl.pursue_and_engage(weapon_already_switched=True)
    keys = _fly_for(ctrl)

    assert ("key_press", NOSE_DOWN_KEY) not in keys


def test_the_airbrake_holds_the_descent_at_any_height(monkeypatch, caplog):
    """The dive limit stops at dive_below_m (3000 m); every fast J-20 dive of
    2026-11-01 began above it, at 2,059 to 2,978 KPH."""
    ctrl = _pursuit(monkeypatch, caplog, _Flying(alt=6000), start=False)
    ctrl._approach_brake_cfg = {**ctrl._approach_brake_cfg, "enabled": True, "dive": True,
                                "cap_kph": 850, "target_kph": 800, "decel_kph_s": 150,
                                "max_dose_s": 3.0}
    ctrl.pursue_and_engage(weapon_already_switched=True)
    _fly_for(ctrl, 0.6)

    assert any(m.startswith("SPEEDCAP: 900 KPH over the 850 KPH cap in a dive")
               and "at 6000 m" in m for m in _messages(caplog))


def test_the_search_climb_gets_a_second_descent_when_the_racks_run_out(monkeypatch, caplog):
    """The pursuit's search climbs when it has no target. Back above ACS's altitude
    with every rack empty, the J-20 comes down before it searches."""
    life = _Flying(alt=2800, ammo=0)
    ctrl = _pursuit(monkeypatch, caplog, life, start=False,
                    tracker=_TrackerStub(visible=False, error_norm=None, error_norm_y=None))
    life.fly(4500)
    ctrl.pursue_and_engage(weapon_already_switched=True)
    assert _wait(lambda: "RESUPPLY: missiles exhausted" in caplog.text, timeout=3.0)
    _end(ctrl)

    assert any(m.startswith("DESCENT: to 3000 m") for m in _messages(caplog))


# ---------------------------------------------------------------------------
# The rearm hands the aircraft back
# ---------------------------------------------------------------------------

def _rearmed_pursuit(monkeypatch, *, mission="j20", climb_s=0.6):
    ctrl = _rearm_with_a_target_low_and_left(
        monkeypatch, resupply_priority_actuate=True, rearm_climb_s=climb_s)
    ctrl._set_last_mission(mission)
    restarts = []
    monkeypatch.setattr(ctrl, "mission_j20", lambda **kw: restarts.append(kw))
    monkeypatch.setattr(ctrl, "mission_acs", lambda **kw: restarts.append(kw))
    done = []
    ctrl.pursue_and_engage(on_complete=lambda: done.append(list(restarts)),
                           weapon_already_switched=True)
    return ctrl, restarts, done


def test_the_rearm_ends_the_pursuit_and_restarts_the_mission(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    ctrl, restarts, done = _rearmed_pursuit(monkeypatch)

    assert _wait(lambda: restarts, timeout=4.0)
    _wait_for_pursuit_to_settle(ctrl)
    logs = caplog.text

    assert restarts[0]["climb_back"] is True
    assert done == [[]], "the mission must not start before the FSM is back in GAME_BATTLE"
    assert ("RESUPPLY: rearmed, the mission takes the aircraft back after the climb-out "
            "(ADR 171)") in logs
    assert "PURSUIT SUMMARY: end=rearmed" in logs
    assert logs.index("rearm climb-out over") < logs.index(
        "rearmed, handing the aircraft back to the mission (ADR 171)"), (
        "the climb-out is what pulls the aircraft off the terrain")


def test_the_nose_up_is_not_dropped_for_the_hand_back(monkeypatch, caplog):
    """No hand-back while the climb-out runs, however early the count confirms."""
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, restarts, _done = _rearmed_pursuit(monkeypatch, climb_s=1.2)

    assert _wait(lambda: "RESUPPLY: rearm climb-out" in caplog.text, timeout=4.0)
    time.sleep(0.5)
    assert restarts == [] and ctrl.is_pursuing()

    assert _wait(lambda: restarts, timeout=4.0)
    _wait_for_pursuit_to_settle(ctrl)


def test_acs_keeps_pursuing_after_a_rearm(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, restarts, done = _rearmed_pursuit(monkeypatch, mission="acs", climb_s=0.1)

    assert _wait(lambda: "RESUPPLY: confirmed ammo=6" in caplog.text, timeout=4.0)
    time.sleep(0.5)
    still_pursuing = ctrl.is_pursuing()
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert still_pursuing
    assert restarts == [] and done == [[]]
    assert "rearmed, handing" not in caplog.text


def test_a_respawn_landing_with_the_rearm_does_not_restart_with_a_climb_back(monkeypatch, caplog):
    """CR-019-02's rule for the dive, for the hand-back: the stop wins."""
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, restarts, done = _rearmed_pursuit(monkeypatch, climb_s=0.4)

    assert _wait(lambda: "RESUPPLY: rearmed" in caplog.text, timeout=4.0)
    ctrl._eject_stop_reason = "respawn_detected"
    ctrl._eject_stop.set()
    _wait_for_pursuit_to_settle(ctrl)

    assert restarts == []
    assert done == [[]]


def test_an_operator_stop_is_not_undone_by_the_hand_back(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, restarts, _done = _rearmed_pursuit(monkeypatch, climb_s=0.4)
    ctrl._operator_stop_event.set()

    assert _wait(lambda: "automatic mission restart refused" in caplog.text, timeout=4.0)
    assert restarts == []


def test_the_end_keys_no_auto_restart_is_honoured(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, restarts, _done = _rearmed_pursuit(monkeypatch, climb_s=0.4)
    ctrl.set_auto_respawn_restart(False)

    assert _wait(lambda: "rearmed with auto restart off" in caplog.text, timeout=4.0)
    assert restarts == []


# ---------------------------------------------------------------------------
# mission_j20 after a rearm
# ---------------------------------------------------------------------------

def _climb_back_ctrl(monkeypatch, samples, **j20):
    ctrl = _make_mission_ctrl(monkeypatch, analyzer=_Analyzer(samples), tracker=_Tracker(),
                              j20=j20)
    log = []
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop", lambda: log.append("sd_start"))
    monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop", lambda: log.append("sd_stop"))
    monkeypatch.setattr(ctrl, "switch_weapon",
                        lambda **_: log.append("switch_weapon"))
    ctrl._set_last_mission("j20")
    return ctrl, log


def _run_j20(ctrl, **kwargs):
    t = threading.Thread(target=ctrl.mission_j20, kwargs=kwargs, daemon=True)
    t.start()
    return t


def test_search_and_destroy_resumes_at_the_operating_altitude(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl, log = _climb_back_ctrl(monkeypatch, [(1400, 40), (3900, 40), (4100, 40)])

    t = _run_j20(ctrl, climb_back=True)

    assert _wait(lambda: "sd_start" in log, timeout=4.0)
    assert ("Controller: mission_j20 - rearmed, climbing back to 4000 m before search "
            "and destroy resumes") in _messages(caplog)
    assert any(m.startswith("Controller: mission_j20 - altitude 4100 m at or above 4000 m")
               for m in _messages(caplog))
    _drain(ctrl, t)


def test_search_and_destroy_stays_off_below_it(monkeypatch):
    ctrl, log = _climb_back_ctrl(monkeypatch, [(1400, 40)])

    t = _run_j20(ctrl, climb_back=True)
    assert _wait(lambda: ctrl.is_mission_running())
    time.sleep(0.7)

    assert "sd_start" not in log
    _drain(ctrl, t)


def test_a_stale_altitude_is_no_reason_to_resume(monkeypatch):
    ctrl, log = _climb_back_ctrl(monkeypatch, [(5000, 0)])
    ctrl._analyzer.get_telemetry = lambda: TelemetrySnapshot(
        speed=TelemetrySignal(value=900.0, stable_value=900.0, ts=1.0, rate=0.0),
        altitude=TelemetrySignal(value=5000, stable_value=5000, ts=1.0, rate=0.0),
        taken_at_s=100.0, stale_after_s=6.0)

    t = _run_j20(ctrl, climb_back=True)
    assert _wait(lambda: ctrl.is_mission_running())
    time.sleep(0.7)

    assert "sd_start" not in log
    _drain(ctrl, t)


def test_the_wait_is_bounded(monkeypatch, caplog):
    """A climb that never gets there must not leave a loaded J-20 holding fire
    for the rest of its life (README section 2.2, default 5)."""
    caplog.set_level("WARNING", logger="wingman.controller")
    ctrl, log = _climb_back_ctrl(monkeypatch, [(1400, 40)], climb_back_max_s=0.6)

    t = _run_j20(ctrl, climb_back=True)

    assert _wait(lambda: "sd_start" in log, timeout=3.0)
    assert ("Controller: mission_j20 - 4000 m not reached in 1s, going on"
            in _messages(caplog))
    _drain(ctrl, t)


def test_a_cancel_during_the_climb_back_starts_no_loops(monkeypatch):
    ctrl, log = _climb_back_ctrl(monkeypatch, [(1400, 40)])

    t = _run_j20(ctrl, climb_back=True)
    assert _wait(lambda: ctrl.is_mission_running())
    ctrl.cancel_mission()
    assert _wait(lambda: not ctrl.is_mission_running(), timeout=3.0)
    t.join(timeout=3.0)

    assert "sd_start" not in log


def test_the_climb_back_writes_no_pitch(monkeypatch):
    """The tree flies it (ADR 075); a second writer on the axis is a bug (rule 6)."""
    ctrl, log = _climb_back_ctrl(monkeypatch, [(1400, 40), (4100, 40)])
    calls = []
    monkeypatch.setattr(ctrl, "climb_mode", lambda *a, **k: calls.append("climb"))
    monkeypatch.setattr(ctrl, "nose_up", lambda *a, **k: calls.append("nose_up"))
    monkeypatch.setattr(ctrl, "nose_down", lambda *a, **k: calls.append("nose_down"))

    t = _run_j20(ctrl, climb_back=True)

    assert _wait(lambda: "sd_start" in log, timeout=4.0)
    assert calls == []
    _drain(ctrl, t)


def test_a_rearm_on_the_secondary_goes_back_to_the_primary_once(monkeypatch):
    """SWITCH_WEAPON is a toggle: track it, and press once (rule 8)."""
    ctrl, log = _climb_back_ctrl(monkeypatch, [(4100, 0)])
    ctrl._eject_weapon_switched = True

    t = _run_j20(ctrl, climb_back=True)

    assert _wait(lambda: "sd_start" in log, timeout=3.0)
    assert log.count("switch_weapon") == 1
    assert log.index("switch_weapon") < log.index("sd_start")
    assert ctrl._eject_weapon_switched is False
    _drain(ctrl, t)


def test_a_rearm_on_the_primary_presses_nothing(monkeypatch):
    ctrl, log = _climb_back_ctrl(monkeypatch, [(4100, 0)])

    t = _run_j20(ctrl, climb_back=True)

    assert _wait(lambda: "sd_start" in log, timeout=3.0)
    assert "switch_weapon" not in log
    _drain(ctrl, t)


def test_the_turn_guard_is_for_a_spawn_heading(monkeypatch):
    """ADR 132: the heading a spawn leaves points into the arena. The one a
    resupply leaves says nothing, and the guard would hold off the boundary turn."""
    ctrl, log = _climb_back_ctrl(monkeypatch, [(4100, 0)])

    t = _run_j20(ctrl, climb_back=True)
    assert _wait(lambda: "sd_start" in log, timeout=3.0)
    assert not ctrl.is_turn_guarded()
    _drain(ctrl, t)

    log.clear()
    t = _run_j20(ctrl)
    assert _wait(lambda: "sd_start" in log, timeout=3.0)
    assert ctrl.is_turn_guarded()
    _drain(ctrl, t)


def test_a_respawn_restart_has_no_climb_back(monkeypatch):
    """A new life opens as it always has: loops up, spawn heading first."""
    ctrl, launched = _restart_ctrl(monkeypatch)
    kwargs = []
    monkeypatch.setattr(ctrl, "mission_j20", lambda **kw: kwargs.append(kw))
    ctrl._set_last_mission("j20")

    ctrl.restart_last_mission()

    assert _wait(lambda: kwargs)
    assert kwargs[0]["climb_back"] is False


# ---------------------------------------------------------------------------
# The ceiling
# ---------------------------------------------------------------------------

def _ceiling_ctrl(monkeypatch, samples):
    """A real controller with real telemetry: one tick of the ceiling per sample."""
    ctrl = _make_mission_ctrl(monkeypatch, analyzer=_Analyzer(samples), tracker=_Tracker())
    ctrl._set_last_mission("j20")
    return ctrl


def test_a_rising_j20_over_the_ceiling_gets_the_nose_held_down(monkeypatch, caplog):
    """2026-11-01: the climb let go at up to 7,011 m and +757 m/s, and the aircraft
    coasted to 9,395 m and stalled."""
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl = _ceiling_ctrl(monkeypatch, [(6000, 40)])

    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held == "down"
    assert ("Controller: mission_j20 - over the ceiling at 6000 m, path +40 deg: nose down "
            "until level (ADR 171)") in _messages(caplog)


def test_it_is_held_and_not_repeated(monkeypatch, caplog):
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl = _ceiling_ctrl(monkeypatch, [(6000, 40), (6100, 30), (6200, 20)])

    for _ in range(3):
        ctrl._j20_ceiling_tick()

    assert sum("over the ceiling" in m for m in _messages(caplog)) == 1
    assert ctrl._pitch_held == "down"


def test_level_lets_it_go(monkeypatch):
    ctrl = _ceiling_ctrl(monkeypatch, [(6000, 40), (6100, 3)])

    ctrl._j20_ceiling_tick()
    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held is None


def test_under_the_ceiling_nothing_is_pressed(monkeypatch):
    ctrl = _ceiling_ctrl(monkeypatch, [(5400, 60)])

    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held is None


def test_a_descending_j20_over_the_ceiling_is_left_alone(monkeypatch):
    """Coming down from it is the point; the stall is what the nose must not meet."""
    ctrl = _ceiling_ctrl(monkeypatch, [(7000, -5)])

    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held is None


def test_a_stale_altitude_lets_go_and_presses_nothing(monkeypatch):
    ctrl = _ceiling_ctrl(monkeypatch, [(6000, 40)])
    ctrl._j20_ceiling_tick()
    assert ctrl._pitch_held == "down"
    ctrl._analyzer.get_telemetry = lambda: TelemetrySnapshot(
        speed=TelemetrySignal(value=900.0, stable_value=900.0, ts=1.0, rate=0.0),
        altitude=TelemetrySignal(value=6000, stable_value=6000, ts=1.0, rate=0.0),
        taken_at_s=100.0, stale_after_s=6.0)

    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held is None


@pytest.mark.parametrize("owner", ["is_spawn_guarding", "is_missile_evading",
                                   "is_boundary_turning"])
def test_it_stands_aside_for_whatever_owns_the_axis(monkeypatch, owner):
    """Rule 6: one writer per axis."""
    ctrl = _ceiling_ctrl(monkeypatch, [(6000, 40)])
    monkeypatch.setattr(ctrl, owner, lambda: True)

    ctrl._j20_ceiling_tick()

    assert ctrl._pitch_held is None


def test_a_routine_climb_is_ended_at_the_ceiling(monkeypatch, caplog):
    """18:37: the altitude reading froze at 3,006 m for 5 s, the hold pulled on to
    8,100 m, and the aircraft was at 79 KPH 6 s later. The ceiling waited for it."""
    caplog.set_level("INFO", logger="wingman.controller")
    ctrl = _ceiling_ctrl(monkeypatch, [(6300, 60)])
    monkeypatch.setattr(ctrl, "is_climbing", lambda: True)

    ctrl._j20_ceiling_tick()

    assert ctrl._climb_stop.is_set()
    assert ctrl._pitch_held is None, "the climb still holds the axis this tick"
    assert ("Controller: mission_j20 - over the ceiling at 6300 m in a climb, ending it "
            "(ADR 171)") in _messages(caplog)


def test_a_hard_emergency_is_never_ended(monkeypatch):
    ctrl = _ceiling_ctrl(monkeypatch, [(6300, 60)])
    monkeypatch.setattr(ctrl, "is_climbing", lambda: True)
    ctrl.set_climb_emergency(True)

    ctrl._j20_ceiling_tick()

    assert not ctrl._climb_stop.is_set()


def test_a_climb_under_the_ceiling_is_left_to_finish(monkeypatch):
    ctrl = _ceiling_ctrl(monkeypatch, [(5000, 60)])
    monkeypatch.setattr(ctrl, "is_climbing", lambda: True)

    ctrl._j20_ceiling_tick()

    assert not ctrl._climb_stop.is_set()


def test_the_running_mission_pushes_over_and_lets_go_when_it_ends(monkeypatch):
    """Rule 5: a key held past the end of its mission presses into the next life."""
    ctrl, log = _climb_back_ctrl(monkeypatch, [(6000, 40)])

    t = _run_j20(ctrl)

    assert _wait(lambda: ctrl._pitch_held == "down", timeout=3.0)
    _drain(ctrl, t)
    assert ctrl._pitch_held is None
    assert ctrl._j20_ceiling_pushing is False


def test_acs_has_no_ceiling(monkeypatch):
    """ADR 147: acs flies its own altitudes, and its search climb goes to 7000 m."""
    source = inspect.getsource(Controller._run_acs_sequence)
    assert "_j20_ceiling" not in source
    assert "_j20_ceiling" not in inspect.getsource(Controller.mission_acs)


# ---------------------------------------------------------------------------
# Steering with the padlock camera
# ---------------------------------------------------------------------------

class _SteerCtrl:
    """The two questions the gate asks and the roll it must not press."""

    def __init__(self, padlock, blocked=False):
        self.padlock = padlock
        self.blocked = blocked
        self.rolls = []

    def padlock_state(self):
        return self.padlock

    def is_padlock_blocked(self):
        return self.blocked

    def orient_nose_to_target(self, error_norm, **_kw):
        self.rolls.append(error_norm)
        return "right"


def _steer(padlock, blocked=False, **kwargs):
    """The real `_actuate_engage` on a real navigator, built without the handler's
    collaborators. Returns the rolls it pressed and whether the navigator ran."""
    handler = BehaviorTreeHandler.__new__(BehaviorTreeHandler)
    handler._ctrl = _SteerCtrl(padlock, blocked)
    updates = []

    class _Nav:
        def update(self, *_a, **_k):
            updates.append(1)
            return Intent("engage-long", "steer", 0.4, None, "steering")

    handler._nav = _Nav()
    handler._dry_run = False
    handler._ctl_cfg = {}
    handler._last_nav_mode = None
    handler._last_orbit_roll_ts = 0.0
    handler._orbit_interval_s = 2.0
    handler._orbit_hold_s = 0.3
    handler._friendly_components = []
    handler.last_nav_roll = None
    handler._actuate_engage([], 5000.0, 100.0, **kwargs)
    return handler._ctrl.rolls, updates


def test_padlock_confirmed_off_steers_at_the_targets():
    rolls, updates = _steer(False)
    assert rolls == [0.4] and updates


def test_padlock_unconfirmed_holds_the_trajectory():
    """Operator, 2026-11-01: "if padlock is on steering continues existing
    trajectory". Unknown is untrusted: no detector can say on."""
    rolls, updates = _steer(None)
    assert rolls == []
    assert updates == [], "a bearing off the turned view must not reach the smoothing"


def test_padlock_on_holds_the_trajectory():
    assert _steer(True)[0] == []


def test_the_steering_through_a_climb_holds_too():
    """Climb steers roll with this same call, and takes nearly half the ticks."""
    assert _steer(None, steer_only=True)[0] == []


def test_acs_never_presses_the_padlock_so_it_never_waits_on_it():
    rolls, _updates = _steer(None, blocked=True)
    assert rolls == [0.4]


def test_the_log_says_why_it_held(caplog):
    caplog.set_level("DEBUG", logger="wingman.tick_handlers")
    _steer(None)
    assert ("EngageNav: holding the trajectory, padlock camera not confirmed off (ADR 171)"
            in _messages(caplog))


# ---------------------------------------------------------------------------
# Round and round, through the real mission, FSM and pursuit loop
# ---------------------------------------------------------------------------

class _J20Life(_TelemetryAnalyzer):
    """A J-20's altitude and ammo, set by the test, on the real transition table."""

    game_state = GameState.GAME_BATTLE

    def fly(self, alt, ammo=None):
        self.alt = self.stable = float(alt)
        if ammo is not None:
            self.ammo = ammo


def test_the_cycle_goes_round(monkeypatch, caplog):
    caplog.set_level("DEBUG", logger="wingman.controller")
    monkeypatch.setattr(Controller, "_search_look_down", lambda self: False)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: None)
    life = _J20Life(alt=6000.0)
    ctrl = _make_ctrl(monkeypatch, analyzer=life, capture=_CaptureStub(), pursuit_enabled=True,
                      pursuit_max_duration_s=0.0, empty_confirm_reads=1,
                      resupply_priority_actuate=True, sustained_hold_enabled=True,
                      rearm_climb_s=0.2)
    ctrl._j20_descent_pulse_s = 0.01
    ctrl.set_target_tracker(_TrackerStub(visible=False, error_norm=None, error_norm_y=None))
    handler = AmmoEventsHandler(life, ctrl, {}, bt_owns_eject=True)

    def logged(text):
        return text in caplog.text

    ctrl._set_last_mission("j20")
    first = threading.Thread(target=ctrl.mission_j20, daemon=True)
    first.start()
    assert _wait(lambda: logged("adaptive mission running"))

    # Primaries out: the Eject leaf's call, at the J-20's own altitude. It comes
    # down before anything else.
    life.fly(6000, ammo=0)
    handler.fire_eject()
    assert life.game_state == GameState.GAME_BATTLE_EJECT
    assert _wait(lambda: logged("DESCENT: to 3000 m"), timeout=4.0)
    assert _wait(lambda: ("key_press", NOSE_DOWN_KEY) in _keys(ctrl))

    # Level at ACS's altitude, every rack empty, then through the resupply point.
    life.fly(2900)
    assert _wait(lambda: logged("DESCENT: over (level at 2900 m)"))
    assert _wait(lambda: logged("RESUPPLY: missiles exhausted"), timeout=4.0)
    life.fly(1400, ammo=6)
    assert _wait(lambda: logged("RESUPPLY: rearmed, the mission takes"), timeout=4.0)
    assert _wait(lambda: logged("handing the aircraft back"), timeout=4.0)

    # Back in GAME_BATTLE with the mission holding the lock and the loops down,
    # which is what the tree's climb needs and no more.
    assert _wait(lambda: logged("climbing back to 4000 m"))
    assert life.game_state == GameState.GAME_BATTLE
    assert ctrl.is_mission_running()
    assert caplog.text.count("adaptive mission running") == 1
    switches = [k for k in _keys(ctrl) if k == ("key_press", SWITCH_WEAPON)]
    assert len(switches) == 2, "secondary for the pursuit, then the primary back: %r" % switches

    # Up at the operating altitude: search and destroy again.
    life.fly(4100)
    assert _wait(lambda: caplog.text.count("adaptive mission running") == 2, timeout=4.0)
    assert ctrl._sdl_weapon_thread is not None and ctrl._sdl_weapon_thread.is_alive()

    ctrl.cancel_mission()
    assert _wait(lambda: not ctrl.is_mission_running(), timeout=4.0)
    first.join(timeout=3.0)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_the_shipped_j20_profile_flies_mission_j20(shipped_cfg):
    from wingman.jet_name import mission_for_jet
    profiles = shipped_cfg["jet_profile"]["profiles"]

    assert mission_for_jet("J-20 MIGHTY DRAGON", profiles) == "j20"
    assert mission_for_jet("J-10 FIREBIRD", profiles) is None


def test_every_other_jet_still_flies_acs_as_shipped(shipped_cfg):
    """Operator, 2026-09-24, unchanged: acs is the default, the J-20 the exception."""
    assert shipped_cfg["mission"]["default_mission"] == "acs"


def test_the_shipped_numbers_match_the_mission_document(shipped_cfg):
    j20 = shipped_cfg["j20_mission"]
    assert j20["resupply_return"] is True
    assert j20["descent_angle_deg"] == -20
    assert j20["descent_max_s"] == 90.0


def test_the_search_resumes_where_the_doctrine_starts(shipped_cfg):
    """The tree climbs a J-20 under its floor, so the loops wait for that and no
    longer: a higher number would have them wait on the sustain band's hysteresis."""
    climb = shipped_cfg["behavior_tree"]["climb"]
    assert shipped_cfg["j20_mission"]["resume_alt_m"] == climb["alt_floor_m"]
    assert shipped_cfg["j20_mission"]["climb_back_max_s"] == climb["sustain"]["max_climb_s"]


def test_the_descent_stays_out_of_the_hard_recovery_under_its_brake(shipped_cfg):
    """recover_below_time_s reads altitude over descent rate, and the descent ends
    at acs's level-off, where the margin is least. The brake holds it at the cap;
    between doses the reading can run over it, so the margin is taken at 1000 KPH."""
    j20 = shipped_cfg["j20_mission"]
    limit_s = shipped_cfg["behavior_tree"]["climb"]["recover_below_time_s"]
    rate = (1000 / 3.6) * math.sin(math.radians(-j20["descent_angle_deg"]))
    assert shipped_cfg["acs_mission"]["climb_alt_m"] / rate > limit_s


def test_nothing_pulls_up_on_the_edge_of_the_stall(shipped_cfg):
    """Above the speed the tree calls a near-stall, or a pull could start at it."""
    min_pull = shipped_cfg["j20_mission"]["descent_min_pull_kph"]
    assert min_pull > shipped_cfg["behavior_tree"]["stall_prevention"]["min_speed_kph"]


def test_the_ceiling_is_above_the_band_the_tree_climbs_to(shipped_cfg):
    """Under it, the ceiling would fight the sustain climb on the same axis."""
    ceiling = shipped_cfg["j20_mission"]["ceiling_alt_m"]
    assert ceiling > shipped_cfg["behavior_tree"]["climb"]["sustain"]["exit_above_alt"]


def test_a_run_from_the_highest_altitude_seen_fits_inside_the_bound(shipped_cfg):
    """8,476 m on 2026-11-01. Running out of time starts the chase wherever the
    aircraft is, which is the failure itself."""
    j20 = shipped_cfg["j20_mission"]
    rate = (shipped_cfg["approach_brake"]["cap_kph"] / 3.6) * math.sin(
        math.radians(-j20["descent_angle_deg"]))
    assert (8476 - shipped_cfg["acs_mission"]["climb_alt_m"]) / rate < j20["descent_max_s"]


def test_it_comes_down_to_where_acs_flies(shipped_cfg, monkeypatch):
    """One source: `effectivly using normal acs mode` (operator, 2026-11-01)."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig.from_config(shipped_cfg, disable_hotkeys=True))
    ctrl._analyzer = _Analyzer([(6000, 0)])
    descent = ctrl._start_descent()

    assert descent.target_alt_m == shipped_cfg["acs_mission"]["climb_alt_m"]
    assert descent.level_angle_deg == shipped_cfg["acs_mission"]["nose_angle_deg"]


def test_the_schema_is_where_the_defaults_live(shipped_cfg):
    j20 = shipped_cfg["j20_mission"]
    for key in ("resupply_return", "descent_angle_deg",
                "descent_tolerance_deg", "descent_pulse_s", "descent_max_s",
                "descent_min_pull_kph", "resume_alt_m", "climb_back_max_s",
                "ceiling_alt_m", "ceiling_path_deg"):
        assert j20[key] == schema_default("j20_mission." + key)


def test_the_controller_reads_the_shipped_blocks(shipped_cfg, monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig.from_config(shipped_cfg, disable_hotkeys=True))

    assert ctrl._j20_descent_angle_deg == -20
    assert ctrl._j20_resume_alt_m == 4000
    assert ctrl._jet_profiles["j20"]["match"] == "J-20"
    ctrl.set_jet_source(lambda: "J-20 MIGHTY DRAGON")
    assert ctrl.launch_mission_name() == "j20"
    ctrl.set_jet_source(lambda: "F-14 TOMCAT")
    assert ctrl.launch_mission_name() == "acs"


def test_the_schema_rejects_a_profile_naming_no_mission_it_knows(shipped_cfg):
    cfg = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    assert validate_config(cfg) == []

    cfg["jet_profile"]["profiles"]["j20"]["mission"] = "f111"
    errors = validate_config(cfg)

    assert any("jet_profile.profiles.j20.mission" in e for e in errors), errors


def test_a_misspelt_key_fails_validation(shipped_cfg):
    cfg = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    cfg["j20_mission"]["descent_alt"] = 2500

    assert any("descent_alt" in e for e in validate_config(cfg))

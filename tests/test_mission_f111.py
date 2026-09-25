"""mission_f111 (ADR 149, docs/missions/f111.md): mission_su30 plus the wing sweep.

  1. nose up on battle start or respawn, and as the climb starts press 'w' once
     to sweep the wings
  2. boresight engage only
  3. no weapon switch until the spawn weapon runs out
  4. at 3000 m set the nose angle to -10 deg
  5. when the altitude descends back to 3000 m press 'w' once to unsweep; wait
     at most 30 s and unsweep anyway on timeout
  6. activate pursuit mode

The steps shared with su30 are pinned in test_mission_su30.py against the same
helpers; these tests pin what is new (the toggle, the bounded descent wait, the
per-life reset), the six-step order, the wiring through default_mission and the
restart branch, the shipped config, and ADR 147's altitude doctrine extended to
this mission.
"""

import math
import pathlib
import threading
import time

import py_trees
import pytest
import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.behavior_tree import (
    AnalyzerSnapshot,
    TACTIC_CLIMB,
    make_snapshot_writer,
    selected_tactic,
)
from wingman.config_schema import validate_config
from wingman.controller import Controller, WINGSWEEP_KEY
from wingman.controller_config import ControllerConfig
from wingman.telemetry import TelemetrySignal, TelemetrySnapshot

_ROOT = pathlib.Path(__file__).resolve().parents[1]

_SPEED_KPH = 900.0


def _snap(alt, angle_deg=0.0, ts=None, taken_at=None):
    """A REAL TelemetrySnapshot (see test_mission_su30._snap)."""
    ts = time.time() if ts is None else ts
    taken_at = ts if taken_at is None else taken_at
    rate = (_SPEED_KPH / 3.6) * math.sin(math.radians(angle_deg))
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=_SPEED_KPH, stable_value=_SPEED_KPH, ts=ts, rate=0.0),
        altitude=TelemetrySignal(value=alt, stable_value=alt, ts=ts, rate=rate),
        taken_at_s=taken_at,
        stale_after_s=6.0,
    )


class _Analyzer:
    """Scripted telemetry: each get_telemetry() consumes one (alt, angle) entry
    (the last repeats) stamped with a NEW timestamp, unless ``same_ts`` pins
    every sample to one timestamp, or ``stale`` ages every sample out."""

    def __init__(self, samples, state=GameState.GAME_BATTLE, refuse_events=(),
                 same_ts=False, stale=False):
        self._samples = list(samples)
        self._n = 0
        self.game_state = state
        self.events = []
        self._refuse = set(refuse_events)
        self._same_ts = same_ts
        self._stale = stale
        self._last_battle_event_ts = 0.0

    def get_telemetry(self):
        alt, angle = self._samples[min(self._n, len(self._samples) - 1)]
        self._n += 1
        ts = 1000.0 if self._same_ts else 1000.0 + self._n
        if self._stale:
            return _snap(alt, angle, ts=ts, taken_at=ts + 60.0)
        return _snap(alt, angle, ts=ts)

    def trigger_event(self, name):
        self.events.append(name)
        return name not in self._refuse

    def get_ammo_missiles(self):
        return 2

    def get_ammo_flares(self):
        return 4

    def get_health(self):
        return 100


class _Tracker:
    def update(self, frame):
        return {"visible": False, "error_norm": None, "error_norm_y": None}


_FAST = {"tick_s": 0.005, "angle_pulse_s": 0.01, "angle_max_s": 2.0,
         "angle_confirm_reads": 2, "angle_tolerance_deg": 4.0,
         "climb_alt_m": 3000, "nose_angle_deg": -10,
         "unsweep_alt_m": 3000, "unsweep_timeout_s": 2.0, "wingsweep_tap_s": 0.1}

# Level off at 3100 m, confirm -10 deg over two reads, then descend: the first
# 2950 m sample is the descent step 5 waits for.
_FLIGHT = [(3100, -10), (3100, -10), (3100, -10), (3100, -10), (2950, -10)]


def _make_ctrl(monkeypatch, analyzer=None, tracker=None, f111=None, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                f111={**_FAST, **(f111 or {})}, **cfg),
    )
    if tracker is not None:
        ctrl.set_target_tracker(tracker)
    return ctrl


def _record(ctrl, monkeypatch):
    """Replace the actuators with recorders on one shared, ordered log."""
    log = []
    monkeypatch.setattr(ctrl, "climb_mode",
                        lambda **kw: log.append(("climb", kw.get("target_alt"))))
    monkeypatch.setattr(ctrl, "switch_weapon",
                        lambda *a, **k: log.append(("switch_weapon",)))
    monkeypatch.setattr(ctrl, "nose_down",
                        lambda *a, **k: log.append(("nose_down",)))
    monkeypatch.setattr(ctrl, "nose_up",
                        lambda *a, **k: log.append(("nose_up",)))
    monkeypatch.setattr(ctrl, "wingsweep",
                        lambda hold_seconds=0.5, block=True:
                        log.append(("wingsweep", hold_seconds)))
    monkeypatch.setattr(
        ctrl, "pursue_and_engage",
        lambda **kw: log.append(("pursue", kw.get("defer_switch_until_empty"))))
    monkeypatch.setattr(ctrl, "eject_and_dive",
                        lambda *a, **k: log.append(("dive",)))
    monkeypatch.setattr(ctrl, "start_boresight_engage_loop",
                        lambda: log.append(("boresight_start",)))
    monkeypatch.setattr(ctrl, "stop_boresight_engage_loop",
                        lambda: log.append(("boresight_stop",)))
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop",
                        lambda: log.append(("sdl_start",)))
    return log


def _run_mission(ctrl):
    t = threading.Thread(target=ctrl.mission_f111, daemon=True)
    t.start()
    return t


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return pred()


def _drain(ctrl, thread):
    """Cancel and wait the mission out (it clears the cancel on entry, so keep
    re-asserting it until the lock is actually free)."""
    deadline = time.time() + 5.0
    while ctrl.is_mission_running() and time.time() < deadline:
        ctrl._mission_cancel.set()
        time.sleep(0.02)
    thread.join(timeout=3.0)
    assert not ctrl.is_mission_running()


def _kinds(log):
    return [e[0] for e in log]


def _messages(caplog):
    return [r.getMessage() for r in caplog.records]


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------

def test_the_six_steps_run_in_the_documented_order(monkeypatch, caplog):
    analyzer = _Analyzer(_FLIGHT)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    with caplog.at_level("INFO"):
        t = _run_mission(ctrl)
        t.join(timeout=5.0)
    assert not t.is_alive()

    kinds = _kinds(log)
    assert kinds[0] == "climb" and log[0][1] == 3000
    sweeps = [i for i, k in enumerate(kinds) if k == "wingsweep"]
    assert len(sweeps) == 2, "exactly one sweep and one unsweep per life"
    assert sweeps[0] < kinds.index("boresight_start") < sweeps[1]
    assert kinds.index("boresight_stop") < kinds.index("pursue")
    assert sweeps[1] < kinds.index("pursue")
    assert ("pursue", True) in log, "pursuit defers the weapon switch, as su30"
    assert "switch_weapon" not in kinds
    assert "sdl_start" not in kinds
    assert "dive" not in kinds
    assert analyzer.events == ["eject_started"]

    msgs = _messages(caplog)
    order = ["step 1/6: nose up", "step 1/6: sweeping the wings",
             "step 2/6: boresight engage on", "step 3/6: keeping the selected",
             "altitude 3100 m at or above 3000 m",
             "step 4/6: 3000 m reached", "step 5/6: waiting up to",
             "step 5/6: altitude 2950 m at or below 3000 m",
             "step 5/6: unsweeping the wings", "step 6/6: activating pursuit mode"]
    idx = []
    for needle in order:
        hits = [i for i, m in enumerate(msgs) if "mission_f111" in m and needle in m]
        assert hits, f"missing log line: {needle}"
        idx.append(hits[0])
    assert idx == sorted(idx), "the step log lines are out of order"


def test_the_wing_sweep_is_a_short_tap_through_the_controller(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer(_FLIGHT), tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    _run_mission(ctrl).join(timeout=5.0)
    assert [e for e in log if e[0] == "wingsweep"] == [("wingsweep", 0.1)] * 2


def _key_presses(ctrl):
    with ctrl._action_intents_lock:
        return [i for i in ctrl._action_intents if i["action_type"] == "key_press"]


def test_the_real_wingsweep_helper_presses_the_toggle_key_once(monkeypatch):
    """No recorder: the tap goes through _execute_key_press to WINGSWEEP_KEY."""
    ctrl = _make_ctrl(monkeypatch)
    assert ctrl._f111_press_wingsweep(swept=True) is True
    presses = _key_presses(ctrl)
    assert [p["key"] for p in presses] == [WINGSWEEP_KEY]
    assert presses[0]["hold_seconds"] == pytest.approx(0.1)
    assert ctrl._f111_wings_swept is True


def test_the_real_loop_fires_and_presses_w_once_mid_climb(monkeypatch):
    """End to end through the real boresight loop and key primitives (only the
    climb is stubbed): the fire key goes down, the weapon toggle never does,
    the padlock camera is never pressed by the mission, and 'w' goes down
    exactly once while the climb holds."""
    from wingman.controller import FIRE_ACTIVE_WEAPON, PADLOCK_CAMERA, SWITCH_WEAPON

    analyzer = _Analyzer([(1000, 20)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      weapon_loop_interval=0.1)
    monkeypatch.setattr(ctrl, "climb_mode", lambda **kw: None)
    t = _run_mission(ctrl)
    assert _wait(lambda: [p["key"] for p in _key_presses(ctrl)].count(
        FIRE_ACTIVE_WEAPON) >= 3)
    keys = [p["key"] for p in _key_presses(ctrl)]
    _drain(ctrl, t)
    assert keys.count(WINGSWEEP_KEY) == 1
    assert PADLOCK_CAMERA not in keys
    assert SWITCH_WEAPON not in keys
    assert ctrl._boresight_thread is None, "the loop outlived the mission"


def test_the_unsweep_waits_for_a_sample_newer_than_the_level_off(monkeypatch):
    """The reading that ended the climb (at or above 3000 m) never stands in for
    the descent; only a later, fresh sample at or below 3000 m unsweeps."""
    flight = [(3100, -10)] * 4 + [(3050, -10)] * 3 + [(3000, -10)]
    analyzer = _Analyzer(flight)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    _run_mission(ctrl).join(timeout=5.0)
    assert _kinds(log).count("wingsweep") == 2
    assert analyzer._n >= len(flight), "unswept before the 3000 m sample arrived"


def test_a_repeated_timestamp_never_counts_as_the_descent(monkeypatch, caplog):
    """Rule 7: one reading seen again is not new evidence. With every sample on
    one timestamp the step can only end by its timeout."""
    analyzer = _Analyzer([(2900, -10)], same_ts=True)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer,
                      f111={"unsweep_timeout_s": 0.3})
    log = _record(ctrl, monkeypatch)
    ctrl._f111_wings_swept = True
    with caplog.at_level("INFO"):
        assert ctrl._f111_unsweep_on_descent() is True
    assert analyzer._n > 3, "the step never re-read the altitude"
    assert log == [("wingsweep", 0.1)]
    msgs = _messages(caplog)
    assert not any("step 5/6: altitude" in m for m in msgs)
    assert any("unsweeping anyway" in m for m in msgs)


def test_stale_altitude_never_advances_the_script(monkeypatch):
    """No fresh altitude: the climb continues and neither the level-off nor the
    unsweep ever fires."""
    analyzer = _Analyzer([(2900, -10)], stale=True)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: analyzer._n > 20)
    kinds = _kinds(log)
    assert kinds.count("wingsweep") == 1, "only the step-1 sweep"
    assert "nose_down" not in kinds and "pursue" not in kinds
    _drain(ctrl, t)


def test_on_timeout_the_wings_are_unswept_anyway_and_pursuit_follows(monkeypatch, caplog):
    analyzer = _Analyzer([(3100, -10)])          # never descends
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      f111={"unsweep_timeout_s": 0.2})
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("WARNING"):
        _run_mission(ctrl).join(timeout=5.0)
    kinds = _kinds(log)
    assert kinds.count("wingsweep") == 2
    assert kinds.index("pursue") > max(i for i, k in enumerate(kinds) if k == "wingsweep")
    assert ctrl._f111_wings_swept is False
    assert any("step 5/6: no fresh altitude at or below 3000 m" in m
               and "unsweeping anyway" in m for m in _messages(caplog))


def test_the_unsweep_wait_is_bounded_by_its_config(monkeypatch):
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      f111={"unsweep_timeout_s": 0.3})
    log = _record(ctrl, monkeypatch)
    ctrl._f111_wings_swept = True
    ctrl._mission_cancel.clear()
    start = time.time()
    assert ctrl._f111_unsweep_on_descent() is True
    assert time.time() - start < 1.5
    assert log == [("wingsweep", 0.1)]


def test_the_unsweep_step_presses_no_pitch_key(monkeypatch):
    """Rule 13: the wait never dives. It only reads altitude and taps 'w'."""
    analyzer = _Analyzer([(3100, -10)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, f111={"unsweep_timeout_s": 0.2})
    log = _record(ctrl, monkeypatch)
    ctrl._f111_wings_swept = True
    ctrl._f111_unsweep_on_descent()
    assert set(_kinds(log)) == {"wingsweep"}


# ---------------------------------------------------------------------------
# The toggle
# ---------------------------------------------------------------------------

def test_a_restart_within_the_same_life_does_not_sweep_again(monkeypatch, caplog):
    """After a disengage roll or 'u' the wings are still swept: a second press
    would unsweep them."""
    analyzer = _Analyzer([(1000, 20)])           # never levels off
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("wingsweep", 0.1) in log)
    _drain(ctrl, t)
    assert ctrl._f111_wings_swept is True

    with caplog.at_level("INFO"):
        t = _run_mission(ctrl)
        assert _wait(lambda: any("wings already swept" in m for m in _messages(caplog)))
        _drain(ctrl, t)
    assert _kinds(log).count("wingsweep") == 1


def test_the_tracked_state_resets_with_the_life(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl._f111_wings_swept = True
    ctrl.stop_eject_sequence("respawn_detected")
    assert ctrl._f111_wings_swept is False


def test_a_new_life_sweeps_again(monkeypatch):
    analyzer = _Analyzer([(1000, 20)])
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: ("wingsweep", 0.1) in log)
    _drain(ctrl, t)
    ctrl.stop_eject_sequence("respawn_detected")
    t = _run_mission(ctrl)
    assert _wait(lambda: _kinds(log).count("wingsweep") == 2)
    _drain(ctrl, t)


def test_a_cancel_while_swept_presses_nothing_and_says_so(monkeypatch, caplog):
    analyzer = _Analyzer([(1000, 20)])           # cancelled mid-climb
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO"):
        t = _run_mission(ctrl)
        assert _wait(lambda: ("wingsweep", 0.1) in log)
        _drain(ctrl, t)
    assert _kinds(log).count("wingsweep") == 1
    assert ctrl._f111_wings_swept is True
    assert any("wings left swept" in m for m in _messages(caplog))
    assert ctrl._climb_stop.is_set()
    assert ("boresight_stop",) in log


def test_a_cancel_during_the_unsweep_wait_presses_nothing(monkeypatch):
    analyzer = _Analyzer([(3100, -10)])          # never descends
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker(),
                      f111={"unsweep_timeout_s": 30.0})
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: analyzer._n > 8)       # past the angle step
    _drain(ctrl, t)
    kinds = _kinds(log)
    assert kinds.count("wingsweep") == 1
    assert "pursue" not in kinds


def test_a_mission_cancelled_before_the_sweep_records_no_sweep(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)
    ctrl._mission_cancel.set()
    assert ctrl._f111_press_wingsweep(swept=True) is False
    assert log == []
    assert ctrl._f111_wings_swept is False


# ---------------------------------------------------------------------------
# Faults and teardown
# ---------------------------------------------------------------------------

def test_missing_tracker_holds_instead_of_diving(monkeypatch):
    analyzer = _Analyzer(_FLIGHT)
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=None)
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: _kinds(log).count("wingsweep") == 2)
    time.sleep(0.1)
    assert ctrl.is_mission_running(), "the mission must hold, not end"
    kinds = _kinds(log)
    assert "pursue" not in kinds and "dive" not in kinds
    _drain(ctrl, t)


def test_fsm_refusal_keeps_pursuit_from_starting(monkeypatch):
    analyzer = _Analyzer(_FLIGHT, refuse_events=("eject_started",))
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer, tracker=_Tracker())
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: "eject_started" in analyzer.events)
    time.sleep(0.05)
    assert "pursue" not in _kinds(log)
    _drain(ctrl, t)


def test_the_lock_is_released_after_the_handoff(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer(_FLIGHT), tracker=_Tracker())
    _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert not ctrl.is_mission_running()


def test_a_failing_boresight_stop_does_not_leave_the_lock_held(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer([(1000, 20)]), tracker=_Tracker())
    _record(ctrl, monkeypatch)

    def _boom():
        raise RuntimeError("stop failed")
    monkeypatch.setattr(ctrl, "stop_boresight_engage_loop", _boom)
    t = _run_mission(ctrl)
    assert _wait(lambda: ctrl.is_mission_running())
    _drain(ctrl, t)


def test_an_automatic_launch_skips_when_a_mission_is_running(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)
    assert ctrl._mission_lock.acquire(blocking=False)
    try:
        ctrl.mission_f111()
    finally:
        ctrl._mission_lock.release()
    assert log == []


# ---------------------------------------------------------------------------
# Launch wiring: default_mission and the restart branch (ADR 145 pattern)
# ---------------------------------------------------------------------------

def _restart_ctrl(monkeypatch, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig(disable_hotkeys=True, **cfg))
    launched = []
    for name in ("j20", "su30", "jas39", "f111", "loiter"):
        monkeypatch.setattr(ctrl, f"mission_{name}",
                            lambda _n=name: launched.append(_n))
    return ctrl, launched


def test_battle_entry_launches_f111_when_it_is_the_default(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="f111")
    ctrl._start_default_mission()
    assert _wait(lambda: launched == ["f111"])
    assert ctrl._last_mission == "f111"


def test_respawn_restart_resumes_the_f111_mission(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch)
    ctrl._set_last_mission("f111")
    assert ctrl.restart_last_mission() is True
    assert _wait(lambda: launched == ["f111"])


def test_restart_with_no_prior_mission_uses_f111_as_the_default(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="f111")
    assert ctrl.restart_last_mission() is True
    assert _wait(lambda: launched == ["f111"])


# ---------------------------------------------------------------------------
# ADR 147 extended by ADR 149: f111 flies its own altitude doctrine
# ---------------------------------------------------------------------------

def test_the_f111_altitude_doctrine_applies_only_while_f111_is_the_mission(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, f111={"alt_floor_m": 3000})
    for mission in ("j20", "loiter", "jas39", "su30"):
        ctrl._set_last_mission(mission)
        assert ctrl.altitude_floor_override_m() is None, mission
        assert ctrl.sustain_climb_suppressed() is False, mission
    ctrl._set_last_mission("f111")
    assert ctrl.altitude_floor_override_m() == 3000.0
    assert ctrl.sustain_climb_suppressed() is True


def test_each_mission_reads_its_own_floor(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, f111={"alt_floor_m": 2800},
                      su30={"alt_floor_m": 3000})
    ctrl._set_last_mission("su30")
    assert ctrl.altitude_floor_override_m() == 3000.0
    ctrl._set_last_mission("f111")
    assert ctrl.altitude_floor_override_m() == 2800.0
    ctrl._set_last_mission("j20")
    assert ctrl.altitude_floor_override_m() is None


def test_without_an_alt_floor_key_f111_leaves_the_trees_doctrine_alone(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, su30={"alt_floor_m": 3000})
    assert "alt_floor_m" not in _FAST
    ctrl._set_last_mission("f111")
    assert ctrl.altitude_floor_override_m() is None
    assert ctrl.sustain_climb_suppressed() is False


_DOCTRINE_CLIMB = {"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
                   "confirm_reads": 1, "alt_floor_m": 4000,
                   "sustain": {"enabled": True, "enter_below_alt": 4000,
                               "exit_above_alt": 5000}}
_BT_CFG = {"disengage_after_s": 30, "disengage_hold_s": 10, "evade_hold_s": 10}


def _tree_snap(altitude):
    return AnalyzerSnapshot(
        health=250, missiles=4, flares=6, ring_short=0, ring_mid=0, ring_long=0,
        enemy_absent_seconds=0.0, altitude=altitude, is_respawning=False,
        incoming_detected=False, mission_running=True,
        game_state=GameState.GAME_BATTLE)


def test_the_tree_leaves_the_f111_level_off_alone_and_floors_at_3000(monkeypatch, caplog):
    """End to end through BehaviorTreeHandler, as ADR 147's su30 test: at 3192 m,
    armed, the tree's own doctrine climbs; f111's does not, and its floor is 3000 m."""
    from wingman.tick_handlers import BehaviorTreeHandler

    py_trees.blackboard.Blackboard.clear()
    try:
        ctrl = _make_ctrl(monkeypatch, f111={"alt_floor_m": 3000})
        handler = BehaviorTreeHandler(
            None, ctrl, dict(_BT_CFG, mode="shadow", climb=dict(_DOCTRINE_CLIMB)))
        writer = make_snapshot_writer()

        def selected(alt):
            writer.set("snapshot", _tree_snap(alt))
            handler._tree.tick()
            return selected_tactic(handler._tree)

        ctrl._set_last_mission("j20")
        assert selected(3192.0) == TACTIC_CLIMB
        ctrl._set_last_mission("f111")
        assert selected(3192.0) != TACTIC_CLIMB
        caplog.clear()
        with caplog.at_level("WARNING"):
            assert selected(2900.0) == TACTIC_CLIMB
        floor = [m for m in _messages(caplog) if "ALTITUDE FLOOR" in m]
        assert floor and all("below 3000m" in m for m in floor)
    finally:
        py_trees.blackboard.Blackboard.clear()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_shipped_f111_block_matches_the_mission_document(shipped_cfg):
    """docs/missions/f111.md: 3000 m, -10 degrees, unsweep at 3000 m within 30 s."""
    f111 = shipped_cfg["f111_mission"]
    assert f111["climb_alt_m"] == 3000
    assert f111["nose_angle_deg"] == -10
    assert f111["unsweep_alt_m"] == 3000
    assert f111["unsweep_timeout_s"] == 30
    assert f111["alt_floor_m"] == 3000
    assert f111["wingsweep_tap_s"] == pytest.approx(0.1)


def test_the_f111_angle_step_copies_su30s_numbers(shipped_cfg):
    f111, su30 = shipped_cfg["f111_mission"], shipped_cfg["su30_mission"]
    for key in ("climb_max_s", "angle_tolerance_deg", "angle_confirm_reads",
                "angle_pulse_s", "angle_max_s", "tick_s"):
        assert f111[key] == su30[key], key


def test_the_shipped_default_mission_is_unchanged(shipped_cfg):
    """README default 3: adding f111 does not change the shipped default."""
    assert shipped_cfg["mission"]["default_mission"] == "su30"


def test_the_controller_reads_the_shipped_f111_block(shipped_cfg, monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig.from_config(shipped_cfg, disable_hotkeys=True))
    assert ctrl._f111_climb_alt_m == 3000.0
    assert ctrl._f111_nose_angle_deg == -10.0
    assert ctrl._f111_unsweep_alt_m == 3000.0
    assert ctrl._f111_unsweep_timeout_s == 30.0
    assert ctrl._f111_wingsweep_tap_s == pytest.approx(0.1)
    ctrl._set_last_mission("f111")
    assert ctrl.altitude_floor_override_m() == 3000.0


def test_the_schema_accepts_f111_and_catches_a_misspelt_key(shipped_cfg):
    assert validate_config(shipped_cfg) == []
    ok = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    ok["mission"]["default_mission"] = "f111"
    assert validate_config(ok) == []
    bad = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    bad["f111_mission"]["unsweep_timout_s"] = 30        # typo
    assert validate_config(bad), "a misspelt f111_mission key must be caught"

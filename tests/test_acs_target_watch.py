"""ADR 191: a life looks for a target, level, before mission_acs climbs.

Before it, every spawn went nose-up: the tree's floor climb was selected at the
spawn guard's hand-off (2026-11-18 10:33:35, 561 m, three seconds before the
mission started), and the mission itself looked for a target once and climbed.
Of 846 climbs on 11-16 to 11-18, 207 met a target within 2 s and 279 within 3 s.
"""

import threading
import time

import pytest

import wingman.controller as controller_module
from test_mission_acs import _Analyzer, _make_ctrl, _record, _run_mission, _wait


class _Tracker:
    """A lock from the given time on (None: never)."""

    def __init__(self, at_s=None):
        self.t0 = time.time()
        self.at_s = at_s

    def last_observation(self):
        visible = self.at_s is not None and time.time() - self.t0 >= self.at_s
        return {"visible": visible, "error_norm": 0.1, "error_norm_y": 0.0}


def _ctrl(monkeypatch, watch_s, lock_at=None):
    return _make_ctrl(monkeypatch, analyzer=_Analyzer([(900, 0)]),
                      tracker=_Tracker(lock_at),
                      acs={"yield_to_target": True, "target_watch_s": watch_s})


def test_a_target_inside_the_window_skips_the_climb(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch, watch_s=0.5, lock_at=0.2)
    log = _record(ctrl, monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        t = _run_mission(ctrl)
        t.join(timeout=5.0)
    kinds = [e[0] for e in log]
    assert "pursue" in kinds and "climb" not in kinds
    assert any("at the start, no climb" in r.getMessage() and "(+0." in r.getMessage()
               for r in caplog.records)


def test_no_target_in_the_window_climbs_after_it(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch, watch_s=0.3)
    log = _record(ctrl, monkeypatch)
    start = time.time()
    with caplog.at_level("INFO", logger="wingman.controller"):
        t = _run_mission(ctrl)
        assert _wait(lambda: ("climb", 3000) in log)
    assert time.time() - start >= 0.3, "the climb waits out the window"
    assert any("no target in" in r.getMessage() for r in caplog.records)
    assert ctrl.start_watch_remaining_s() == 0.0, "the mission's own climb is not held off"
    ctrl._mission_cancel.set()
    t.join(timeout=5.0)


def test_a_zero_window_is_the_old_single_look(monkeypatch):
    ctrl = _ctrl(monkeypatch, watch_s=0.0, lock_at=0.2)
    log = _record(ctrl, monkeypatch)
    t = _run_mission(ctrl)
    assert _wait(lambda: ("climb", 3000) in log, timeout=0.2)
    ctrl._mission_cancel.set()
    t.join(timeout=5.0)


# --- climb_mode inside the window ----------------------------------------------

def _bare_ctrl(monkeypatch, watch_s=2.0):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return _make_ctrl(monkeypatch, acs={"yield_to_target": True, "target_watch_s": watch_s})


def _held_off(caplog):
    return any("climb held off" in r.getMessage() for r in caplog.records)


def test_an_ordinary_climb_is_held_off_inside_the_window(monkeypatch, caplog):
    ctrl = _bare_ctrl(monkeypatch)
    ctrl.arm_start_watch("respawn")
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode()
    assert _held_off(caplog) and not ctrl._climbing.is_set()


def test_a_hard_emergency_still_climbs_inside_the_window(monkeypatch, caplog):
    """Terrain or time-to-ground outranks the watch."""
    ctrl = _bare_ctrl(monkeypatch)
    ctrl.arm_start_watch("respawn")
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode(emergency=True)
    assert not _held_off(caplog)


def test_outside_the_window_nothing_is_held_off(monkeypatch, caplog):
    ctrl = _bare_ctrl(monkeypatch)
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode()
    assert not _held_off(caplog)


def test_arming_stops_an_ordinary_climb_already_running(monkeypatch):
    ctrl = _bare_ctrl(monkeypatch)
    ctrl._climbing.set()
    ctrl._climb_stop.clear()
    ctrl.arm_start_watch("respawn")
    assert ctrl._climb_stop.is_set()


def test_arming_leaves_an_emergency_climb_running(monkeypatch):
    ctrl = _bare_ctrl(monkeypatch)
    ctrl._climbing.set()
    ctrl._climb_stop.clear()
    ctrl._climb_emergency_requested = True
    ctrl.arm_start_watch("respawn")
    assert not ctrl._climb_stop.is_set()


def test_the_respawn_hand_off_window_is_the_lifes_only_window(monkeypatch):
    """The spawn guard arms it about 2.7 s before the restart; the mission does
    not arm a second one for the same life."""
    ctrl = _ctrl(monkeypatch, watch_s=0.2)
    log = _record(ctrl, monkeypatch)
    ctrl.arm_start_watch("respawn")
    time.sleep(0.3)                                   # the hand-off gap
    start = time.time()
    t = _run_mission(ctrl)
    assert _wait(lambda: ("climb", 3000) in log)
    assert time.time() - start < 0.15
    ctrl._mission_cancel.set()
    t.join(timeout=5.0)


@pytest.mark.parametrize("cfg,expected", [({}, 2.0), ({"target_watch_s": 0}, 0.0)])
def test_the_window_length_comes_from_the_acs_block(monkeypatch, cfg, expected):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    from wingman.controller import Controller
    from wingman.controller_config import ControllerConfig
    ctrl = Controller((0, 0, 1920, 1200), exit_event=threading.Event(),
                      config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                              acs=cfg))
    assert ctrl._acs_target_watch_s == expected


# --- D4: the spawn guard spawns an acs life level ------------------------------

def _guard_ctrl(monkeypatch, mission, watch_s=2.0):
    ctrl = _make_ctrl(monkeypatch, acs={"yield_to_target": True, "target_watch_s": watch_s,
                                        "spawn_watch_only": True})
    ctrl._spawn_guard_enabled = True
    ctrl._sg_pulse_s = ctrl._sg_observe_s = 0.05
    ctrl._last_mission = mission
    presses = []
    monkeypatch.setattr(ctrl, "_climb_key",
                        lambda key, press, action: presses.append((key, press, action)))
    ts = {"v": 1.0}
    monkeypatch.setattr(ctrl, "_telemetry_stable_ts", lambda: ts["v"])
    return ctrl, presses, ts


def test_the_spawn_guard_presses_nothing_for_an_acs_life(monkeypatch, caplog):
    """2026-11-18 12:10:48: its pulses had the nose at +90 deg, 285 kph."""
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "acs")
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.start_spawn_guard()
        time.sleep(0.3)
        ts["v"] = 2.0                                   # the HUD is back
        assert _wait(lambda: not ctrl.is_spawn_guarding())
    assert [p for p in presses if p[1]] == []
    assert ctrl.start_watch_remaining_s() > 0.0, "the hand-off opens the watch"
    assert any("watch only" in r.getMessage() for r in caplog.records)


def test_the_spawn_guard_still_pulses_for_j20(monkeypatch):
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "j20")
    ctrl.start_spawn_guard()
    time.sleep(0.3)
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())
    assert any(p[1] for p in presses)
    assert ctrl.start_watch_remaining_s() == 0.0


def test_the_spawn_guard_still_pulses_with_no_window(monkeypatch):
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "acs", watch_s=0.0)
    ctrl.start_spawn_guard()
    time.sleep(0.3)
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())
    assert any(p[1] for p in presses)


def test_the_last_lifes_climb_ends_when_the_respawn_starts(monkeypatch):
    """2026-11-18 12:59:53: an 11.2 s climb ran through the respawn screen and the
    new life read +90 deg 1.5 s after the hand-off."""
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "acs")
    ctrl._climbing.set()
    ctrl._climb_stop.clear()
    ctrl._climb_emergency_requested = True           # even an emergency: that life is over
    ctrl.start_spawn_guard()
    assert ctrl._climb_stop.is_set()
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())


def test_a_j20_respawn_leaves_a_running_climb_alone(monkeypatch):
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "j20")
    ctrl._climbing.set()
    ctrl._climb_stop.clear()
    ctrl.start_spawn_guard()
    assert not ctrl._climb_stop.is_set()
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())


def test_an_ordinary_climb_is_held_off_before_the_hand_off_too(monkeypatch, caplog):
    """2026-11-18 13:21:47: the floor climb pressed nose-up for 0.9 s between the
    respawn and the hand-off, before the watch had opened."""
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "acs")
    ctrl.start_spawn_guard()
    with caplog.at_level("INFO", logger="wingman.controller"):
        ctrl.climb_mode()
    assert _held_off(caplog) and not ctrl._climbing.is_set()
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())


def test_the_shipped_spawn_guard_still_pulses_for_acs(monkeypatch):
    """D7: watch-only spawns raised terrain deaths (8 in 34 against 5 in 106)."""
    from wingman.config_schema import schema_default
    assert schema_default("acs_mission.spawn_watch_only") is False
    ctrl, presses, ts = _guard_ctrl(monkeypatch, "acs")
    ctrl._acs_spawn_watch_only = False
    ctrl.start_spawn_guard()
    time.sleep(0.3)
    ts["v"] = 2.0
    assert _wait(lambda: not ctrl.is_spawn_guarding())
    assert any(p[1] for p in presses)
    assert ctrl.start_watch_remaining_s() > 0.0, "the hand-off still opens the watch"

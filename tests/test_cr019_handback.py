"""Review 019 CR-019-01 to CR-019-04 — nothing starts flying after the airframe
has been handed back, and the takeover cleanup runs outside the FSM lock.

CR-019-01: an automatic mission restart (the respawn and disengage restarts)
is refused after Backspace (standby), an exit request, or a manual takeover.
CR-019-02: eject_and_dive and pursue_and_engage refuse likewise, and a stop
that lands as a pursuit ends on ammo or the cap wins over the dive hand-off.
CR-019-03: the MANUAL_TAKEOVER subscribers run after _trigger releases
_state_lock, and still before trigger_event returns.
CR-019-04: cleanup() sets _disengage_stop.
"""

import copy
import threading
import time
import types
from pathlib import Path

import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameEvent, GameState, GameStateAnalyzer
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from constants import CONFIG_PATH
from tests.test_pursuit_mode import (
    _AnalyzerStub, _CaptureStub, _TrackerStub, _wait_for_pursuit_to_settle,
)


class _StateAnalyzer(_AnalyzerStub):
    def __init__(self, state=GameState.GAME_BATTLE, ammo=2):
        super().__init__(ammo=ammo)
        self.game_state = state


def _make_ctrl(monkeypatch, analyzer=None, capture=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer if analyzer is not None else _StateAnalyzer(),
        exit_event=threading.Event(),
        capture=capture,
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            telemetry={"eject_closed_loop": {"enabled": False, "legacy_nose_hold_s": 0.05,
                                             "eject_max_s": 0.2}},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0},
        ),
    )


def _started_missions(monkeypatch, ctrl):
    started = []
    for name in ("mission_j20", "mission_loiter", "mission_su30", "mission_jas39",
                 "mission_f111"):
        monkeypatch.setattr(ctrl, name, lambda *a, _n=name, **k: started.append(_n))
    return started


# ---------------------------------------------------------------------------
# CR-019-01
# ---------------------------------------------------------------------------

class TestAutomaticRestartRefused:
    def test_restart_runs_normally(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        started = _started_missions(monkeypatch, ctrl)
        ctrl._set_last_mission("j20")
        assert ctrl.restart_last_mission() is True
        time.sleep(0.05)
        assert started == ["mission_j20"]

    def test_refused_in_standby(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        started = _started_missions(monkeypatch, ctrl)
        ctrl._set_last_mission("j20")
        ctrl._operator_stop_event.set()
        assert ctrl.restart_last_mission() is False
        time.sleep(0.05)
        assert started == []

    def test_refused_on_exit(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        started = _started_missions(monkeypatch, ctrl)
        ctrl._set_last_mission("j20")
        ctrl._exit_event.set()
        assert ctrl.restart_last_mission() is False
        assert started == []

    def test_refused_during_manual_takeover(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, analyzer=_StateAnalyzer(GameState.GAME_BATTLE_MANUAL))
        started = _started_missions(monkeypatch, ctrl)
        ctrl._set_last_mission("j20")
        assert ctrl.restart_last_mission() is False
        assert started == []

    def test_refused_default_fallback_too(self, monkeypatch):
        """No mission recorded: the configured default is refused the same way."""
        ctrl = _make_ctrl(monkeypatch)
        started = _started_missions(monkeypatch, ctrl)
        ctrl._operator_stop_event.set()
        assert ctrl.restart_last_mission() is False
        time.sleep(0.05)
        assert started == []

    def test_backspace_mid_disengage_does_not_restart_the_mission(self, monkeypatch):
        """The CR-019-01 scenario end to end: roll interrupted by Backspace."""
        ctrl = _make_ctrl(monkeypatch)
        started = _started_missions(monkeypatch, ctrl)
        released = []
        monkeypatch.setattr(controller_module, "keyboard_module",
                            types.SimpleNamespace(release=released.append))
        monkeypatch.setattr(controller_module, "_press_key", lambda k: None)
        monkeypatch.setattr(ctrl, "start_search_and_destroy_loop", lambda: None)
        monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop", lambda: None)
        ctrl._set_last_mission("j20")
        ctrl.disengage_roll_right(duration=5.0)
        time.sleep(0.2)
        ctrl._operator_stop_event.set()
        ctrl._exit_event.set()
        ctrl._disengage_thread.join(timeout=3.0)
        assert not ctrl._disengage_thread.is_alive()
        assert started == [], "a disengage ended by Backspace must not restart the mission"
        assert released, "the roll key is still released"


# ---------------------------------------------------------------------------
# CR-019-02
# ---------------------------------------------------------------------------

class TestEjectAndPursuitRefused:
    def test_eject_and_dive_refused_in_standby_leaves_the_stop_set(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch)
        ctrl._operator_stop_event.set()
        ctrl.eject_and_dive()
        assert ctrl._eject_thread is None
        assert ctrl._eject_stop.is_set()
        assert ctrl._eject_stop_reason == "operator stop (standby)"

    def test_eject_and_dive_refused_during_takeover(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, analyzer=_StateAnalyzer(GameState.GAME_BATTLE_MANUAL))
        ctrl.eject_and_dive()
        assert ctrl._eject_thread is None and ctrl._eject_stop.is_set()

    def test_pursue_and_engage_refused_on_exit(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, capture=_CaptureStub())
        ctrl.set_target_tracker(_TrackerStub())
        ctrl._exit_event.set()
        ctrl.pursue_and_engage()
        assert ctrl._pursuing_thread is None and not ctrl.is_pursuing()
        assert ctrl._eject_stop.is_set()

    def test_a_stop_as_the_pursuit_ends_on_ammo_wins_over_the_dive(self, monkeypatch, caplog):
        """The loop breaks for ammo; a respawn stop lands before the hand-off."""
        ctrl = _make_ctrl(monkeypatch, analyzer=_StateAnalyzer(ammo=0), capture=_CaptureStub())
        ctrl.set_target_tracker(_TrackerStub())
        real_release = ctrl.release_tracking_holds

        def release_then_stop(why="release"):
            real_release(why=why)
            if not ctrl._eject_stop.is_set():
                ctrl.stop_eject_sequence("respawn_detected")
        monkeypatch.setattr(ctrl, "release_tracking_holds", release_then_stop)
        done = []
        with caplog.at_level("INFO", logger="wingman.controller"):
            ctrl.pursue_and_engage(on_complete=lambda: done.append(True),
                                   weapon_already_switched=True)
            _wait_for_pursuit_to_settle(ctrl)
        assert ctrl._eject_thread is None, "no dive after the stop"
        assert done == [True], "the external-stop path still calls on_complete"
        assert any("not handing off to eject_and_dive" in r.getMessage() for r in caplog.records)

    def test_the_pursuit_still_falls_through_without_a_stop(self, monkeypatch):
        ctrl = _make_ctrl(monkeypatch, analyzer=_StateAnalyzer(ammo=0), capture=_CaptureStub())
        ctrl.set_target_tracker(_TrackerStub())
        ctrl.pursue_and_engage(weapon_already_switched=True)
        _wait_for_pursuit_to_settle(ctrl)
        assert ctrl._eject_thread is not None, "ammo exhausted still hands off to the dive"


# ---------------------------------------------------------------------------
# CR-019-03
# ---------------------------------------------------------------------------

def _analyzer():
    with Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        return GameStateAnalyzer(copy.deepcopy(yaml.safe_load(fh)))


def test_takeover_subscribers_run_outside_the_state_lock_but_before_return():
    a = _analyzer()
    try:
        seen = []
        a.subscribe(GameEvent.MANUAL_TAKEOVER,
                    lambda: seen.append(a._state_lock.locked()), name="t")
        assert a.trigger_event("manual_force_battle")
        assert a.trigger_event("manual_takeover")
        assert seen == [False], "must run once, with _state_lock released"
        assert a.game_state == GameState.GAME_BATTLE_MANUAL
    finally:
        a.cleanup()


def test_takeover_emits_before_cancel_mission_as_before():
    a = _analyzer()
    try:
        order = []
        a.subscribe(GameEvent.MANUAL_TAKEOVER, lambda: order.append("takeover"), name="t")
        a.subscribe(GameEvent.CANCEL_MISSION, lambda: order.append("cancel"), name="c")
        a.trigger_event("manual_force_battle")
        a.trigger_event("manual_takeover")
        assert order == ["takeover", "cancel"]
    finally:
        a.cleanup()


def test_a_hook_outside_trigger_still_emits_immediately():
    a = _analyzer()
    try:
        seen = []
        a.subscribe(GameEvent.MANUAL_TAKEOVER, lambda: seen.append(1), name="t")
        a.on_enter_GAME_BATTLE_MANUAL()
        assert seen == [1]
    finally:
        a.cleanup()


# ---------------------------------------------------------------------------
# CR-019-04
# ---------------------------------------------------------------------------

def test_cleanup_sets_the_disengage_stop(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.cleanup()
    assert ctrl._disengage_stop.is_set()

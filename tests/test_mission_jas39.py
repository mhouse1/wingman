"""mission_jas39 (ADR 145, docs/missions/jas39.md): mission_j20 plus the cloak.

  1. fly the spawn heading for 10 s     2. engage with search_and_destroy
  3. press 'q' every 3 s (the cloak)    4. run until cancelled

The mission is J20's shape with one new routine, the cloak loop, so these tests
pin the step order and the properties that make a key loop dangerous on a live
aircraft: a press that overlaps the next, a loop that outlives its mission into
the next life or into manual flight, and a press that ignores manual takeover.

The mission has no hotkey of its own. mission.default_mission picks it, and the
'u' hotkey launches whichever mission that names (operator, 2026-09-24).
"""

import logging
import pathlib
import threading
import time

import pytest
import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.config_schema import validate_config
from wingman.controller import (Controller, FIRE_ACTIVE_WEAPON, MISSION_J20_KEY,
                                PADLOCK_CAMERA, SPECIAL_ABILITY)
from wingman.controller_config import ControllerConfig

_ROOT = pathlib.Path(__file__).resolve().parents[1]


class _Analyzer:
    def __init__(self, state=GameState.GAME_BATTLE):
        self.game_state = state
        self._last_battle_event_ts = 0.0

    def trigger_event(self, _name):
        return True


def _make_ctrl(monkeypatch, analyzer=None, jas39=None, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                jas39=jas39 or {}, **cfg),
    )


def _record(ctrl, monkeypatch):
    """Replace the mission's building blocks with recorders on one ordered log."""
    log = []
    monkeypatch.setattr(ctrl, "arm_turn_guard",
                        lambda seconds=None: log.append(("turn_guard", seconds)))
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop",
                        lambda: log.append(("sdl_start",)))
    monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop",
                        lambda: log.append(("sdl_stop",)))
    monkeypatch.setattr(ctrl, "start_cloak_loop",
                        lambda: log.append(("cloak_start",)))
    monkeypatch.setattr(ctrl, "stop_cloak_loop",
                        lambda: log.append(("cloak_stop",)))
    monkeypatch.setattr(ctrl, "start_boresight_engage_loop",
                        lambda: log.append(("boresight_start",)))
    return log


def _run_mission(ctrl):
    t = threading.Thread(target=ctrl.mission_jas39, daemon=True)
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
    """Cancel and wait the mission out (mission_jas39 clears the cancel on
    entry, so keep re-asserting it until the lock is actually free)."""
    deadline = time.time() + 5.0
    while ctrl.is_mission_running() and time.time() < deadline:
        ctrl._mission_cancel.set()
        time.sleep(0.02)
    thread.join(timeout=3.0)
    assert not ctrl.is_mission_running()


def _key_presses(ctrl, key):
    with ctrl._action_intents_lock:
        return [i for i in ctrl._action_intents
                if i["action_type"] == "key_press" and i["key"] == key]


# ---------------------------------------------------------------------------
# The sequence
# ---------------------------------------------------------------------------

def test_the_four_steps_run_in_the_documented_order(monkeypatch):
    """turn guard, search_and_destroy, cloak loop — then hold until cancelled."""
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    time.sleep(0.1)

    assert log == [("turn_guard", 10.0), ("sdl_start",), ("cloak_start",)]
    assert ctrl.is_mission_running(), "step 4: the mission holds until cancelled"
    _drain(ctrl, t)


def test_the_turn_guard_reads_the_mission_block(monkeypatch):
    """The spawn-heading span is the jas39_mission key, not J20's."""
    ctrl = _make_ctrl(monkeypatch, jas39={"turn_guard_s": 4.0}, j20_turn_guard_s=25.0)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    _drain(ctrl, t)

    assert log[0] == ("turn_guard", 4.0)


def test_engagement_is_search_and_destroy_like_j20(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    _drain(ctrl, t)

    assert ("sdl_start",) in log
    assert ("boresight_start",) not in log


def test_each_step_logs_one_line(monkeypatch, caplog):
    """README default 6: one step N/M line per bullet, readable in a live trial."""
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        t = _run_mission(ctrl)
        assert _wait(lambda: "step 4/4" in caplog.text)
        _drain(ctrl, t)
    assert ("cloak_start",) in log

    lines = [r.getMessage() for r in caplog.records
             if "mission_jas39 - step" in r.getMessage()]
    assert [line.split("step ")[1][:3] for line in lines] == ["1/4", "2/4", "3/4", "4/4"]
    assert "10s turn guard" in lines[0]
    assert "q every 3.0s" in lines[2]


# ---------------------------------------------------------------------------
# Every exit ends both loops and releases the lock
# ---------------------------------------------------------------------------

def test_cancel_stops_both_loops_and_releases_the_lock(monkeypatch):
    """A respawn or takeover must not carry either key loop into the next life."""
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    _drain(ctrl, t)

    assert ("cloak_stop",) in log
    assert ("sdl_stop",) in log
    assert not ctrl.is_mission_running()


def test_an_exit_request_ends_the_mission(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    ctrl._exit_event.set()
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert ("cloak_stop",) in log and ("sdl_stop",) in log
    assert not ctrl.is_mission_running()


def test_a_failing_stop_neither_skips_the_other_nor_keeps_the_lock(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)
    def _boom():
        raise RuntimeError("stop failed")
    monkeypatch.setattr(ctrl, "stop_cloak_loop", _boom)

    t = _run_mission(ctrl)
    assert _wait(lambda: ("cloak_start",) in log)
    _drain(ctrl, t)

    assert ("sdl_stop",) in log, "a failing cloak stop must not skip the other loop"
    assert not ctrl.is_mission_running()


def test_a_failing_start_still_stops_the_loops_and_releases_the_lock(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)
    def _boom():
        raise RuntimeError("start failed")
    monkeypatch.setattr(ctrl, "start_cloak_loop", _boom)

    t = _run_mission(ctrl)
    t.join(timeout=5.0)

    assert not t.is_alive()
    assert ("sdl_stop",) in log and ("cloak_stop",) in log
    assert not ctrl.is_mission_running()


def test_automatic_launch_skips_when_a_mission_is_running(monkeypatch):
    """Like mission_j20: no preempt. A running mission is not interrupted."""
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)
    assert ctrl._mission_lock.acquire(blocking=False)
    try:
        ctrl.mission_jas39()
    finally:
        ctrl._mission_lock.release()

    assert log == []


# ---------------------------------------------------------------------------
# The cloak loop (the one new routine)
# ---------------------------------------------------------------------------

def test_the_cloak_loop_presses_q_at_once_then_waits_the_interval(monkeypatch):
    """First press straight away (the cloak is available at spawn), then none
    until the interval has passed."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})

    ctrl.start_cloak_loop()
    try:
        assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1, timeout=0.5)
        time.sleep(0.6)
        assert len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1, \
            "a second press inside the 3 s interval"
    finally:
        ctrl.stop_cloak_loop()


def test_the_cloak_loop_keeps_pressing_every_interval(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 0.05})

    ctrl.start_cloak_loop()
    try:
        assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) >= 3)
    finally:
        ctrl.stop_cloak_loop()


def test_each_cloak_press_is_a_short_tap(monkeypatch):
    """_execute_key_press defaults to a 2.5 s hold, which on a 3 s loop would
    keep 'q' down most of the time. The helper must pass 0.1 s."""
    ctrl = _make_ctrl(monkeypatch)

    ctrl.activate_special_weapon()

    presses = _key_presses(ctrl, SPECIAL_ABILITY)
    assert len(presses) == 1
    assert presses[0]["hold_seconds"] == pytest.approx(0.1)


def test_a_cloak_press_logs_only_at_debug(monkeypatch, caplog):
    """The loop presses every few seconds; per-press lines stay at DEBUG,
    where `make rd` still shows each one."""
    ctrl = _make_ctrl(monkeypatch)

    with caplog.at_level(logging.DEBUG, logger="wingman.controller"):
        ctrl.activate_special_weapon()
        ctrl.activate_special_weapon()

    assert [r for r in caplog.records if r.levelno >= logging.INFO] == []
    assert caplog.text.count(
        "activate_special_weapon - pressing 'q' key for 0.1 seconds") == 2


def test_the_cloak_loop_logs_its_start_and_stop(monkeypatch, caplog):
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})

    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.start_cloak_loop()
        assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
        ctrl.stop_cloak_loop()

    assert "cloak loop started (q every 3.0s)" in caplog.text
    assert "cloak loop stopped after 1 press(es)" in caplog.text


def test_manual_takeover_suppresses_the_cloak_press(monkeypatch):
    """SAF-001: in GAME_BATTLE_MANUAL the operator owns every key but flares."""
    ctrl = _make_ctrl(monkeypatch, analyzer=_Analyzer(GameState.GAME_BATTLE_MANUAL))

    ctrl.activate_special_weapon()

    assert _key_presses(ctrl, SPECIAL_ABILITY) == []


def test_stop_ends_the_loop_within_a_slice(monkeypatch):
    """Stopping mid-interval must not wait the rest of a 3 s interval out."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
    thread = ctrl._cloak_thread

    started = time.time()
    ctrl.stop_cloak_loop()

    assert time.time() - started < 1.0
    assert not thread.is_alive()
    assert ctrl._cloak_thread is None


def test_a_cancel_ends_the_loop_on_its_own(monkeypatch):
    """The loop also watches _mission_cancel, as every mission loop does."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
    thread = ctrl._cloak_thread

    ctrl._mission_cancel.set()

    assert _wait(lambda: not thread.is_alive(), timeout=1.0)
    ctrl.stop_cloak_loop()


def test_a_second_start_does_not_run_two_loops(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    try:
        first = ctrl._cloak_thread
        ctrl.start_cloak_loop()
        assert ctrl._cloak_thread is first
        time.sleep(0.3)
        assert len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1
    finally:
        ctrl.stop_cloak_loop()


def test_the_loop_restarts_after_a_stop(monkeypatch):
    """Every life starts a fresh loop (respawn restart)."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
    ctrl.stop_cloak_loop()

    ctrl.start_cloak_loop()
    try:
        assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 2)
    finally:
        ctrl.stop_cloak_loop()


def test_manual_takeover_stops_the_cloak_loop(monkeypatch):
    """README section 5 rule 10: every key writer stops on takeover."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
    thread = ctrl._cloak_thread

    ctrl.release_for_manual_takeover()

    assert not thread.is_alive()
    assert ctrl._cloak_thread is None


def test_cleanup_stops_the_cloak_loop(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 3.0})
    ctrl.start_cloak_loop()
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) == 1)
    thread = ctrl._cloak_thread

    ctrl.cleanup()

    assert not thread.is_alive()
    assert ctrl._cloak_thread is None


# ---------------------------------------------------------------------------
# End to end through the real loops
# ---------------------------------------------------------------------------

def test_the_real_mission_cloaks_fires_and_padlocks_then_stops_everything(monkeypatch):
    """The real search_and_destroy and cloak loops, only the key injection
    simulated: 'q', fire and padlock all go down, and nothing is pressed after
    the mission ends."""
    ctrl = _make_ctrl(monkeypatch, jas39={"cloak_press_interval_s": 0.05},
                      weapon_loop_interval=0.1)

    t = _run_mission(ctrl)
    assert _wait(lambda: len(_key_presses(ctrl, SPECIAL_ABILITY)) >= 2
                 and _key_presses(ctrl, FIRE_ACTIVE_WEAPON)
                 and _key_presses(ctrl, PADLOCK_CAMERA))
    _drain(ctrl, t)

    pressed = len(_key_presses(ctrl, SPECIAL_ABILITY))
    time.sleep(0.4)
    assert len(_key_presses(ctrl, SPECIAL_ABILITY)) == pressed, \
        "the cloak loop outlived its mission"
    assert ctrl._cloak_thread is None
    assert ctrl._sdl_padlock_thread is None and ctrl._sdl_weapon_thread is None


def test_the_padlock_is_not_blocked_for_jas39(monkeypatch):
    """ADR 144's padlock block is su30's alone; JAS39 engages with the padlock."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl._set_last_mission("jas39")
    assert ctrl.is_padlock_blocked() is False


def test_mission_j20_does_not_start_the_cloak_loop(monkeypatch):
    """J20 is untouched: the cloak belongs to the JAS39 mission only."""
    ctrl = _make_ctrl(monkeypatch)
    log = _record(ctrl, monkeypatch)

    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()
    assert _wait(lambda: ("sdl_start",) in log)
    _drain(ctrl, t)

    assert ("cloak_start",) not in log


# ---------------------------------------------------------------------------
# Wiring: 'u' launches the configured mission; restart and battle entry
# ---------------------------------------------------------------------------

class _KeyboardStub:
    def __init__(self):
        self.handlers = {}

    def on_press_key(self, key, callback, suppress=False):
        self.handlers[key] = callback

    def add_hotkey(self, _key, _callback):
        return None


class _ThreadStub:
    started = []

    def __init__(self, target=None, daemon=None, args=None, kwargs=None):
        self._target, self._kwargs = target, kwargs or {}

    def start(self):
        _ThreadStub.started.append((self._target, self._kwargs))


class _StateAnalyzer:
    def __init__(self, state):
        self.game_state = state
        self.trigger_calls = []
        self._last_battle_event_ts = 0.0

    def trigger_event(self, name):
        self.trigger_calls.append(name)
        return True


def _hotkey_ctrl(monkeypatch, state, **cfg):
    keyboard = _KeyboardStub()
    monkeypatch.setattr(controller_module, "keyboard_module", keyboard)
    monkeypatch.setattr(controller_module.threading, "Thread", _ThreadStub)
    analyzer = _StateAnalyzer(state)
    ctrl = Controller((0, 0, 1920, 1200), analyzer=analyzer,
                      config=ControllerConfig(**cfg))
    _ThreadStub.started = []
    return ctrl, keyboard, analyzer


@pytest.mark.parametrize("mission", ["jas39", "su30", "j20"])
def test_u_launches_the_configured_mission(monkeypatch, mission):
    """ADR 145: missions have no hotkeys of their own; 'u' starts whichever
    mission mission.default_mission names."""
    ctrl, keyboard, analyzer = _hotkey_ctrl(monkeypatch, GameState.GAME_LOBBY,
                                            default_mission=mission)

    keyboard.handlers[MISSION_J20_KEY](object())

    assert analyzer.trigger_calls == ["manual_force_battle"]
    assert ctrl._last_mission == mission, "respawn restarts whichever mission ran last"
    assert len(_ThreadStub.started) == 1
    target, kwargs = _ThreadStub.started[0]
    assert target == getattr(ctrl, f"mission_{mission}")
    assert kwargs == {}, "'u' skips, rather than preempts, a running mission"


def test_u_without_a_configured_mission_still_launches_j20(monkeypatch):
    """A ControllerConfig that names no mission (tests, replay lanes) keeps the
    pre-ADR-145 behaviour exactly."""
    ctrl, keyboard, _ = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE)

    keyboard.handlers[MISSION_J20_KEY](object())

    assert ctrl._last_mission == "j20"
    assert _ThreadStub.started[0][0] == ctrl.mission_j20


def test_u_logs_which_mission_it_starts(monkeypatch, caplog):
    ctrl, keyboard, _ = _hotkey_ctrl(monkeypatch, GameState.GAME_LOBBY,
                                     default_mission="jas39")

    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        keyboard.handlers[MISSION_J20_KEY](object())

    assert ("'u' key pressed - starting the configured mission (jas39, "
            "state=GAME_LOBBY)") in caplog.text
    assert "mission 'jas39' started" in caplog.text
    assert _ThreadStub.started[0][0] == ctrl.mission_jas39


def test_u_while_a_mission_is_flying_changes_nothing(monkeypatch):
    """Review finding 1: a skipped launch must not relabel the mission that is
    actually flying. Relabelling su30 as jas39 turned su30's padlock block off,
    made the next respawn restart the wrong mission, and reset the 2 s
    takeover grace, so Enter and the arrows were briefly ignored."""
    ctrl, keyboard, analyzer = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE,
                                            default_mission="jas39")
    ctrl._set_last_mission("su30")              # su30 started with 'o'
    battle_since = ctrl._game_battle_since
    assert ctrl._mission_lock.acquire(blocking=False)   # ...and is flying
    try:
        keyboard.handlers[MISSION_J20_KEY](object())
    finally:
        ctrl._mission_lock.release()

    assert _ThreadStub.started == []
    assert ctrl._last_mission == "su30"
    assert ctrl._game_battle_since == battle_since
    assert analyzer.trigger_calls == []


def test_u_during_a_cancelled_missions_teardown_still_launches(monkeypatch):
    """The lock can still be held by a cancelled mission that is unwinding,
    for example straight after a manual takeover. 'u' must not treat that as
    a mission flying, or resuming from manual would silently do nothing."""
    ctrl, keyboard, _ = _hotkey_ctrl(monkeypatch, GameState.GAME_BATTLE_MANUAL,
                                     default_mission="jas39")
    assert ctrl._mission_lock.acquire(blocking=False)
    ctrl._mission_cancel.set()                   # tearing down
    try:
        keyboard.handlers[MISSION_J20_KEY](object())
    finally:
        ctrl._mission_lock.release()

    assert ctrl._last_mission == "jas39"
    assert _ThreadStub.started[0][0] == ctrl.mission_jas39


def test_u_resuming_from_manual_launches_the_configured_mission(monkeypatch):
    ctrl, keyboard, analyzer = _hotkey_ctrl(
        monkeypatch, GameState.GAME_BATTLE_MANUAL, default_mission="jas39")

    keyboard.handlers[MISSION_J20_KEY](object())

    assert analyzer.trigger_calls == ["manual_force_battle"]
    assert _ThreadStub.started[0][0] == ctrl.mission_jas39


def _restart_ctrl(monkeypatch, **cfg):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig(disable_hotkeys=True, **cfg))
    launched = []
    for name in ("j20", "su30", "jas39", "loiter"):
        monkeypatch.setattr(ctrl, f"mission_{name}",
                            lambda name=name: launched.append(name))
    return ctrl, launched


def test_respawn_restart_resumes_the_jas39_mission(monkeypatch, caplog):
    ctrl, launched = _restart_ctrl(monkeypatch)
    ctrl._set_last_mission("jas39")

    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        assert ctrl.restart_last_mission() is True
    assert _wait(lambda: launched == ["jas39"])
    assert "restarting last mission (JAS39)" in caplog.text


def test_battle_entry_launches_jas39_when_configured(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="jas39")

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["jas39"])
    assert ctrl._last_mission == "jas39"


def test_an_unknown_configured_mission_falls_back_to_j20(monkeypatch):
    ctrl, launched = _restart_ctrl(monkeypatch, default_mission="mig29")

    ctrl._start_default_mission()

    assert _wait(lambda: launched == ["j20"])


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def test_shipped_jas39_block_matches_the_mission_document(shipped_cfg):
    """docs/missions/jas39.md: 10 s on the spawn heading, 'q' every 3 s."""
    jas39 = shipped_cfg["jas39_mission"]
    assert jas39["turn_guard_s"] == 10.0
    assert jas39["cloak_press_interval_s"] == 3.0


def test_the_controller_reads_the_shipped_jas39_block(shipped_cfg, monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200),
                      config=ControllerConfig.from_config(
                          shipped_cfg, disable_hotkeys=True))
    assert ctrl._jas39_turn_guard_s == 10.0
    assert ctrl._jas39_cloak_interval_s == 3.0


def test_the_schema_accepts_jas39_as_the_default_mission(shipped_cfg):
    cfg = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    cfg["mission"]["default_mission"] = "jas39"
    assert validate_config(cfg) == []


def test_the_schema_rejects_a_misspelt_or_too_short_jas39_key(shipped_cfg):
    bad = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    bad["jas39_mission"]["cloak_press_interval"] = 3.0          # typo
    assert validate_config(bad), "a misspelt jas39_mission key must be caught"
    bad = yaml.safe_load(yaml.safe_dump(shipped_cfg))
    bad["jas39_mission"]["cloak_press_interval_s"] = 0.1
    assert any("cloak_press_interval_s" in e for e in validate_config(bad)), \
        "an interval shorter than 0.5 s holds the key rather than retrying it"

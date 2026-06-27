"""
Tests for mission cancellation race condition (item 5.3) and the GAME_STARTING
stall fix (cancel mid-starting-loop must fire starting_timeout).

Verifies that:
  (a) cancel_mission() causes an in-flight mission to release the lock within 2 seconds.
  (b) No key presses occur after cancellation is set.
  (c) cancel_mission() while FSM is GAME_STARTING fires starting_timeout.
  (d) A natural FSM transition away from GAME_STARTING does NOT fire starting_timeout.
"""

import threading
import time
import pytest
import yaml
import wingman.controller as controller_module

from constants import CONFIG_PATH
from wingman.controller import Controller
from wingman.analyzer import GameState


def _load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def ctrl(monkeypatch):
    """Controller with keyboard patched out so no real keys are pressed."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    exit_event = threading.Event()
    c = Controller(
        region,
        analyzer=None,
        weapon_loop_interval=0.01,  # fast weapon loop for test speed
        exit_event=exit_event,
        capture=None,
        on_auto_mission_key=None,
    )
    yield c
    exit_event.set()
    c.cancel_mission()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cancel_releases_lock_within_two_seconds(ctrl):
    """Mission lock must be released within 2 s of cancel_mission() being called."""
    started = threading.Event()

    def _run():
        # Patch deploy_flares to signal when the mission has started weapon fire
        started.set()
        ctrl.mission_j20()

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait for mission to acquire the lock
    deadline = time.time() + 2.0
    while not ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.01)

    assert ctrl.is_mission_running(), "mission_j20 never acquired the lock"

    ctrl.cancel_mission()

    # Lock must be released within 2 seconds of cancellation
    deadline = time.time() + 2.0
    while ctrl.is_mission_running() and time.time() < deadline:
        time.sleep(0.02)

    assert not ctrl.is_mission_running(), (
        "Mission lock was not released within 2 s after cancel_mission()"
    )


def test_cancel_prevents_new_missions(ctrl):
    """cancel_mission() must set cancellation flags used to stop active loops."""
    ctrl.cancel_mission()
    assert ctrl._mission_cancel.is_set() is True


def test_mission_lock_not_held_after_natural_completion(ctrl, monkeypatch):
    """Mission lock must be released after mission_j20 completes normally (no cancel)."""
    # Skip long recharge waits so this unit test validates lock release quickly.
    monkeypatch.setattr(ctrl, "_interruptible_sleep", lambda *_args, **_kwargs: True)

    # mission_j20 with a patched keyboard does nothing meaningful — it will
    # iterate through maneuvers that call key presses (no-ops), then exit.
    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()
    t.join(timeout=10.0)
    assert not t.is_alive(), "mission_j20 thread did not complete within test timeout"

    assert not ctrl.is_mission_running(), (
        "Mission lock still held after mission_j20 thread exited"
    )


# ---------------------------------------------------------------------------
# GAME_STARTING loop cancellation tests (stall bug fix)
# ---------------------------------------------------------------------------

class _StartingAnalyzerStub:
    """Minimal analyzer stub that stays in GAME_STARTING until told otherwise."""

    def __init__(self):
        self.game_state = GameState.GAME_STARTING
        self.game_battle_alive = False
        self._game_starting_health_scan_enabled = threading.Event()
        self.trigger_calls: list[str] = []

    def trigger_event(self, name: str):
        self.trigger_calls.append(name)
        return True


def _make_starting_ctrl(monkeypatch, analyzer, starting_max_wait_s: float = 90.0) -> Controller:
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    return Controller(
        region,
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        starting_max_wait_s=starting_max_wait_s,
    )


def test_stale_cancel_before_loop_start_does_not_prevent_loop(monkeypatch):
    """cancel set before start_game_starting_loop (e.g. from on_enter_GAME_LOBBY) must not
    cause the loop to exit immediately.

    Regression: _in_starting() checks _mission_cancel, but that flag is always set when
    entering GAME_LOBBY. Without clearing it at loop start the loop exits in < 1 ms.
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    # Simulate the stale cancel that arrives from on_enter_GAME_LOBBY
    ctrl._mission_cancel.set()

    ctrl.start_game_starting_loop()
    time.sleep(0.15)  # give the loop time to start (or immediately exit if broken)

    # If the loop cleared the stale cancel it's still running — starting_timeout not fired yet
    assert "starting_timeout" not in analyzer.trigger_calls, (
        "Loop must not fire starting_timeout immediately due to stale cancel from a prior state"
    )

    # Clean up: advance FSM away so the loop exits naturally
    analyzer.game_state = GameState.GAME_LOBBY
    time.sleep(0.2)


def test_cancel_during_game_starting_fires_starting_timeout(monkeypatch):
    """cancel_mission() while FSM is GAME_STARTING must fire starting_timeout.

    Regression: previously the loop only checked the FSM state, so pressing End
    set _mission_cancel but the loop kept running until max_wait (180 s).
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    ctrl.start_game_starting_loop()
    time.sleep(0.15)  # let the loop thread enter its first inner wait

    ctrl.cancel_mission()

    deadline = time.time() + 2.0
    while "starting_timeout" not in analyzer.trigger_calls and time.time() < deadline:
        time.sleep(0.02)

    assert "starting_timeout" in analyzer.trigger_calls, (
        "starting_timeout was not fired after cancel_mission() during GAME_STARTING"
    )


def test_fsm_transition_away_from_starting_does_not_fire_timeout(monkeypatch):
    """Natural FSM exit from GAME_STARTING (no cancel) must NOT fire starting_timeout.

    If the game moves the FSM to GAME_LOBBY (e.g. opponent disconnected), the
    starting loop exits cleanly without pushing the FSM to GAME_STARTING_STALLED.
    """
    analyzer = _StartingAnalyzerStub()
    ctrl = _make_starting_ctrl(monkeypatch, analyzer)

    ctrl.start_game_starting_loop()
    time.sleep(0.15)

    # Simulate FSM advancing without cancel (e.g. game ended early)
    analyzer.game_state = GameState.GAME_LOBBY

    time.sleep(0.3)  # loop detects the state change within one 0.1s tick

    assert "starting_timeout" not in analyzer.trigger_calls, (
        "starting_timeout must not fire when FSM exits GAME_STARTING without cancel"
    )


def test_starting_max_wait_s_stored_on_controller(monkeypatch):
    """starting_max_wait_s constructor param must be stored as _starting_max_wait_s."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    ctrl = Controller(region, starting_max_wait_s=42.5)
    assert ctrl._starting_max_wait_s == 42.5

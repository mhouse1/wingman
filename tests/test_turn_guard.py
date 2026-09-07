"""ADR 132 — the post-spawn turn guard.

On respawn and at battle entry the boundary detector often reads an edge
immediately, BoundaryTurn banks, and the aircraft orbits instead of flying into
the arena. Spawn points face inward, so for the first seconds of a life the
correct heading is simply the one it spawned on.

Built on the real Controller and the real config, not a stub: the guard is
plumbed config -> ControllerConfig -> Controller, and a fake would prove only
that the test agrees with itself.
"""

import time
import unittest.mock as mock

import pytest
import yaml

import wingman.controller as controller_module
from constants import CONFIG_PATH
from wingman.analyzer import GameState
from wingman.controller import Controller


def _load_config():
    with open(CONFIG_PATH) as fh:
        return yaml.safe_load(fh)


class _AnalyzerStub:
    game_state = GameState.GAME_BATTLE
    def trigger_event(self, *_a, **_k): pass
    def get_telemetry(self): return None


@pytest.fixture
def ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    c = Controller(region, analyzer=_AnalyzerStub())
    c._execute_key_press = mock.MagicMock()
    return c


# --- the window --------------------------------------------------------------

def test_the_guard_is_off_by_default(ctrl):
    assert ctrl.is_turn_guarded() is False


def test_arming_blocks_a_commanded_turn(ctrl):
    ctrl.arm_turn_guard(10.0)
    ctrl.roll_right()
    ctrl.roll_left()
    assert ctrl._execute_key_press.call_count == 0


def test_the_guard_expires_on_its_own(ctrl):
    """A DEADLINE, not a flag. A boolean somebody forgets to clear leaves the
    aircraft unable to turn for the rest of the round — far worse than the
    circling it prevents."""
    ctrl.arm_turn_guard(0.05)
    assert ctrl.is_turn_guarded() is True
    time.sleep(0.08)
    assert ctrl.is_turn_guarded() is False
    ctrl.roll_right()
    assert ctrl._execute_key_press.called


def test_a_zero_window_disables_the_guard(ctrl):
    ctrl.arm_turn_guard(0.0)
    assert ctrl.is_turn_guarded() is False
    ctrl.roll_right()
    assert ctrl._execute_key_press.called


def test_it_can_be_dropped_early(ctrl):
    ctrl.arm_turn_guard(10.0)
    ctrl.clear_turn_guard()
    assert ctrl.is_turn_guarded() is False


def test_the_default_window_comes_from_config(ctrl):
    """Tuning lives in config.yaml, never in the code."""
    expected = _load_config()["mission"]["j20_turn_guard_s"]
    ctrl.arm_turn_guard()
    assert ctrl._turn_guard_until == pytest.approx(
        time.monotonic() + expected, abs=0.5)


# --- the tactic that caused the circling -------------------------------------

def test_the_boundary_turn_is_blocked(ctrl):
    """The reason the guard exists. boundary_turn_mode presses the roll key
    through _climb_key, so gating roll_right() alone would miss it entirely."""
    ctrl.arm_turn_guard(10.0)
    ctrl.boundary_turn_mode()
    assert ctrl.is_boundary_turning() is False


def test_the_boundary_turn_runs_once_the_guard_lapses(ctrl):
    """The guard must not disable the tactic, only delay it."""
    ctrl.arm_turn_guard(0.05)
    ctrl.boundary_turn_mode()
    assert ctrl.is_boundary_turning() is False
    time.sleep(0.08)
    ctrl.boundary_turn_mode()
    assert ctrl.is_boundary_turning() is True
    ctrl._boundary_turn_stop.set()


def test_the_disengage_roll_is_blocked(ctrl):
    """Also presses the roll key directly rather than through roll_right()."""
    ctrl.arm_turn_guard(10.0)
    ctrl.cancel_mission = mock.MagicMock()
    ctrl.disengage_roll_right(duration=1.0)
    assert ctrl.cancel_mission.call_count == 0, \
        "a blocked disengage must not cancel the mission on its way out"


# --- the exemption that must NOT be blocked ----------------------------------

def test_the_missile_evade_is_deliberately_exempt():
    """SAF-001/ADR 070. Evade is a survival response and holds ROLL_RIGHT; ten
    seconds of suppressed evade after a spawn could cost the aircraft the guard
    is protecting. It presses its keys directly rather than through the guarded
    methods, so it bypasses the gate — asserted here so a future refactor that
    routes it through roll_right() fails loudly instead of silently disarming
    the evade for the first ten seconds of every life."""
    import inspect
    src = inspect.getsource(Controller._run_missile_evade_hold)
    assert "_turn_blocked" not in src
    for guarded in ("self.roll_right(", "self.roll_left(", "boundary_turn_mode("):
        assert guarded not in src, f"evade must not route through {guarded}"


# --- armed where it needs to be ----------------------------------------------

def test_mission_j20_arms_the_guard(ctrl, monkeypatch):
    """Battle entry AND every respawn restart go through mission_j20, so one arm
    point covers both cases named in ADR 132 and cannot drift apart from them.

    Run in a thread and cancelled from outside: mission_j20 CLEARS
    `_mission_cancel` after arming, so pre-setting it does not end the runner —
    it loops forever. (Found by this test hanging.)
    """
    import threading
    armed = []
    monkeypatch.setattr(ctrl, "arm_turn_guard",
                        lambda *a, **k: armed.append(True))
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop", lambda: None)
    monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop", lambda: None)
    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()
    for _ in range(100):                # wait for the arm, not a fixed sleep
        if armed:
            break
        time.sleep(0.01)
    ctrl._mission_cancel.set()
    t.join(timeout=3.0)
    assert armed, "mission_j20 must arm the turn guard"

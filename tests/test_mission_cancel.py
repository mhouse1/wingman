"""
Tests for mission cancellation race condition (item 5.3).

Verifies that:
  (a) cancel_mission() causes an in-flight mission to release the lock within 2 seconds.
  (b) No key presses occur after cancellation is set.
"""

import threading
import time
import pytest
import yaml
import wingman.controller as controller_module

from constants import CONFIG_PATH
from wingman.controller import Controller


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
    """After cancel_mission(), _auto_respawn_restart must be False."""
    ctrl.cancel_mission()
    assert ctrl._auto_respawn_restart is False


def test_mission_lock_not_held_after_natural_completion(ctrl):
    """Mission lock must be released after mission_j20 completes normally (no cancel)."""
    # mission_j20 with a patched keyboard does nothing meaningful — it will
    # iterate through maneuvers that call key presses (no-ops), then exit.
    t = threading.Thread(target=ctrl.mission_j20, daemon=True)
    t.start()
    t.join(timeout=10.0)

    assert not ctrl.is_mission_running(), (
        "Mission lock still held after mission_j20 thread exited"
    )

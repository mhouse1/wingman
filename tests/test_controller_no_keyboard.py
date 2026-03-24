"""
Tests for Controller behaviour when the keyboard module is unavailable (item 5.2).

Verifies that Controller initialises without exception when the module-level
keyboard_module is None and that game-control methods degrade gracefully instead
of raising AttributeError.
"""

import threading
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
    """Controller with keyboard_module patched to None."""
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    cfg = _load_config()
    region = (0, 0, cfg["region"]["width"], cfg["region"]["height"])
    exit_event = threading.Event()
    c = Controller(
        region,
        analyzer=None,
        weapon_loop_interval=0.5,
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

def test_init_without_keyboard(ctrl):
    """Controller must initialise without raising when keyboard_module is None."""
    assert ctrl is not None


def test_deploy_flares_without_keyboard(ctrl):
    """deploy_flares() must not raise when keyboard is None."""
    ctrl.deploy_flares(hold_seconds=0.0)


def test_cancel_mission_without_keyboard(ctrl):
    """cancel_mission() must not raise when keyboard is None."""
    ctrl.cancel_mission()


def test_is_mission_running_without_keyboard(ctrl):
    """is_mission_running() must return a bool without raising."""
    result = ctrl.is_mission_running()
    assert isinstance(result, bool)


def test_restart_last_mission_no_history(ctrl):
    """restart_last_mission() returns None when no mission has been started."""
    result = ctrl.restart_last_mission()
    assert result is None


def test_restart_last_mission_returns_false_when_running(ctrl):
    """restart_last_mission() returns False when the mission lock is held."""
    ctrl._mission_lock.acquire(blocking=False)
    try:
        result = ctrl.restart_last_mission()
        assert result is False
    finally:
        if ctrl._mission_lock.locked():
            ctrl._mission_lock.release()

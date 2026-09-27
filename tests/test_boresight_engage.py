"""boresight_engage (ADR 144): the weapon-fire loop WITHOUT the padlock camera.

search_and_destroy presses the fire key and the padlock camera; boresight
engagement is the same fire loop with the camera left alone, so the nose is the
aim point. It is a second function beside search_and_destroy, not a mode flag
inside it, so either can be started and neither can disturb the other. These
tests pin the property the operator asked for (never PADLOCK_CAMERA), the
independence that makes "either mode can be activated" true, and the lifecycle
discipline ADR 118 established for the loops it is modelled on.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller import (
    Controller, FIRE_ACTIVE_WEAPON, PADLOCK_CAMERA,
)
from wingman.controller_config import ControllerConfig


def _make_ctrl(monkeypatch, interval=0.1):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True,
                                weapon_loop_interval=interval),
    )


def _presses(ctrl, key):
    with ctrl._action_intents_lock:
        return [i for i in ctrl._action_intents
                if i["action_type"] == "key_press" and i["key"] == key]


def _wait(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _drain(ctrl):
    ctrl.stop_boresight_engage_loop()
    ctrl.stop_search_and_destroy_loop()


# ---------------------------------------------------------------------------
# The property: fires, never toggles the padlock camera
# ---------------------------------------------------------------------------

def test_fires_the_active_weapon_and_never_presses_padlock(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    try:
        assert _wait(lambda: len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) >= 3), \
            "the loop is not firing"
        time.sleep(0.3)   # give a padlock loop every chance to have pressed
    finally:
        _drain(ctrl)

    assert _presses(ctrl, PADLOCK_CAMERA) == [], \
        "boresight engagement must never toggle the padlock camera"


def test_search_and_destroy_still_presses_padlock(monkeypatch):
    """The contrast that gives the test above its meaning: the other mode is
    unchanged and still presses the padlock camera."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_search_and_destroy_loop()
    try:
        assert _wait(lambda: len(_presses(ctrl, PADLOCK_CAMERA)) >= 1)
        assert _wait(lambda: len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) >= 1)
    finally:
        _drain(ctrl)


def test_fire_cadence_follows_the_weapon_loop_interval(monkeypatch):
    """Same cadence knob as search_and_destroy's weapon loop, not a new one."""
    ctrl = _make_ctrl(monkeypatch, interval=0.1)
    ctrl.start_boresight_engage_loop()
    try:
        time.sleep(1.0)
    finally:
        _drain(ctrl)
    n = len(_presses(ctrl, FIRE_ACTIVE_WEAPON))
    assert 4 <= n <= 12, f"expected roughly one shot per 0.1 s (+ press time), got {n}"


# ---------------------------------------------------------------------------
# Independence: either mode can be activated
# ---------------------------------------------------------------------------

def test_starting_boresight_does_not_start_search_and_destroy(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    try:
        assert ctrl._boresight_thread is not None
        assert ctrl._sdl_padlock_thread is None
        assert ctrl._sdl_weapon_thread is None
        assert ctrl._sdl_stop is None
    finally:
        _drain(ctrl)


def test_starting_search_and_destroy_does_not_start_boresight(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_search_and_destroy_loop()
    try:
        assert ctrl._boresight_thread is None
        assert ctrl._boresight_stop is None
    finally:
        _drain(ctrl)


def test_stopping_one_mode_leaves_the_other_running(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_search_and_destroy_loop()
    ctrl.start_boresight_engage_loop()
    try:
        ctrl.stop_boresight_engage_loop()
        assert ctrl._boresight_thread is None
        assert ctrl._sdl_weapon_thread is not None and ctrl._sdl_weapon_thread.is_alive()
        assert ctrl._sdl_padlock_thread is not None and ctrl._sdl_padlock_thread.is_alive()

        ctrl.start_boresight_engage_loop()
        ctrl.stop_search_and_destroy_loop()
        assert ctrl._sdl_weapon_thread is None
        assert ctrl._boresight_thread is not None and ctrl._boresight_thread.is_alive()
    finally:
        _drain(ctrl)


def test_the_two_modes_have_separate_lifecycle_locks(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    assert ctrl._boresight_lifecycle_lock is not ctrl._sdl_lifecycle_lock
    assert ctrl._boresight_stop is None and ctrl._sdl_stop is None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def test_stop_ends_the_loop_and_the_firing(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    assert _wait(lambda: len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) >= 1)
    thread = ctrl._boresight_thread

    ctrl.stop_boresight_engage_loop()

    assert not thread.is_alive()
    assert ctrl._boresight_thread is None
    fired = len(_presses(ctrl, FIRE_ACTIVE_WEAPON))
    time.sleep(0.4)
    assert len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) == fired, "still firing after stop"


def test_a_mission_cancel_ends_the_loop_without_an_explicit_stop(monkeypatch):
    """Same self-termination search_and_destroy has: cancel_mission() must be
    enough to silence the fire key (respawn, takeover, match end all cancel)."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    thread = ctrl._boresight_thread
    assert _wait(lambda: len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) >= 1)

    ctrl.cancel_mission()

    thread.join(timeout=2.0)
    assert not thread.is_alive()


def test_a_second_start_does_not_spawn_a_second_loop(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    first = ctrl._boresight_thread
    try:
        ctrl.start_boresight_engage_loop()
        assert ctrl._boresight_thread is first
        assert sum(1 for t in threading.enumerate()
                   if t.is_alive() and t is first) == 1
    finally:
        _drain(ctrl)


def test_restart_after_the_loop_ended_on_its_own(monkeypatch):
    """A cancel ends the thread but leaves the stop event unset. A later start
    must notice the thread is dead rather than report 'already running'."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    first = ctrl._boresight_thread
    ctrl.cancel_mission()
    first.join(timeout=2.0)
    ctrl._mission_cancel.clear()

    ctrl.start_boresight_engage_loop()
    try:
        assert ctrl._boresight_thread is not first
        assert ctrl._boresight_thread.is_alive()
    finally:
        _drain(ctrl)


def test_stop_when_not_running_is_a_no_op(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.stop_boresight_engage_loop()          # never started
    ctrl.start_boresight_engage_loop()
    ctrl.stop_boresight_engage_loop()
    ctrl.stop_boresight_engage_loop()          # already stopped


def test_stopping_an_unstarted_thread_does_not_raise():
    """The ADR 118 failure, for this loop: a Thread object assigned but never
    started must not make stop() raise from a mission thread."""
    c = Controller.__new__(Controller)
    c._boresight_lifecycle_lock = threading.Lock()
    c._boresight_lifecycle_timeout_s = 2.0
    c._boresight_stop = threading.Event()
    c._boresight_thread = threading.Thread(target=lambda: None, daemon=True)
    c._boresight_stop.clear()
    c.stop_boresight_engage_loop()
    assert c._boresight_thread is None


def test_a_busy_lifecycle_lock_skips_instead_of_wedging(monkeypatch):
    """Lock-acquire-with-timeout rule: a mission thread must not block forever."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl._boresight_lifecycle_timeout_s = 0.1
    ctrl._boresight_lifecycle_lock.acquire()
    try:
        started = time.time()
        ctrl.start_boresight_engage_loop()
        ctrl.stop_boresight_engage_loop()
        assert time.time() - started < 1.0
        assert ctrl._boresight_thread is None
    finally:
        ctrl._boresight_lifecycle_lock.release()


# ---------------------------------------------------------------------------
# It is a writer on the fire key, so it stops wherever writers stop
# ---------------------------------------------------------------------------

def test_manual_takeover_stops_the_boresight_loop(monkeypatch):
    """SAF-001: after takeover the operator owns the aircraft. A fire loop that
    survives it keeps pressing the fire key on the operator's flight."""
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    thread = ctrl._boresight_thread
    assert _wait(lambda: len(_presses(ctrl, FIRE_ACTIVE_WEAPON)) >= 1)

    ctrl.release_for_manual_takeover()

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    assert ctrl._boresight_thread is None


def test_cleanup_stops_the_boresight_loop(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl.start_boresight_engage_loop()
    thread = ctrl._boresight_thread

    ctrl.cleanup()

    thread.join(timeout=2.0)
    assert not thread.is_alive()
    # cleanup() cancels the mission first, which alone ends the loop's thread;
    # the explicit stop is what also retires the loop's own state.
    assert ctrl._boresight_thread is None
    assert ctrl._boresight_stop.is_set()

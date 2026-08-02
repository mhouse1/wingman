"""Regression tests for disengage_roll_right (no-enemy-for-30s path).

Observed failure (2026-07-28 20:40:03): disengage_roll_right calls
cancel_mission() and then held the roll via _interruptible_sleep, which
aborted on the very cancel flag it had just set — an 8 ms "roll" — and the
follow-up mission restart raced the cancelled mission thread's teardown and
was skipped, leaving the aircraft uncommanded and flying out of the arena.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.controller import Controller, ROLL_RIGHT_KEY


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key, time.time()))

    def release(self, key):
        self.events.append(("release", key, time.time()))


def _make_ctrl(monkeypatch, kb):
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    ctrl = Controller(
        (0, 0, 1920, 1200),
        exit_event=threading.Event(),
        capture=None,
        disable_hotkeys=True,
    )
    monkeypatch.setattr(ctrl, "start_search_and_destroy_loop", lambda: None)
    monkeypatch.setattr(ctrl, "stop_search_and_destroy_loop", lambda: None)
    return ctrl


def test_disengage_roll_survives_its_own_cancel_and_restarts(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb)
    ctrl._set_last_mission("j20")

    restarted = threading.Event()
    monkeypatch.setattr(ctrl, "restart_last_mission", restarted.set)

    ctrl.disengage_roll_right(duration=0.4)

    assert restarted.wait(timeout=5.0), "mission was not restarted after disengage"

    presses = [e for e in kb.events if e[0] == "press" and e[1] == ROLL_RIGHT_KEY]
    releases = [e for e in kb.events if e[0] == "release" and e[1] == ROLL_RIGHT_KEY]
    assert presses and releases
    held_s = releases[0][2] - presses[0][2]
    # The roll must run its full duration despite cancel_mission() having set
    # _mission_cancel — the observed bug held the key for ~8 ms.
    assert held_s >= 0.3, f"roll held only {held_s * 1000:.0f} ms"


def test_disengage_restart_waits_for_mission_teardown(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb)
    ctrl._set_last_mission("j20")

    restarted = threading.Event()
    monkeypatch.setattr(ctrl, "restart_last_mission", restarted.set)

    # Simulate the cancelled mission thread still holding its lock briefly
    # past the roll's end (the observed 3 ms race, exaggerated).
    ctrl._mission_lock.acquire()

    def _release_lock_later():
        time.sleep(0.8)
        ctrl._mission_lock.release()

    threading.Thread(target=_release_lock_later, daemon=True).start()

    ctrl.disengage_roll_right(duration=0.1)

    assert restarted.wait(timeout=5.0), (
        "restart was skipped because the teardown race was not waited out"
    )


def test_disengage_roll_aborts_on_exit_event(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb)
    ctrl._set_last_mission("j20")
    monkeypatch.setattr(ctrl, "restart_last_mission", lambda: None)

    ctrl._exit_event.set()
    t0 = time.time()
    ctrl.disengage_roll_right(duration=5.0)

    deadline = time.time() + 2.0
    while time.time() < deadline:
        if any(e[0] == "release" and e[1] == ROLL_RIGHT_KEY for e in kb.events):
            break
        time.sleep(0.05)
    else:
        raise AssertionError("roll key was not released after exit event")
    assert time.time() - t0 < 2.0  # did not hold for the full 5s
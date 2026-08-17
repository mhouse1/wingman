"""Unit tests for the spawn-attitude guard (ADR 076 d1/d2, SAF-009).

Controller-side: press-and-hold under the programmatic bracket, the alive
handoff with overlap, the state-exit and tactic-preempt releases, the
max-hold backstop, ownership-aware release while a climb hold is active,
and the SAF-001 takeover stop. No real keyboard, no OCR.
"""

import threading
import time

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import Controller, NOSE_UP_KEY


class _FakeKeyboard:
    def __init__(self):
        self.events = []

    def press(self, key):
        self.events.append(("press", key, time.time()))

    def release(self, key):
        self.events.append(("release", key, time.time()))


class _FakeStateAnalyzer:
    def __init__(self, state=GameState.GAME_BATTLE):
        self.game_state = state


SG_CFG = {"enabled": True, "exit_above_alt": 1000,
          "spawn_guard": {"enabled": True, "max_hold_s": 5.0,
                          "release_overlap_s": 0.3}}


def _make_ctrl(monkeypatch, kb, analyzer, climb_cfg=None):
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        capture=None,
        disable_hotkeys=True,
        climb_cfg=climb_cfg or SG_CFG,
    )


def _wait_done(ctrl, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not ctrl.is_spawn_guarding():
            return True
        time.sleep(0.02)
    return False


def _presses(kb):
    return [e for e in kb.events if e[0] == "press" and e[1] == NOSE_UP_KEY]


def _releases(kb):
    return [e for e in kb.events if e[0] == "release" and e[1] == NOSE_UP_KEY]


def test_guard_holds_nose_up_under_bracket(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    ctrl.start_spawn_guard()
    assert ctrl.is_spawn_guarding()
    time.sleep(0.1)
    assert _presses(kb), "nose-up never pressed"
    with ctrl._programmatic_key_lock:
        assert ctrl._programmatic_key_counts.get(NOSE_UP_KEY, 0) > 0, \
            "hold not bracketed — auto-repeats would read as manual takeover"
    ctrl._sg_stop.set()
    assert _wait_done(ctrl)
    assert _releases(kb), "nose-up never released"


def test_alive_handoff_releases_after_overlap(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    t0 = time.time()
    ctrl.start_spawn_guard()
    time.sleep(0.1)
    ctrl.notify_spawn_alive()
    assert _wait_done(ctrl, timeout=3.0), "guard did not release on alive handoff"
    held = time.time() - t0
    assert held < 3.0, f"guard ran {held:.1f}s — ended by cap, not handoff"
    assert held >= 0.3, "guard released before the overlap window"
    assert _releases(kb)


def test_notify_without_guard_is_noop(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())
    ctrl.notify_spawn_alive()   # no guard running — must not blow up
    assert not kb.events


def test_state_exit_releases(monkeypatch):
    kb = _FakeKeyboard()
    analyzer = _FakeStateAnalyzer()
    ctrl = _make_ctrl(monkeypatch, kb, analyzer)

    ctrl.start_spawn_guard()
    time.sleep(0.1)
    analyzer.game_state = GameState.GAME_END_B   # match end mid-hold
    assert _wait_done(ctrl, timeout=2.0), "guard did not release on state exit"
    assert _releases(kb)


def test_max_hold_backstop(monkeypatch):
    kb = _FakeKeyboard()
    cfg = {"enabled": True, "exit_above_alt": 1000,
           "spawn_guard": {"enabled": True, "max_hold_s": 0.5,
                           "release_overlap_s": 0.3}}
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer(), cfg)

    ctrl.start_spawn_guard()
    assert _wait_done(ctrl, timeout=3.0), "guard did not end at the backstop"
    assert _releases(kb)


def test_eject_preempt_releases(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    ctrl.start_spawn_guard()
    time.sleep(0.1)
    ctrl._ejecting.set()
    assert _wait_done(ctrl, timeout=2.0), "guard did not yield to the eject"
    assert _releases(kb)


def test_release_skipped_while_climb_holds_the_key(monkeypatch):
    """ADR 076 d2 ownership rule: with a climb hold active, the guard must
    not issue the OS-level key-up — releasing would yank a climb pitch pulse
    in progress. The bracket is still decremented."""
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    ctrl.start_spawn_guard()
    time.sleep(0.1)
    ctrl._climbing.set()
    try:
        ctrl._sg_stop.set()
        assert _wait_done(ctrl)
        assert not _releases(kb), \
            "guard released the pitch key out from under an active climb hold"
        with ctrl._programmatic_key_lock:
            assert ctrl._programmatic_key_counts.get(NOSE_UP_KEY, 0) == 0, \
                "bracket not decremented on ownership-aware release"
    finally:
        ctrl._climbing.clear()


def test_takeover_stops_guard(monkeypatch):
    """SAF-001: a physical maneuver key during the guard hold triggers
    takeover (even with no mission running) and stops the guard."""
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    ctrl.start_spawn_guard()
    time.sleep(0.1)
    assert not ctrl.is_mission_running()
    took_over = ctrl._handle_maneuver_key_press("l")
    assert took_over, "maneuver key did not trigger takeover during the guard hold"
    assert _wait_done(ctrl, timeout=2.0), "takeover did not stop the guard"
    assert _releases(kb)


def test_disabled_config_is_noop(monkeypatch):
    kb = _FakeKeyboard()
    cfg = {"enabled": True, "exit_above_alt": 1000,
           "spawn_guard": {"enabled": False}}
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer(), cfg)

    ctrl.start_spawn_guard()
    assert not ctrl.is_spawn_guarding()
    assert not kb.events


def test_duplicate_start_suppressed(monkeypatch):
    kb = _FakeKeyboard()
    ctrl = _make_ctrl(monkeypatch, kb, _FakeStateAnalyzer())

    ctrl.start_spawn_guard()
    ctrl.start_spawn_guard()
    time.sleep(0.15)
    assert len(_presses(kb)) == 1, "second start pressed the key again"
    ctrl._sg_stop.set()
    assert _wait_done(ctrl)

"""Afterburner held while a missile is inbound. ADR 128 / FR-008.

Flares change what the missile is tracking; speed changes whether it can still
reach the aircraft. The hold is released on the ALERT going quiet, not on a
fixed burn time, because the alert's persistence is what says the threat is
still live.
"""

import threading
import time
import types
import unittest.mock as mock

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.hold_tactic import HoldTactic
from wingman.keybindings import AFTERBURNER_KEY


def _ctrl(clear_s=0.3, max_s=5.0, state=GameState.GAME_BATTLE):
    c = Controller.__new__(Controller)
    c._analyzer = types.SimpleNamespace(game_state=state)
    c._climb_emergency_active = False
    c._ab_evade_active = threading.Event()
    c._ab_evade_stop = threading.Event()
    c._ab_evade_thread = None
    c._ab_evade = HoldTactic("afterburner evade", running=c._ab_evade_active,
                             stop=c._ab_evade_stop)
    c._ab_evade_until = 0.0
    c._ab_evade_clear_s = clear_s
    c._ab_evade_max_s = max_s
    c._exit_event = threading.Event()
    c._climb_key = mock.MagicMock()
    return c


def _presses(c):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == AFTERBURNER_KEY and k.kwargs.get("press")]


def _releases(c):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == AFTERBURNER_KEY
            and k.kwargs.get("press") is False]


def _settle(c, timeout=3.0):
    t = getattr(c, "_ab_evade_thread", None)
    if t is not None:
        t.join(timeout=timeout)


def test_an_incoming_detection_holds_the_afterburner():
    c = _ctrl()
    c.note_incoming(True)
    assert c.is_afterburner_evading(), "no hold started on an incoming alert"
    assert _presses(c), "afterburner was never pressed"
    _settle(c)
    assert _releases(c), "afterburner was never released"


def test_no_detection_does_nothing():
    """A tick with no alert must not press the throttle."""
    c = _ctrl()
    c.note_incoming(False)
    assert not c.is_afterburner_evading()
    assert not _presses(c)


def test_the_hold_releases_only_after_the_alert_goes_quiet():
    """The requirement is 'until incoming has not appeared for N seconds', not
    a fixed burn."""
    c = _ctrl(clear_s=0.4)
    t0 = time.time()
    c.note_incoming(True)
    _settle(c)
    held = time.time() - t0
    assert held >= 0.35, f"released after {held:.2f}s, before the quiet window"


def test_a_repeat_detection_extends_rather_than_starts_a_second_hold():
    """Two rival holds on one key would fight: whichever finished first would
    release the throttle while the other still wanted it."""
    c = _ctrl(clear_s=0.4)
    c.note_incoming(True)
    first = c._ab_evade_thread
    time.sleep(0.15)
    c.note_incoming(True)             # still inbound
    assert c._ab_evade_thread is first, "a second hold thread was started"
    _settle(c)
    assert len(_releases(c)) == 1, "the key was released more than once"


def test_the_hold_is_bounded_by_an_absolute_cap():
    """AFTERBURNER_KEY is not a watched maneuver key, so a stuck press would
    not surface as a takeover — it would just be a throttle nobody can
    release."""
    c = _ctrl(clear_s=60.0, max_s=0.3)
    t0 = time.time()
    c.note_incoming(True)
    _settle(c)
    held = time.time() - t0
    assert held < 2.0, f"cap did not bound the hold ({held:.1f}s)"
    assert _releases(c), "capped hold did not release the key"


def test_the_key_is_re_pressed_so_a_climb_cannot_cut_the_burn():
    """climb_mode drives the same key and releases on its own schedule. Without
    a re-press the burn would end silently, exactly while a missile is
    inbound."""
    c = _ctrl(clear_s=2.5)
    c.note_incoming(True)
    time.sleep(1.4)
    c._ab_evade_until = 0.0           # let it finish
    _settle(c)
    assert len(_presses(c)) >= 2, "the key was pressed once and never refreshed"


def test_exit_releases_the_key():
    """Shutdown must not leave the throttle held."""
    c = _ctrl(clear_s=30.0)
    c.note_incoming(True)
    time.sleep(0.1)
    c._exit_event.set()
    _settle(c)
    assert _releases(c), "exit left the afterburner pressed"


# --- CR-018-07 / SAF-001: the operator's aircraft is not wingman's to burn ---
#
# Background OCR keeps reporting incoming in GAME_BATTLE_MANUAL (that is what
# keeps flares working there), and the tree calls note_incoming every tick in
# every state. Before CR-018-07 nothing on this path checked for a takeover,
# and release_for_manual_takeover had no way to stop the hold.

def _wait(pred, timeout=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_no_press_while_the_operator_has_the_aircraft():
    c = _ctrl(clear_s=2.0, state=GameState.GAME_BATTLE_MANUAL)
    for _ in range(12):
        c.note_incoming(True)
        time.sleep(0.1)
    assert not c.is_afterburner_evading()
    assert not _presses(c), "the throttle was pressed on the operator's flight"


def test_a_takeover_mid_hold_ends_it_within_one_poll():
    """Even if the stop event never arrives, the hold's own poll must notice."""
    c = _ctrl(clear_s=10.0)
    c.note_incoming(True)
    assert c.is_afterburner_evading()
    c._analyzer.game_state = GameState.GAME_BATTLE_MANUAL
    t0 = time.time()
    assert _wait(lambda: not c.is_afterburner_evading(), timeout=1.0), \
        "the hold kept running after takeover"
    assert time.time() - t0 < 0.5
    assert _releases(c), "takeover left the afterburner pressed"


def test_the_stop_event_ends_the_hold():
    """What release_for_manual_takeover() and cleanup() set."""
    c = _ctrl(clear_s=10.0)
    c.note_incoming(True)
    c._ab_evade_stop.set()
    t0 = time.time()
    _settle(c, timeout=1.0)
    assert not c.is_afterburner_evading()
    assert time.time() - t0 < 0.5
    assert _releases(c)


def test_a_new_alert_after_the_handback_starts_a_hold_again():
    """The stop event must not outlive the takeover that set it."""
    c = _ctrl(clear_s=0.3)
    c._ab_evade_stop.set()            # left set by an earlier takeover
    c.note_incoming(True)
    assert c.is_afterburner_evading()
    assert _presses(c)
    _settle(c)


def _real_ctrl(monkeypatch, analyzer):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer,
        exit_event=threading.Event(),
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            missile_evade={"afterburner_clear_s": 4.0, "afterburner_max_s": 20.0},
        ),
    )


def _evade_press_intents(ctrl, since):
    return [i for i in ctrl.get_action_intents()
            if i["action_type"] == "key_press" and i.get("key") == AFTERBURNER_KEY
            and i.get("action") == "evade" and i["timestamp"] >= since]


def test_release_for_manual_takeover_stops_the_hold_and_it_stays_stopped(monkeypatch):
    """The review's probe, as a test: after the takeover release, an alert that
    persists for 2.5 s produces no afterburner press."""
    analyzer = types.SimpleNamespace(game_state=GameState.GAME_BATTLE)
    ctrl = _real_ctrl(monkeypatch, analyzer)
    ctrl.note_incoming(True)
    assert ctrl.is_afterburner_evading()
    thread = ctrl._ab_evade_thread

    analyzer.game_state = GameState.GAME_BATTLE_MANUAL
    ctrl.release_for_manual_takeover()
    taken = time.time()
    thread.join(timeout=1.0)
    assert not thread.is_alive(), "the hold outlived the takeover"

    deadline = taken + 2.5
    while time.time() < deadline:
        ctrl.note_incoming(True)
        time.sleep(0.1)
    assert _evade_press_intents(ctrl, since=taken) == []
    assert not ctrl.is_afterburner_evading()


def test_cleanup_ends_the_hold(monkeypatch):
    analyzer = types.SimpleNamespace(game_state=GameState.GAME_BATTLE)
    ctrl = _real_ctrl(monkeypatch, analyzer)
    ctrl.note_incoming(True)
    thread = ctrl._ab_evade_thread
    ctrl.cleanup()
    assert not thread.is_alive(), "cleanup() returned with the hold still running"
    assert ctrl._ab_evade_stop.is_set()


# --- CR-018-08 / ADR 137: the emergency airbrake outranks the evade ----------

def test_no_press_during_an_emergency_climb():
    """The review's probe: 2 afterburner presses in 1.5 s with the emergency
    airbrake held and an alert persisting."""
    c = _ctrl(clear_s=2.0)
    c._climb_emergency_active = True
    for _ in range(15):
        c.note_incoming(True)
        time.sleep(0.1)
    assert not c.is_afterburner_evading()
    assert not _presses(c), "the throttle cancelled the emergency airbrake"


def test_an_emergency_mid_hold_ends_it_within_one_poll():
    c = _ctrl(clear_s=10.0)
    c.note_incoming(True)
    assert c.is_afterburner_evading()
    c._climb_emergency_active = True
    assert _wait(lambda: not c.is_afterburner_evading(), timeout=1.0)
    assert _releases(c), "the emergency left the afterburner pressed"


def test_the_evade_resumes_when_the_emergency_clears():
    """The alert may outlast the emergency; the burn should come back."""
    c = _ctrl(clear_s=0.3)
    c._climb_emergency_active = True
    c.note_incoming(True)
    assert not c.is_afterburner_evading()
    c._climb_emergency_active = False
    c.note_incoming(True)
    assert c.is_afterburner_evading()
    _settle(c)

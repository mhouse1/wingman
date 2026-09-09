"""Afterburner held while a missile is inbound. ADR 128 / FR-008.

Flares change what the missile is tracking; speed changes whether it can still
reach the aircraft. The hold is released on the ALERT going quiet, not on a
fixed burn time, because the alert's persistence is what says the threat is
still live.
"""

import threading
import time
import unittest.mock as mock

from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY


def _ctrl(clear_s=0.3, max_s=5.0):
    c = Controller.__new__(Controller)
    c._ab_evade_active = threading.Event()
    c._ab_evade_thread = None
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

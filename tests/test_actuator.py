"""CR-018-09 Phase 1: every key wingman injects goes through the Actuator.

Phase 1 changes no behaviour. What it adds is one place: the structural check
below fails the moment a module presses a key without it, which is how the
five-path state of CR-018-09 came about.
"""

import ast
import logging
import pathlib
import threading
import unittest.mock as mock

import pytest

import wingman.controller as controller
from wingman.actuator import Actuator
from wingman.controller import Controller
from wingman.hold_tactic import HoldTactic
from wingman.keybindings import AFTERBURNER_KEY, ROLL_RIGHT_KEY

_RAW_KEY_CALLS = {"press", "release", "press_and_release", "send", "write"}


def _allow(_what):
    return True


def _deny(_what):
    return False


# --- structure ---------------------------------------------------------------

def test_no_module_but_the_actuator_calls_the_keyboard_backend():
    """AST, like the config drift guard: `keyboard_module.press` or `.release`
    anywhere else — called directly or picked by a conditional — is a press
    path that skips whatever the Actuator applies."""
    offenders = []
    for path in sorted(pathlib.Path("wingman").glob("*.py")):
        if path.name == "actuator.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (isinstance(node, ast.Attribute) and node.attr in _RAW_KEY_CALLS
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "keyboard_module"):
                offenders.append(f"{path}:{node.lineno} keyboard_module.{node.attr}")
    assert offenders == [], offenders


def test_every_press_names_its_owner():
    """The owner is what Phase 2's leases and the takeover log are keyed on. A
    press without one would be the anonymous writer this review is about."""
    tree = ast.parse(pathlib.Path("wingman/controller.py").read_text())
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Name) and f.id == "_press_key":
            if not any(k.arg == "owner" for k in node.keywords):
                missing.append(f"_press_key at line {node.lineno}")
        elif (isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name)
              and f.value.id == "_actuator" and f.attr in ("press", "release", "tap")):
            if len(node.args) < 2 and not any(k.arg == "owner" for k in node.keywords):
                missing.append(f"_actuator.{f.attr} at line {node.lineno}")
    assert missing == [], missing


# --- the Actuator itself -----------------------------------------------------

def test_a_focus_gated_press_the_guard_refuses_presses_nothing():
    kb = mock.MagicMock()
    act = Actuator(lambda: kb, _deny)
    assert act.press("f", "flares", focus_gate=True) is False
    kb.press.assert_not_called()
    assert act.held() == {}


def test_an_ungated_press_ignores_the_guard():
    """_climb_key's presses never had the focus gate. Phase 1 keeps that."""
    kb = mock.MagicMock()
    act = Actuator(lambda: kb, _deny)
    assert act.press(AFTERBURNER_KEY, "cruise", focus_gate=False) is True
    kb.press.assert_called_once_with(AFTERBURNER_KEY)


def test_releases_are_never_gated():
    kb = mock.MagicMock()
    act = Actuator(lambda: kb, _deny)
    act.release("k", "eject")
    kb.release.assert_called_once_with("k")


def test_the_backend_is_looked_up_on_every_call():
    """Tests swap controller.keyboard_module; a reference captured at import
    would let a test press a REAL key into the X server (CR-016-01)."""
    backends = {"kb": mock.MagicMock()}
    act = Actuator(lambda: backends["kb"], _allow)
    first = backends["kb"]
    backends["kb"] = mock.MagicMock()
    act.press("e", "cruise", focus_gate=False)
    first.press.assert_not_called()
    backends["kb"].press.assert_called_once_with("e")


def test_no_backend_means_no_press():
    act = Actuator(lambda: None, _allow)
    assert act.press("e", "cruise", focus_gate=False) is False
    act.release("e", "cruise")
    act.tap("u", "game_starting_loop")


def test_errors_reach_the_caller_unchanged():
    """Each site keeps its own handling in Phase 1, so the Actuator must not
    swallow what the backend raised."""
    kb = mock.MagicMock()
    kb.release.side_effect = OSError("X connection lost")
    act = Actuator(lambda: kb, _allow)
    with pytest.raises(OSError):
        act.release("e", "cruise")


def test_held_tracks_owners_and_a_release_forgets_the_key():
    kb = mock.MagicMock()
    act = Actuator(lambda: kb, _allow)
    act.press(AFTERBURNER_KEY, "cruise", focus_gate=False)
    act.press(AFTERBURNER_KEY, "afterburner_evade", focus_gate=False)
    act.press(ROLL_RIGHT_KEY, "missile_evade", focus_gate=True)
    assert act.held() == {AFTERBURNER_KEY: ("afterburner_evade", "cruise"),
                          ROLL_RIGHT_KEY: ("missile_evade",)}
    act.release(ROLL_RIGHT_KEY, "missile_evade")
    assert ROLL_RIGHT_KEY not in act.held()


def test_dropping_another_owners_key_is_logged(caplog):
    """The re-press race: the X server keeps one state per key, so one writer's
    release ends every writer's hold. Phase 1 does not change that; it makes
    each occurrence countable before Phase 2 does."""
    kb = mock.MagicMock()
    act = Actuator(lambda: kb, _allow)
    act.press(AFTERBURNER_KEY, "afterburner_evade", focus_gate=False)
    with caplog.at_level(logging.DEBUG, logger="wingman.actuator"):
        act.release(AFTERBURNER_KEY, "climb")
    assert "climb released 'e' while afterburner_evade held it" in caplog.text


def test_release_all_keeps_going_past_a_failed_key(caplog):
    kb = mock.MagicMock()
    kb.release.side_effect = [None, OSError("boom"), None]
    act = Actuator(lambda: kb, _allow)
    act.press("e", "cruise", focus_gate=False)
    with caplog.at_level(logging.ERROR, logger="wingman.actuator"):
        act.release_all(("i", "k", "e"), "Controller: takeover", "latched")
    assert [c.args[0] for c in kb.release.call_args_list] == ["i", "k", "e"]
    assert "Controller: takeover release of 'k' failed — latched" in caplog.text
    assert act.held() == {}


# --- wired into the controller -----------------------------------------------

@pytest.fixture
def fresh_actuator(monkeypatch):
    kb = mock.MagicMock()
    monkeypatch.setattr(controller, "keyboard_module", kb)
    monkeypatch.setattr(controller, "_actuator",
                        Actuator(lambda: controller.keyboard_module,
                                 lambda what: controller._may_inject(what)))
    return kb


def _bare_ctrl():
    c = Controller.__new__(Controller)
    c._simulate_os_input = False
    return c


def test_climb_key_presses_through_the_actuator(fresh_actuator):
    c = _bare_ctrl()
    c._climb_key(AFTERBURNER_KEY, press=True, action="cruise")
    fresh_actuator.press.assert_called_once_with(AFTERBURNER_KEY)
    assert controller._actuator.held() == {AFTERBURNER_KEY: ("cruise",)}
    c._climb_key(AFTERBURNER_KEY, press=False, action="cruise")
    fresh_actuator.release.assert_called_once_with(AFTERBURNER_KEY)
    assert controller._actuator.held() == {}


def test_takeover_names_what_wingman_was_holding(fresh_actuator, caplog):
    """SAF-001 diagnostics: the takeover line says which writer held which key
    at the moment the operator took the aircraft."""
    c = Controller.__new__(Controller)
    c._simulate_os_input = False
    for name in ("_eject_stop", "_me_stop", "_climb_stop", "_boundary_turn_stop",
                 "_sg_stop", "_disengage_stop", "_ab_evade_stop"):
        setattr(c, name, threading.Event())
    c._ab_evade = HoldTactic("afterburner evade", stop=c._ab_evade_stop)
    c._eject_stop_reason = None
    c.cancel_mission = lambda: None
    c.release_tracking_holds = lambda why=None: None
    c.stop_search_and_destroy_loop = lambda: None
    c.stop_boresight_engage_loop = lambda: None
    c.stop_cloak_loop = lambda: None
    c._climb_key(AFTERBURNER_KEY, press=True, action="afterburner_evade")
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        c.release_for_manual_takeover()
    assert "(held by wingman: e by afterburner_evade)" in caplog.text
    assert controller._actuator.held() == {}


# --- Phase 2: the throttle is leased --------------------------------------------
#
# Measured before Phase 2 (2026-09-27 09:15 run, first 30 min): `climb` dropped the
# `e` that `cruise` was holding 18 times and `stall_prevention` and `cruise` dropped
# each other's 11 times. Each drop left the burner off until the holder's next tick.

def _leased(kb, may_hold=None):
    act = Actuator(lambda: kb, _allow, leased_keys=(AFTERBURNER_KEY,))
    if may_hold is not None:
        act.set_hold_policy(may_hold)
    return act


def test_one_owners_release_keeps_the_key_down_for_another():
    kb = mock.MagicMock()
    act = _leased(kb)
    act.press(AFTERBURNER_KEY, "cruise", focus_gate=False)
    act.press(AFTERBURNER_KEY, "climb", focus_gate=False)
    act.release(AFTERBURNER_KEY, "climb")
    kb.release.assert_not_called()
    assert act.held() == {AFTERBURNER_KEY: ("cruise",)}
    act.release(AFTERBURNER_KEY, "cruise")
    kb.release.assert_called_once_with(AFTERBURNER_KEY)
    assert act.held() == {}


def test_a_holder_that_must_yield_does_not_keep_the_key_down():
    kb = mock.MagicMock()
    act = _leased(kb, may_hold=lambda _k, owner: owner != "cruise")
    act.press(AFTERBURNER_KEY, "cruise", focus_gate=False)
    act.press(AFTERBURNER_KEY, "climb", focus_gate=False)
    act.release(AFTERBURNER_KEY, "climb")
    kb.release.assert_called_once_with(AFTERBURNER_KEY)


def test_reevaluate_lifts_the_key_when_every_holder_must_yield():
    kb = mock.MagicMock()
    allowed = {"cruise": True}
    act = _leased(kb, may_hold=lambda _k, owner: allowed.get(owner, True))
    act.press(AFTERBURNER_KEY, "cruise", focus_gate=False)
    act.reevaluate(AFTERBURNER_KEY)
    kb.release.assert_not_called()
    allowed["cruise"] = False
    act.reevaluate(AFTERBURNER_KEY)
    kb.release.assert_called_once_with(AFTERBURNER_KEY)
    assert act.held() == {AFTERBURNER_KEY: ("cruise",)}, \
        "the lease stays; cruise re-presses when it may again"


def test_other_keys_keep_phase_one_semantics():
    """One key at a time: the pitch key's release still lifts it for everyone."""
    kb = mock.MagicMock()
    act = _leased(kb)
    act.press("i", "spawn_guard", focus_gate=False)
    act.release("i", "climb")
    kb.release.assert_called_once_with("i")


@pytest.fixture
def leased_ctrl(monkeypatch):
    kb = mock.MagicMock()
    monkeypatch.setattr(controller, "keyboard_module", kb)
    act = Actuator(lambda: controller.keyboard_module,
                   lambda what: controller._may_inject(what),
                   leased_keys=(AFTERBURNER_KEY,))
    monkeypatch.setattr(controller, "_actuator", act)
    c = Controller.__new__(Controller)
    c._simulate_os_input = False
    c._analyzer = None
    c._climb_emergency_active = False
    act.set_hold_policy(c._owner_may_hold)
    return c, kb


def test_a_climb_ending_no_longer_drops_the_cruise_burn(leased_ctrl):
    c, kb = leased_ctrl
    c._climb_key(AFTERBURNER_KEY, press=True, action="cruise")
    c._climb_key(AFTERBURNER_KEY, press=True, action="climb")
    c._climb_key(AFTERBURNER_KEY, press=False, action="climb")
    kb.release.assert_not_called()


def test_an_emergency_lifts_the_cruise_burn_at_once(leased_ctrl):
    """ADR 137: the airbrake and the burner cancel each other. Cruise yields to the
    emergency, so its lease must not hold the key down until its next tick."""
    c, kb = leased_ctrl
    c._climb_key(AFTERBURNER_KEY, press=True, action="cruise")
    c._climb_emergency_active = True
    controller._actuator.reevaluate(AFTERBURNER_KEY)
    kb.release.assert_called_once_with(AFTERBURNER_KEY)


def test_the_climb_reevaluates_the_throttle_where_the_emergency_starts():
    src = pathlib.Path("wingman/controller.py").read_text(encoding="utf-8")
    hold = src[src.index("    def _run_climb_hold"):]
    hold = hold[:hold.index("\n    def ", 10)]
    # Start, mid-hold escalation, and the path-below-level re-brake (2026-10-02):
    # every place the airbrake goes on re-evaluates the throttle.
    assert hold.count("self._climb_emergency_active = True") == 3
    assert hold.count("_actuator.reevaluate(AFTERBURNER_KEY)") == 3


def test_the_policy_table_is_what_may_hold_key_answers():
    """ADR 139 D4's requester gates, unchanged, now read from one table."""
    c = Controller.__new__(Controller)
    c._analyzer = None
    c._climb_emergency_active = True
    for requester, yields in (("cruise", True), ("afterburner_evade", True),
                              ("climb", False), ("missile_evade", False),
                              ("eject", False), ("stall_prevention", False)):
        assert c._may_hold_key(AFTERBURNER_KEY, requester) is (not yields), requester

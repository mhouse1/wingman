"""FR-011 / ADR 164: roll right through the flares on every incoming-missile alert.

Operator, 2026-10-27: hold ROLL_RIGHT_KEY until 2 seconds after the flares are
deployed. The roll is a continuous barrel roll: it does not change the trajectory,
it makes the aircraft harder for a missile to hit. The behaviour was lost twice
(ADR 164, "Why"); FR-011 and these tests are what keep it.

What is pinned here, with a REAL Controller and a real roll thread:
- an alert holds roll-right until flare_roll_after_s past the burst's last flare,
  and a burst during the hold extends it with one press;
- it does not start, and it ends, on takeover, outside a battle, during an eject
  and during a hard-emergency climb; takeover, exit and the cap end it too;
- the roll keys are leased, so the roll and the missile evade share roll-right;
- the chase yields pitch and roll while it is held, and steers again after;
- end to end from the flare handler, whose real burst reports its last flare;
- FR-011 exists, is traced to Controller.flare_roll, and the shipped wait is 2 s.
"""

import ast
import logging
import pathlib
import threading
import time

import pytest
import yaml

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.config_schema import schema_default
from wingman.controller import (
    Controller, NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
)
from wingman.controller_config import ControllerConfig
from wingman.tick_handlers import AmmoEventsHandler

from tests.test_missile_evade import _FakeIncomingAnalyzer, _FakeKeyboard, _releases
from tests.test_pursuit_recovery import _CLIMB, _Analyzer, _Capture, _Tracker, _wait

_AFTER_S = 0.3
_STEER_KEYS = {NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY}


def _ctrl(monkeypatch, state=GameState.GAME_BATTLE, max_s=5.0, analyzer=None):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer or _Analyzer(state=state),
        exit_event=threading.Event(),
        capture=_Capture(),
        config=ControllerConfig(
            simulate_os_input=True,
            disable_hotkeys=True,
            climb=_CLIMB,
            tracking={"sustained_hold_enabled": True},
            pursuit_mode={"enabled": True, "pursuit_max_duration_s": 0.0,
                          "pursuit_padlock_verify": False, "ammo_zero_grace_s": 0.0,
                          "search_resume_delay_s": 0.0, "search_resume_centre_delay_s": 0.0,
                          "empty_confirm_reads": 3, "recovery_max_s": 30.0},
            missile_evade={"flare_roll_after_s": _AFTER_S, "flare_roll_max_s": max_s},
        ),
    )


def _roll_intents(ctrl):
    """(action_type, key, timestamp) of the flare roll's own presses and releases."""
    return [(i["action_type"], i["key"], i["timestamp"]) for i in ctrl.get_action_intents()
            if i.get("action") == "flare_roll"]


def _stop(ctrl):
    ctrl._flare_roll.stop()
    assert _wait(lambda: not ctrl.is_flare_rolling())


# --- the hold ---------------------------------------------------------------------

def test_an_alert_holds_roll_right_until_after_s_past_the_last_flare(monkeypatch):
    ctrl = _ctrl(monkeypatch)
    flares_done = threading.Event()
    try:
        assert ctrl.flare_roll(flares_done) is True
        assert ctrl.is_flare_rolling()
        time.sleep(_AFTER_S + 0.2)
        assert ctrl.is_flare_rolling(), "released before the last flare was out"
        out_at = time.time()
        flares_done.set()
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=2.0)
        intents = _roll_intents(ctrl)
        assert [(a, k) for a, k, _ in intents] == [("key_press", ROLL_RIGHT_KEY),
                                                  ("key_release", ROLL_RIGHT_KEY)]
        assert _AFTER_S - 0.02 <= intents[-1][2] - out_at <= _AFTER_S + 0.25
    finally:
        _stop(ctrl)


def test_a_burst_during_the_roll_extends_it_with_one_press(monkeypatch):
    """Through a volley the bursts come about 1.5 s apart: the roll lasts until
    flare_roll_after_s past the last one, not the first."""
    ctrl = _ctrl(monkeypatch)
    first, second = threading.Event(), threading.Event()
    try:
        ctrl.flare_roll(first)
        first.set()
        time.sleep(_AFTER_S / 2)
        assert ctrl.flare_roll(second) is True
        time.sleep(_AFTER_S)                        # past the first burst's release time
        assert ctrl.is_flare_rolling(), "the second burst did not extend the roll"
        out_at = time.time()
        second.set()
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=2.0)
        intents = _roll_intents(ctrl)
        assert [a for a, _, _ in intents] == ["key_press", "key_release"]
        assert _AFTER_S - 0.02 <= intents[-1][2] - out_at <= _AFTER_S + 0.25
    finally:
        _stop(ctrl)


# --- when it does not hold --------------------------------------------------------

def _takeover(_ctrl, analyzer):
    analyzer.game_state = GameState.GAME_BATTLE_MANUAL


def _left_the_battle(_ctrl, analyzer):
    analyzer.game_state = GameState.GAME_LOBBY


def _eject(ctrl, _analyzer):
    ctrl._ejecting.set()


def _emergency_climb(ctrl, _analyzer):
    ctrl._climbing.set()
    ctrl._climb_emergency_requested = True


_REFUSALS = [(_takeover, "manual takeover"), (_left_the_battle, "not in battle"),
             (_eject, "eject in progress"), (_emergency_climb, "emergency climb")]


@pytest.mark.parametrize("condition, reason", _REFUSALS)
def test_it_does_not_start(monkeypatch, caplog, condition, reason):
    ctrl = _ctrl(monkeypatch)
    condition(ctrl, ctrl._analyzer)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        assert ctrl.flare_roll(threading.Event()) is False
    assert not ctrl.is_flare_rolling()
    assert _roll_intents(ctrl) == []
    assert f"flare roll not started — {reason}" in caplog.text


@pytest.mark.parametrize("condition, reason", _REFUSALS)
def test_it_ends(monkeypatch, caplog, condition, reason):
    ctrl = _ctrl(monkeypatch)
    try:
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.flare_roll(threading.Event())
            time.sleep(0.1)
            condition(ctrl, ctrl._analyzer)
            assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=1.0)
        assert [a for a, _, _ in _roll_intents(ctrl)] == ["key_press", "key_release"]
        assert f"flare roll released ({reason}" in caplog.text
    finally:
        _stop(ctrl)


def test_takeover_stops_it_through_the_writer_registry(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.flare_roll(threading.Event())
        ctrl.release_for_manual_takeover()
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=1.0)
    assert "flare roll released (stopped" in caplog.text


def test_program_exit_ends_it(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.flare_roll(threading.Event())
        ctrl._exit_event.set()
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=1.0)
    assert "flare roll released (exit" in caplog.text


def test_the_cap_ends_a_burst_that_never_reports(monkeypatch, caplog):
    ctrl = _ctrl(monkeypatch, max_s=0.3)
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl.flare_roll(threading.Event())                # never set
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=2.0)
    assert "flare roll hit its" in caplog.text
    assert "flare roll released (cap" in caplog.text


# --- the lease with the missile evade ---------------------------------------------

class _BattleIncoming(_FakeIncomingAnalyzer):
    game_state = GameState.GAME_BATTLE


def test_the_roll_and_the_evade_share_roll_right(monkeypatch):
    """Both hold ROLL_RIGHT, under their own owners: the flare roll letting go
    first must not lift the evade's roll, and the evade's own release lifts it."""
    kb = _FakeKeyboard()
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    ctrl = Controller((0, 0, 1920, 1200), analyzer=_BattleIncoming(detected=True),
                      exit_event=threading.Event(), capture=None,
                      config=ControllerConfig(disable_hotkeys=True, missile_evade={
                          "clear_seconds": 5.0, "max_manoeuvre_s": 5.0, "max_hold_s": 6.0,
                          "flare_roll_after_s": 0.1}))
    flares_done = threading.Event()
    try:
        ctrl.missile_evade_mode()
        assert ctrl.flare_roll(flares_done) is True
        assert _wait(lambda: sum(1 for e in kb.events if e[:2] == ("press", ROLL_RIGHT_KEY)) >= 2)
        flares_done.set()
        assert _wait(lambda: not ctrl.is_flare_rolling())
        assert _releases(kb, ROLL_RIGHT_KEY) == [], "the flare roll lifted the evade's roll"
        ctrl._me_stop.set()
        assert _wait(lambda: not ctrl.is_missile_evading())
        assert len(_releases(kb, ROLL_RIGHT_KEY)) == 1, "the evade's own release lifts it"
    finally:
        ctrl._flare_roll.stop()
        ctrl._me_stop.set()
        _wait(lambda: not ctrl.is_flare_rolling() and not ctrl.is_missile_evading())
        controller_module._actuator.release_all((ROLL_RIGHT_KEY,), "test cleanup", "")


# --- the chase yields -------------------------------------------------------------

def _steering_presses(ctrl, since=0):
    """The chase's own steering presses, not the flare roll's."""
    return [(i["key"], i.get("action")) for i in ctrl.get_action_intents()[since:]
            if i["action_type"] == "key_press" and i["key"] in _STEER_KEYS
            and i.get("action") != "flare_roll"]


def test_the_chase_yields_pitch_and_roll_while_it_rolls_and_steers_after(monkeypatch, caplog):
    """A barrel roll leaves the flight path alone only if nothing pitches during it."""
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl = _ctrl(monkeypatch, state=GameState.GAME_BATTLE_EJECT)
        ctrl.set_target_tracker(_Tracker())
        ctrl.pursue_and_engage(defer_switch_until_empty=True)
        assert _wait(ctrl.is_pursuing)
        flares_done = threading.Event()
        try:
            time.sleep(0.5)                                   # steering is running
            assert _steering_presses(ctrl), "the chase never steered"
            ctrl.flare_roll(flares_done)
            time.sleep(0.4)                                   # let the loop notice and release
            mark = len(ctrl.get_action_intents())
            time.sleep(0.6)                                   # several loop cycles, still rolling
            assert ctrl.is_flare_rolling()
            assert _steering_presses(ctrl, mark) == []
            assert ctrl._roll_held is None and ctrl._pitch_held is None
            flares_done.set()
            assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=2.0)
            time.sleep(0.5)
            keys = {k for k, _ in _steering_presses(ctrl, mark)}
            assert ROLL_RIGHT_KEY in keys and NOSE_UP_KEY in keys
        finally:
            ctrl._flare_roll.stop()
            ctrl.stop_eject_sequence("respawn_detected")
            _wait(lambda: not ctrl.is_pursuing() and not ctrl.is_flare_rolling())
    assert "yielding pitch and roll to the flare roll (ADR 164)" in caplog.text
    assert "flare roll over, steering resumes" in caplog.text


# --- end to end from the flare handler ---------------------------------------------

class _AlertAnalyzer(_Analyzer):
    def __init__(self):
        super().__init__(state=GameState.GAME_BATTLE)
        self.incoming_ts = time.time()

    def get_incoming_cache_result(self):
        return (True, 0.9, "template")

    def get_incoming_cache_timestamp(self):
        return self.incoming_ts


def test_an_alert_rolls_from_the_first_flare_to_after_s_past_the_last(monkeypatch):
    analyzer = _AlertAnalyzer()
    ctrl = _ctrl(monkeypatch, analyzer=analyzer)
    handler = AmmoEventsHandler(analyzer, ctrl, {})
    try:
        assert handler.deploy_flares_on_new_incoming() is True
        assert ctrl.is_flare_rolling()
        assert _wait(lambda: not ctrl.is_flare_rolling(), timeout=4.0)
        intents = ctrl.get_action_intents()
        flares = [i["timestamp"] for i in intents
                  if i.get("action") == "deploy_flares" and i["action_type"] == "key_press"]
        roll = _roll_intents(ctrl)
        assert len(flares) == 3
        assert [a for a, _, _ in roll] == ["key_press", "key_release"]
        assert roll[0][2] <= flares[0] + 0.1, "the roll did not start with the flares"
        assert _AFTER_S <= roll[1][2] - flares[-1] <= _AFTER_S + 0.35
    finally:
        _stop(ctrl)


# --- what keeps it from being lost a third time ------------------------------------

def test_fr011_is_stated_and_traced_to_the_code():
    sdoc = pathlib.Path("docs/requirements/002-functional.sdoc").read_text(encoding="utf-8")
    assert "UID: FR-011" in sdoc
    tree = ast.parse(pathlib.Path("wingman/controller.py").read_text(encoding="utf-8"))
    docstrings = {n.name: ast.get_docstring(n) or "" for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)}
    assert "@relation(FR-011, scope=function)" in docstrings.get("flare_roll", "")


def test_the_shipped_wait_is_the_operators_two_seconds():
    cfg = yaml.safe_load(pathlib.Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert cfg["behavior_tree"]["missile_evade"]["flare_roll_after_s"] == 2.0
    assert schema_default("behavior_tree.missile_evade.flare_roll_after_s") == 2.0

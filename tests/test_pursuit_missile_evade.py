"""ADR 163: the missile evade flies through a pursuit.

Measured (Research 019, 193 missions): 556 of 559 shoot-downs came inside a pursuit,
where the behavior tree cannot select the evade (Idle holds GAME_BATTLE_EJECT), so
the evade ran in 4 of 434 alerted missile kills while flares and the afterburner
fired on nearly all of them.

What is pinned here, with a REAL Controller, a real pursuit loop and a real evade
thread:
- an alert inside a pursuit starts the evade for the evade arm, and not for the
  control arm, outside a pursuit (the tree's, ADR 070), at share 0, or while the
  dive recovery flies (ADR 148);
- an engagement draws its arm once, and an evade that ends while its engagement
  goes on starts again on the next alert, as the tree re-selects it;
- a round draws its arm once, logged once, and the next round draws again (ADR 165);
- the chase writes neither axis while the evade flies, says so, and steers again
  after it;
- the roll keys are leased, so the chase releasing its own roll-right does not
  lift the evade's;
- a hard-emergency climb ends a flying evade and climbs, and no evade starts while
  it flies (D5); a climb that is not an emergency, or a recovery the pursuit holds
  off, leaves the evade flying.
"""

import logging
import threading
import time

import wingman.controller as controller_module
from wingman.analyzer import GameState
from wingman.controller import (
    Controller, NOSE_DOWN_KEY, NOSE_UP_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
)
from wingman.controller_config import ControllerConfig
from wingman.evade_trial import EvadeTrial

from tests.test_missile_evade import _FakeIncomingAnalyzer, _FakeKeyboard, _releases
from tests.test_pursuit_recovery import _CLIMB, _Analyzer, _Capture, _Tracker, _wait

_STEER_KEYS = {NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY}


class _Draws:
    def __init__(self, *values):
        self._values = list(values)

    def random(self):
        return self._values.pop(0)


def _ctrl(monkeypatch, share=1.0, max_manoeuvre_s=1.2, analyzer=None, **pursuit_extra):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    return Controller(
        (0, 0, 1920, 1200),
        analyzer=analyzer or _Analyzer(),
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
                          "empty_confirm_reads": 3, "recovery_max_s": 30.0,
                          **pursuit_extra},
            missile_evade={"pursuit_share": share, "clear_seconds": 0.3,
                           "min_clear_samples": 2, "max_manoeuvre_s": max_manoeuvre_s,
                           "max_hold_s": 5.0},
        ),
    )


def _start_chase(monkeypatch, **kw):
    ctrl = _ctrl(monkeypatch, **kw)
    ctrl.set_target_tracker(_Tracker())
    ctrl.pursue_and_engage(defer_switch_until_empty=True)
    assert _wait(ctrl.is_pursuing)
    return ctrl


def _end_chase(ctrl):
    ctrl._me_stop.set()
    ctrl._climb_stop.set()
    ctrl._pursuit_recovery.clear()
    ctrl.stop_eject_sequence("respawn_detected")
    _wait(lambda: not ctrl.is_pursuing() and not ctrl.is_missile_evading()
          and not ctrl.is_climbing())


def _steering_presses(ctrl, since=0):
    """The chase's own steering presses, not the evade's."""
    with ctrl._action_intents_lock:
        intents = list(ctrl._action_intents)[since:]
    return [(i["key"], i.get("action")) for i in intents
            if i["action_type"] == "key_press" and i["key"] in _STEER_KEYS
            and i.get("action") != "missile_evade"]


def _mark(ctrl):
    with ctrl._action_intents_lock:
        return len(ctrl._action_intents)


# --- when it starts ---------------------------------------------------------------

def test_an_alert_in_a_pursuit_starts_the_evade_for_the_evade_arm(monkeypatch):
    ctrl = _start_chase(monkeypatch, share=1.0)
    try:
        assert ctrl.pursuit_evade_on_alert(time.time()) == ("evade", True)
        assert ctrl.is_missile_evading()
    finally:
        _end_chase(ctrl)


def test_the_control_arm_starts_nothing(monkeypatch):
    ctrl = _start_chase(monkeypatch, share=0.5)
    ctrl._evade_trial = EvadeTrial(0.5, rng=_Draws(0.9))
    try:
        assert ctrl.pursuit_evade_on_alert(time.time()) == ("control", False)
        assert not ctrl.is_missile_evading()
    finally:
        _end_chase(ctrl)


def test_outside_a_pursuit_the_tree_owns_the_evade(monkeypatch):
    ctrl = _ctrl(monkeypatch, share=1.0)
    assert ctrl.pursuit_evade_on_alert(time.time()) == (None, False)
    assert not ctrl.is_missile_evading()


def test_share_zero_is_the_old_behaviour(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl = _start_chase(monkeypatch, share=0.0)
        try:
            assert ctrl.pursuit_evade_on_alert(time.time()) == (None, False)
            assert not ctrl.is_missile_evading()
        finally:
            _end_chase(ctrl)
    assert "MISSILE EVADE[pursuit]" not in caplog.text


def test_no_evade_starts_while_the_dive_recovery_flies(monkeypatch, caplog):
    ctrl = _start_chase(monkeypatch, share=1.0)
    try:
        ctrl._pursuit_recovery.set()
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            assert ctrl.pursuit_evade_on_alert(time.time()) == ("evade", False)
        assert not ctrl.is_missile_evading()
        assert "not started — the dive recovery has the airframe" in caplog.text
    finally:
        _end_chase(ctrl)


def test_one_engagement_one_draw_and_the_evade_starts_again_while_it_lasts(monkeypatch):
    ctrl = _start_chase(monkeypatch, share=1.0, max_manoeuvre_s=0.4)
    try:
        t0 = time.time()
        assert ctrl.pursuit_evade_on_alert(t0) == ("evade", True)
        assert ctrl.pursuit_evade_on_alert(t0 + 0.3) == (None, False)    # running: extends
        assert _wait(lambda: not ctrl.is_missile_evading(), timeout=3.0)
        # The same engagement's next alert (within the 3 s volley): no new draw, a new start.
        assert ctrl.pursuit_evade_on_alert(t0 + 1.5) == (None, True)
        assert ctrl._evade_trial.engagements == 1
    finally:
        _end_chase(ctrl)


def test_a_round_draws_once_and_the_next_round_draws_again(monkeypatch, caplog):
    """ADR 165: the round's later engagements take its arm without a draw, and
    the round start the flare handler passes on opens the next draw."""
    ctrl = _start_chase(monkeypatch, share=0.5)
    ctrl._evade_trial = EvadeTrial(0.5, rng=_Draws(0.9, 0.95))   # two draws, both control
    try:
        t0 = time.time()
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.pursuit_evade_new_round()
            assert ctrl.pursuit_evade_on_alert(t0) == ("control", False)
            assert ctrl.pursuit_evade_on_alert(t0 + 60.0) == ("control", False)
            ctrl.pursuit_evade_new_round()
            assert ctrl.pursuit_evade_on_alert(t0 + 400.0) == ("control", False)
        assert caplog.text.count(
            "MISSILE EVADE[pursuit]: round 1 — control arm (ADR 165, share 0.50)") == 1
        assert caplog.text.count(
            "MISSILE EVADE[pursuit]: round 2 — control arm (ADR 165, share 0.50)") == 1
        assert caplog.text.count("MISSILE EVADE[pursuit]: engagement ") == 3
        assert not ctrl.is_missile_evading()
    finally:
        _end_chase(ctrl)


# --- the chase yields -------------------------------------------------------------

def test_the_chase_writes_neither_axis_while_the_evade_flies(monkeypatch):
    ctrl = _start_chase(monkeypatch, share=1.0, max_manoeuvre_s=2.0)
    try:
        time.sleep(0.5)                                   # steering is running
        assert _steering_presses(ctrl), "the chase never steered"
        ctrl.pursuit_evade_on_alert(time.time())
        time.sleep(0.4)                                   # let the loop notice and release
        mark = _mark(ctrl)
        time.sleep(0.9)                                   # several loop cycles, evade still on
        assert ctrl.is_missile_evading()
        assert _steering_presses(ctrl, mark) == []
        assert ctrl._roll_held is None and ctrl._pitch_held is None
    finally:
        _end_chase(ctrl)


def test_the_chase_steers_again_when_the_evade_ends_and_says_so(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl = _start_chase(monkeypatch, share=1.0, max_manoeuvre_s=0.6)
        try:
            ctrl.pursuit_evade_on_alert(time.time())
            time.sleep(0.3)               # yielded: from here the chase presses nothing...
            mark = _mark(ctrl)
            assert _wait(lambda: not ctrl.is_missile_evading(), timeout=3.0)
            time.sleep(0.5)               # ...until the evade ends: its holds are taken up at once
            keys = {k for k, _ in _steering_presses(ctrl, mark)}
            assert ROLL_RIGHT_KEY in keys and NOSE_UP_KEY in keys
        finally:
            _end_chase(ctrl)
    assert "yielding pitch and roll to the missile evade (ADR 163)" in caplog.text
    assert "missile evade over, steering resumes" in caplog.text


# --- the roll lease -----------------------------------------------------------------

def test_the_roll_keys_are_leased():
    leased = controller_module._actuator._leased
    assert {ROLL_LEFT_KEY, ROLL_RIGHT_KEY} <= leased


def test_the_chases_release_of_its_own_roll_right_does_not_lift_the_evades(monkeypatch):
    """The yield's release_roll_hold releases ROLL_RIGHT as `tracking_roll`; the
    evade holds the same key as `missile_evade` and must keep it down."""
    kb = _FakeKeyboard()
    monkeypatch.setattr(controller_module, "keyboard_module", kb)
    ctrl = Controller((0, 0, 1920, 1200), analyzer=_FakeIncomingAnalyzer(detected=True),
                      exit_event=threading.Event(), capture=None,
                      config=ControllerConfig(disable_hotkeys=True, missile_evade={
                          "clear_seconds": 5.0, "max_manoeuvre_s": 5.0, "max_hold_s": 6.0}))
    try:
        ctrl._press_tracking_key(ROLL_RIGHT_KEY, "tracking_roll")
        ctrl._roll_held, ctrl._roll_hold_reason = "right", "target"
        ctrl.missile_evade_mode()
        assert _wait(lambda: ("press", ROLL_RIGHT_KEY) in {(e[0], e[1]) for e in kb.events
                                                            if e[0] == "press"} and
                     sum(1 for e in kb.events if e[:2] == ("press", ROLL_RIGHT_KEY)) >= 2)
        ctrl.release_roll_hold(why="yield to missile evade")
        assert _releases(kb, ROLL_RIGHT_KEY) == [], "the chase lifted the evade's roll-right"
        ctrl._me_stop.set()
        assert _wait(lambda: not ctrl.is_missile_evading())
        assert len(_releases(kb, ROLL_RIGHT_KEY)) == 1, "the evade's own release lifts it"
    finally:
        ctrl._me_stop.set()
        _wait(lambda: not ctrl.is_missile_evading())
        controller_module._actuator.release_all((ROLL_RIGHT_KEY,), "test cleanup", "")


# --- a dive emergency ends it (D5) --------------------------------------------------

def _in_battle(monkeypatch):
    """The tree's evade (ADR 070) in GAME_BATTLE, long enough that only a stop ends it."""
    return _ctrl(monkeypatch, max_manoeuvre_s=5.0,
                 analyzer=_Analyzer(state=GameState.GAME_BATTLE))


def _stop_holds(ctrl):
    ctrl._me_stop.set()
    ctrl._climb_stop.set()
    _wait(lambda: not ctrl.is_missile_evading() and not ctrl.is_climbing())


def test_a_hard_emergency_ends_the_evade_and_climbs(monkeypatch, caplog):
    """The trial of 2026-10-26: 3 of 78 evades began in a descent, the evade steepened
    it, and the climb was refused 3 or 4 times until the evade ended; each crashed."""
    ctrl = _in_battle(monkeypatch)
    try:
        ctrl.missile_evade_mode()
        assert ctrl.is_missile_evading()
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
            assert not ctrl.is_missile_evading(), "the climb started under a flying evade"
            assert ctrl.is_climbing()
            time.sleep(0.6)           # two polls: the climb's own evade check must not end it
            assert ctrl.is_climbing()
        intents = ctrl.get_action_intents()
        last_evade_release = max(n for n, i in enumerate(intents)
                                 if i.get("action") == "missile_evade"
                                 and i["action_type"] == "key_release")
        first_climb_press = min(n for n, i in enumerate(intents)
                                if i.get("action", "").startswith("climb")
                                and i["action_type"] == "key_press")
        assert last_evade_release < first_climb_press, "the climb pressed before the evade let go"
    finally:
        _stop_holds(ctrl)
    assert "hard emergency ends the missile evade (ADR 163)" in caplog.text
    assert "missile_evade complete (dive_emergency" in caplog.text
    assert "climb suppressed" not in caplog.text


def test_a_climb_that_is_not_an_emergency_still_waits_for_the_evade(monkeypatch, caplog):
    """The altitude floor is preventive (ADR 141 D2): its climb does not end the evade."""
    ctrl = _in_battle(monkeypatch)
    try:
        ctrl.missile_evade_mode()
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=False)
        assert ctrl.is_missile_evading() and not ctrl.is_climbing()
        assert "climb suppressed — missile evade in progress" in caplog.text
    finally:
        _stop_holds(ctrl)


def test_no_evade_starts_while_the_emergency_climb_flies(monkeypatch, caplog):
    """Else the next alert takes the airframe straight back, and the climb's own evade
    check ends the climb. Once the tree no longer calls it an emergency, it may."""
    ctrl = _in_battle(monkeypatch)
    try:
        ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.missile_evade_mode()
        assert not ctrl.is_missile_evading() and ctrl.is_climbing()
        assert "missile_evade suppressed — emergency climb in progress" in caplog.text
        ctrl.set_climb_emergency(False)
        ctrl.missile_evade_mode()
        assert ctrl.is_missile_evading()
        assert _wait(lambda: not ctrl.is_climbing(), timeout=2.0), "the climb did not yield"
    finally:
        _stop_holds(ctrl)


def test_in_a_pursuit_the_recovery_takes_the_airframe_from_the_evade(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        ctrl = _start_chase(monkeypatch, share=1.0, max_manoeuvre_s=5.0,
                            dive_safety=False, crash_recovery_clear_view_s=0.0)
        try:
            t0 = time.time()
            assert ctrl.pursuit_evade_on_alert(t0) == ("evade", True)
            ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
            assert not ctrl.is_missile_evading()
            assert _wait(ctrl.pursuit_recovery_active), "the recovery never took the airframe"
            assert _wait(lambda: "yielding pitch and roll to the dive recovery (ADR 148)"
                         in caplog.text), "the chase did not yield to the recovery"
            # The engagement's next alert starts nothing while the recovery flies (D2).
            assert ctrl.pursuit_evade_on_alert(t0 + 1.0) == (None, False)
            assert not ctrl.is_missile_evading()
        finally:
            _end_chase(ctrl)


def test_in_a_pursuit_a_held_off_recovery_leaves_the_evade_flying(monkeypatch, caplog):
    """With the target in view the pursuit holds the recovery off (operator,
    2026-10-08): the evade is not ended for a climb that does not start."""
    ctrl = _start_chase(monkeypatch, share=1.0, max_manoeuvre_s=5.0, dive_safety=False)
    try:
        assert _wait(lambda: ctrl._pursuit_objective_in_view() == "target")
        assert ctrl.pursuit_evade_on_alert(time.time()) == ("evade", True)
        with caplog.at_level(logging.INFO, logger="wingman.controller"):
            ctrl.climb_mode(target_alt=5000, max_s=5.0, emergency=True)
        assert ctrl.is_missile_evading() and not ctrl.is_climbing()
        assert "crash recovery held off — the pursuit has the target in view" in caplog.text
    finally:
        _end_chase(ctrl)

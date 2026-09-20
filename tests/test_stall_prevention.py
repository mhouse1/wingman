"""Stall prevention: release AIRBRAKE, hold AFTERBURNER below a speed floor.

Phase 1 (operator directive, verbatim): "if speed below 300KPH it should
deactivate break and activate afterburner, during evade manuvers and
mission_j20 if altitude below 3000 it should automatically fly up" (the
altitude-floor half lives in behavior_tree.py's ClimbCondition; this file
covers the speed half only).

Deliberately mirrors tests/test_afterburner_cruise.py's exact shape (same
tree-independent, every-tick note_X() pattern, ADR 134 D9's precedent) —
this is the same kind of watchdog, just keyed on speed instead of fuel, and
overriding rather than deferring for the same reason: a near-stall is a
physical fact about the aircraft's energy state, not a tactical decision,
so it must win regardless of what else currently holds the airframe —
including Climb's own emergency airbrake hold, which is deliberately NOT
one of the exceptions here (unlike cruise-afterburner's climb-emergency
exception): the whole point is catching the case where an emergency climb's
own airbrake-and-suppressed-afterburner posture bleeds the aircraft's speed
down to a near-stall (live-observed the same night this shipped).
"""

import unittest.mock as mock

from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY, AIRBRAKE_KEY


def _ctrl(min_speed_kph=300.0, confirm_reads=2, speed=None,
          manual_takeover=False):
    c = Controller.__new__(Controller)
    c._stall_prevention_enabled = True
    c._stall_min_speed_kph = min_speed_kph
    c._stall_confirm_reads = confirm_reads
    c._stall_active = False
    c._stall_low_streak = 0
    c._climb_key = mock.MagicMock()
    c._manual_takeover_active = mock.MagicMock(return_value=manual_takeover)
    c._read_speed_kph = mock.MagicMock(return_value=speed)
    return c


def _presses(c, key):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == key and k.kwargs.get("press")]


def _releases(c, key):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == key and k.kwargs.get("press") is False]


def test_speed_above_the_floor_does_nothing():
    c = _ctrl(speed=450)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _presses(c, AFTERBURNER_KEY)
    assert not _releases(c, AIRBRAKE_KEY)


def test_a_single_low_reading_does_not_engage():
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active(), "engaged on a single low reading"


def test_confirm_reads_consecutive_low_readings_engage():
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    assert _releases(c, AIRBRAKE_KEY)
    assert _presses(c, AFTERBURNER_KEY)


def test_the_exact_live_27_kph_shape():
    """Live incident this shipped from: MissileEvade held the airframe ~6s
    while speed bled to 27 KPH and altitude then collapsed ~745m in under a
    second — the near-stall the ttg trigger's rate requirement cannot see."""
    c = _ctrl(speed=27, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()


def test_recovery_releases_the_afterburner_immediately():
    """Asymmetric on purpose: engaging needs confirm_reads consecutive low
    readings (don't react to one bad read), but recovering releases on the
    very first reading back above the floor — the same asymmetry ADR 086
    d3 already uses for the ttg trigger's bypass window."""
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    c._read_speed_kph.return_value = 320
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert _releases(c, AFTERBURNER_KEY)


def test_stale_reading_reasserts_but_does_not_release():
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    c._read_speed_kph.return_value = None
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active(), "a stale read released the hold"
    assert len(_presses(c, AFTERBURNER_KEY)) == 2, \
        "should still re-assert on a stale read"


def test_manual_takeover_blocks_engagement():
    c = _ctrl(speed=250, confirm_reads=1, manual_takeover=True)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _presses(c, AFTERBURNER_KEY)


def test_outside_battle_states_does_nothing():
    c = _ctrl(speed=250, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_LOBBY)
    assert not c.is_stall_prevention_active()
    assert not _presses(c, AFTERBURNER_KEY)


def test_game_battle_eject_still_applies():
    """A real stall can happen mid-eject too, independent of the intentional
    descent eject_and_dive itself controls (Non-Goal: this never touches
    eject_and_dive's own nose-down/afterburner sequencing directly — it only
    ever touches AIRBRAKE_KEY, which eject_and_dive does not use)."""
    c = _ctrl(speed=250, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE_EJECT)
    assert c.is_stall_prevention_active()


def test_disabled_does_nothing():
    c = _ctrl(speed=250, confirm_reads=1)
    c._stall_prevention_enabled = False
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _presses(c, AFTERBURNER_KEY)


def test_active_every_tick_reasserts_the_hold():
    """Same D9-precedent reasoning as cruise-afterburner: a harmless repeat
    press each tick it holds, so another subsystem dropping the same key
    behind stall-prevention's back gets undone within one tick."""
    c = _ctrl(speed=250, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert len(_presses(c, AFTERBURNER_KEY)) == 3, \
        "must re-press every tick while holding, not just once"

"""Stall prevention: release airbrake / hold afterburner below a speed
floor. Operator directive, 2026-09-19.

Live: a MissileEvade hold ran ~6s while speed bled to a confirmed 27 KPH
(a 3 KPH reading 3s before that), with nothing watching airspeed at all —
the aircraft then lost ~745m of altitude in under a second. Airbrake adds
drag, exactly wrong during a stall; Climb's own emergency mode deliberately
holds airbrake (ADR 137) for the opposite reason. This overrides that when
both are true at once — a stalled airframe has degraded control authority
regardless of what the pitch axis is commanding.

Tree-independent, called every tick like ADR 134 D9's note_afterburner_cruise
— same test shape as tests/test_afterburner_cruise.py.
"""

import unittest.mock as mock

from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY, AIRBRAKE_KEY


def _ctrl(min_speed_kph=300.0, confirm_reads=2, speed=None, manual_takeover=False):
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
    c = _ctrl(speed=600)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _presses(c, AFTERBURNER_KEY)
    assert not _releases(c, AIRBRAKE_KEY)


def test_a_single_low_reading_does_not_trigger():
    """confirm_reads debounces entry — one bad low reading must not release
    the airbrake, the same discipline every other emergency trigger in this
    codebase uses."""
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _releases(c, AIRBRAKE_KEY)


def test_confirm_reads_consecutive_low_readings_trigger():
    c = _ctrl(speed=250, confirm_reads=2)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    assert _releases(c, AIRBRAKE_KEY)
    assert _presses(c, AFTERBURNER_KEY)


def test_the_exact_live_shape_27_kph():
    c = _ctrl(speed=27, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    assert _releases(c, AIRBRAKE_KEY)
    assert _presses(c, AFTERBURNER_KEY)


def test_speed_recovering_above_the_floor_releases_immediately():
    """Unlike entry, exit is a single good reading — there is nothing held
    to debounce releasing, the airbrake release is a one-shot action."""
    c = _ctrl(speed=250, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert c.is_stall_prevention_active()
    c._read_speed_kph.return_value = 500
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()


def test_a_stale_reading_does_nothing():
    c = _ctrl(speed=None)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _releases(c, AIRBRAKE_KEY)
    assert not _presses(c, AFTERBURNER_KEY)


def test_manual_takeover_blocks_it_entirely():
    c = _ctrl(speed=27, confirm_reads=1, manual_takeover=True)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _releases(c, AIRBRAKE_KEY)
    assert not _presses(c, AFTERBURNER_KEY)


def test_outside_battle_states_does_nothing():
    c = _ctrl(speed=27, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_LOBBY)
    assert not c.is_stall_prevention_active()
    assert not _releases(c, AIRBRAKE_KEY)


def test_battle_eject_state_still_applies():
    """A stall can happen mid-eject too — no reason to exempt it."""
    c = _ctrl(speed=27, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE_EJECT)
    assert c.is_stall_prevention_active()


def test_disabled_does_nothing():
    c = _ctrl(speed=27, confirm_reads=1)
    c._stall_prevention_enabled = False
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert not c.is_stall_prevention_active()
    assert not _releases(c, AIRBRAKE_KEY)


def test_releases_airbrake_every_tick_while_active():
    """Same D9 reasoning as cruise: re-assert every tick so anything else
    (Climb's own emergency hold) re-pressing airbrake behind this gets
    undone within one tick instead of staying held."""
    c = _ctrl(speed=27, confirm_reads=1)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    c.note_stall_prevention(GameState.GAME_BATTLE)
    assert len(_releases(c, AIRBRAKE_KEY)) == 3
    assert len(_presses(c, AFTERBURNER_KEY)) == 3

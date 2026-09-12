"""Cruise afterburner: hold AB in a fuel hysteresis band. ADR 134 D9.

Afterburner was only ever pressed inside three narrow tactics (evade, climb,
eject). This holds it the rest of the time too — burn from wherever fuel is
down to min_fuel_pct, then wait for a real recharge up to rearm_fuel_pct
before holding again, rather than re-arming at just above the floor — so the
aircraft spends real time either above the floor or actively burning, never
idling near-full with afterburner unused.

D9: overrides climb, missile evade and eject rather than deferring to them.
Each already toggles the same key on its own schedule (its own fuel floor,
its own gating), so deferring to "is that tactic active" left gaps where fuel
recovered unused while cruise sat out. Cruise re-asserts the press every
tick it may hold, winning the key back within one tick of anything else
releasing it. The only things it still yields to are manual takeover and
being outside GAME_BATTLE / an inactive mission — not tactic state.

Synchronous, no thread, ticked once per frame like ADR 128's note_incoming().
"""

import unittest.mock as mock

from wingman.analyzer import GameState
from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY


def _ctrl(min_fuel_pct=40.0, rearm_fuel_pct=90.0, confirm_reads=2, fuel=None,
          manual_takeover=False, climb_emergency=False):
    c = Controller.__new__(Controller)
    c._cruise_ab_enabled = True
    c._cruise_ab_min_fuel_pct = min_fuel_pct
    c._cruise_ab_rearm_fuel_pct = rearm_fuel_pct
    c._cruise_ab_confirm_reads = confirm_reads
    c._cruise_ab_active = False
    c._cruise_ab_low_streak = 0
    c._climb_emergency_active = climb_emergency
    c._climb_key = mock.MagicMock()
    c._manual_takeover_active = mock.MagicMock(return_value=manual_takeover)
    c._read_fuel_pct = mock.MagicMock(return_value=fuel)
    return c


def _presses(c):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == AFTERBURNER_KEY and k.kwargs.get("press")]


def _releases(c):
    return [k.args[0] for k in c._climb_key.call_args_list
            if k.args and k.args[0] == AFTERBURNER_KEY
            and k.kwargs.get("press") is False]


def test_fuel_at_or_above_rearm_holds_the_afterburner():
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    assert _presses(c)


def test_fuel_exactly_at_rearm_boundary_engages():
    c = _ctrl(fuel=90, rearm_fuel_pct=90.0)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()


def test_fuel_between_floor_and_rearm_does_not_engage():
    """The hysteresis gap — D8's whole point. Re-arming at just above the
    floor (D4's original design) meant the aircraft barely left the floor
    before diving back through it."""
    c = _ctrl(fuel=70)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert not _presses(c)


def test_fuel_at_or_below_the_floor_never_engages():
    c = _ctrl(fuel=40)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert not _presses(c)


def test_a_single_low_reading_does_not_release_the_hold():
    c = _ctrl(fuel=95, confirm_reads=2)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    c._read_fuel_pct.return_value = 38
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising(), "released on a single low reading"
    assert not _releases(c)


def test_confirm_reads_consecutive_low_readings_release_the_hold():
    c = _ctrl(fuel=95, confirm_reads=2)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    c._read_fuel_pct.return_value = 35
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert _releases(c)
    # Stays released while fuel sits in the hysteresis gap — it must recharge
    # all the way to rearm_fuel_pct, not just tick back above the floor.
    c._read_fuel_pct.return_value = 41
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising(), "re-armed inside the hysteresis gap"
    c._read_fuel_pct.return_value = 90
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising(), "did not re-arm once fuel recharged"


def test_active_every_tick_reasserts_the_press():
    """D9: a harmless repeat press each tick it holds, so another subsystem
    dropping the same key behind cruise's back gets undone within one tick."""
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert len(_presses(c)) == 3, "must re-press every tick while holding, not just once"


def test_other_tactics_no_longer_block_engagement():
    """D9: overrides climb/evade/eject rather than deferring to them — each
    already toggles the same key on its own schedule, so deferring to
    'tactic selected' left gaps where fuel recovered unused."""
    for flag in ("is_ejecting", "is_climbing", "is_missile_evading", "is_afterburner_evading"):
        c = _ctrl(fuel=95)
        setattr(c, flag, mock.MagicMock(return_value=True))
        c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
        assert c.is_afterburner_cruising(), f"deferred to {flag}"
        assert _presses(c)


def test_outside_battle_or_mission_not_running_releases_and_stays_off():
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    c.note_afterburner_cruise(GameState.GAME_LOBBY, True)
    assert not c.is_afterburner_cruising()
    assert _releases(c)

    c2 = _ctrl(fuel=95)
    c2.note_afterburner_cruise(GameState.GAME_BATTLE, False)
    assert not c2.is_afterburner_cruising()


def test_ejecting_holds_afterburner_despite_mission_running_false():
    """GAME_BATTLE_EJECT is its own FSM state, and mission_running reads
    False throughout an eject (the mission thread isn't running — eject was
    never gated on it elsewhere either, see fire_eject). Both silently
    blocked cruise even after D9 removed the is_ejecting() check; this is
    the fix for that (live-observed 2026-09-07)."""
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE_EJECT, False)
    assert c.is_afterburner_cruising()
    assert _presses(c)


def test_manual_takeover_blocks_a_new_press():
    c = _ctrl(fuel=95, manual_takeover=True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert not _presses(c)


def test_manual_takeover_releases_an_existing_hold():
    """SAF-001 is the one deferral D9 keeps — this is not tactic state, it's
    the operator physically flying."""
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    c._manual_takeover_active.return_value = True
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert _releases(c)


def test_climb_emergency_blocks_a_new_press():
    """ADR 137: the one exception to D9's "override everything" — measured
    live 2026-09-09, cruise re-pressing the key inside 10 of 18 emergency
    climb windows cancelled the airbrake's own deceleration each time."""
    c = _ctrl(fuel=95, climb_emergency=True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert not _presses(c)


def test_climb_emergency_releases_an_existing_hold():
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    c._climb_emergency_active = True
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert _releases(c)


def test_climb_emergency_ending_lets_cruise_resume():
    c = _ctrl(fuel=95, climb_emergency=True)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    c._climb_emergency_active = False
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()


def test_ejecting_still_holds_afterburner_when_not_a_climb_emergency():
    """D9's other overrides (eject, evade, climb's own fuel logic) are
    unaffected — only the climb-emergency case is a new exception."""
    c = _ctrl(fuel=95, climb_emergency=False)
    c.note_afterburner_cruise(GameState.GAME_BATTLE_EJECT, False)
    assert c.is_afterburner_cruising()


def test_stale_fuel_reading_reasserts_but_does_not_release():
    c = _ctrl(fuel=95)
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising()
    c._read_fuel_pct.return_value = None
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert c.is_afterburner_cruising(), "a stale read released the hold"
    assert not _releases(c)
    assert len(_presses(c)) == 2, "should still re-assert on a stale read"


def test_disabled_does_nothing():
    c = _ctrl(fuel=95)
    c._cruise_ab_enabled = False
    c.note_afterburner_cruise(GameState.GAME_BATTLE, True)
    assert not c.is_afterburner_cruising()
    assert not _presses(c)

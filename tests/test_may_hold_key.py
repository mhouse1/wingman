"""ADR 139 D4: the shared AFTERBURNER_KEY/AIRBRAKE_KEY arbitration point.

A consolidation, not a fix — each requester's condition here reproduces
what its own call site did before D4, not a normalized policy. See
docs/adr/139-behavior-tree-slot-composition-and-wiring.md D4.
"""

import unittest.mock as mock

import pytest

from wingman.controller import Controller
from wingman.keybindings import AFTERBURNER_KEY


def _ctrl(manual_takeover=False, climb_emergency=False):
    c = Controller.__new__(Controller)
    c._manual_takeover_active = mock.MagicMock(return_value=manual_takeover)
    c._climb_emergency_active = climb_emergency
    return c


def test_cruise_yields_to_manual_takeover():
    assert _ctrl(manual_takeover=True)._may_hold_key(
        AFTERBURNER_KEY, requester="cruise") is False


def test_cruise_yields_to_climb_emergency():
    assert _ctrl(climb_emergency=True)._may_hold_key(
        AFTERBURNER_KEY, requester="cruise") is False


def test_cruise_may_hold_absent_both():
    assert _ctrl()._may_hold_key(AFTERBURNER_KEY, requester="cruise") is True


@pytest.mark.parametrize("requester", ["climb", "missile_evade", "afterburner_evade", "eject"])
def test_other_requesters_are_unconditional_today(requester):
    """Direct audit found none of these four check manual takeover or climb
    emergency — this pins that as the current, replicated behavior, not an
    oversight introduced by the consolidation."""
    c = _ctrl(manual_takeover=True, climb_emergency=True)
    assert c._may_hold_key(AFTERBURNER_KEY, requester=requester) is True


def test_unknown_requester_raises():
    with pytest.raises(ValueError):
        _ctrl()._may_hold_key(AFTERBURNER_KEY, requester="not_a_real_tactic")

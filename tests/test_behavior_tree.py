"""Unit tests for the Phase 3.0 shadow behavior tree (ADR 024).

Node-level, per the ADR: given snapshot X → assert selected tactic Y.
No Controller threads, no OCR, no real clock.
"""

import py_trees
import pytest

from wingman.analyzer import GameState
from wingman.behavior_tree import (
    AnalyzerSnapshot,
    MinimumHold,
    TACTIC_ATTACK_SUPPORT,
    TACTIC_DISENGAGE,
    TACTIC_EJECT,
    TACTIC_ENGAGE,
    TACTIC_EVADE,
    TACTIC_IDLE,
    TACTIC_RESPAWN_WAIT,
    build_tree,
    make_snapshot_writer,
    selected_tactic,
)

BT_CFG = {"disengage_after_s": 30, "disengage_hold_s": 10, "evade_hold_s": 10}


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def clean_blackboard():
    py_trees.blackboard.Blackboard.clear()
    yield
    py_trees.blackboard.Blackboard.clear()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def harness(clock):
    tree = build_tree(dict(BT_CFG), clock=clock)
    writer = make_snapshot_writer()
    return tree, writer


def make_snap(**overrides):
    base = dict(
        health=250, missiles=4, flares=6,
        ring_short=0, ring_mid=0, ring_long=0,
        enemy_absent_seconds=0.0, altitude=5000.0,
        is_respawning=False, incoming_detected=False,
        mission_running=True, game_state=GameState.GAME_BATTLE,
    )
    base.update(overrides)
    return AnalyzerSnapshot(**base)


def tick(harness, snap):
    tree, writer = harness
    writer.set("snapshot", snap)
    tree.tick()
    return selected_tactic(tree)


def test_idle_outside_battle(harness):
    assert tick(harness, make_snap(game_state=GameState.GAME_LOBBY)) == TACTIC_IDLE
    assert tick(harness, make_snap(game_state=GameState.GAME_BATTLE_MANUAL)) == TACTIC_IDLE
    assert tick(harness, make_snap(game_state=GameState.GAME_BATTLE_EJECT)) == TACTIC_IDLE


def test_respawn_wait_beats_everything_in_battle(harness):
    snap = make_snap(is_respawning=True, missiles=0, ring_short=3)
    assert tick(harness, snap) == TACTIC_RESPAWN_WAIT


def test_eject_on_missiles_empty_beats_engage(harness):
    snap = make_snap(missiles=0, ring_short=2, ring_mid=1)
    assert tick(harness, snap) == TACTIC_EJECT


def test_engage_when_any_ring_occupied(harness):
    assert tick(harness, make_snap(ring_long=1)) == TACTIC_ENGAGE
    assert tick(harness, make_snap(ring_mid=2)) == TACTIC_ENGAGE
    assert tick(harness, make_snap(ring_short=1)) == TACTIC_ENGAGE


def test_attack_support_is_the_fallback(harness):
    snap = make_snap(enemy_absent_seconds=5.0)   # no contacts, not absent long enough
    assert tick(harness, snap) == TACTIC_ATTACK_SUPPORT


def test_disengage_selects_after_absence_and_holds(harness, clock):
    # All rings empty for 30s+ → Disengage.
    assert tick(harness, make_snap(enemy_absent_seconds=31.0)) == TACTIC_DISENGAGE
    # Contacts reappear immediately — the hold keeps Disengage selected
    # (the anti-flap semantics ADR 024 assigns to the decorator).
    clock.advance(1.5)
    assert tick(harness, make_snap(ring_mid=1)) == TACTIC_DISENGAGE
    # Hold expired → selection falls through to Engage.
    clock.advance(11.0)
    assert tick(harness, make_snap(ring_mid=1)) == TACTIC_ENGAGE


def test_evade_disabled_without_threshold(harness):
    snap = make_snap(health=10, ring_mid=1)
    assert tick(harness, snap) == TACTIC_ENGAGE


def test_evade_selected_when_threshold_configured(clock):
    cfg = dict(BT_CFG, evade_health_threshold=50)
    tree = build_tree(cfg, clock=clock)
    writer = make_snapshot_writer()
    harness = (tree, writer)
    assert tick(harness, make_snap(health=30, ring_mid=1)) == TACTIC_EVADE
    # Health recovers — the hold prevents flapping straight back to Engage.
    clock.advance(1.5)
    assert tick(harness, make_snap(health=200, ring_mid=1)) == TACTIC_EVADE
    clock.advance(11.0)
    assert tick(harness, make_snap(health=200, ring_mid=1)) == TACTIC_ENGAGE


def test_missiles_unknown_is_not_empty(harness):
    assert tick(harness, make_snap(missiles=None, ring_mid=1)) == TACTIC_ENGAGE


# ---------------------------------------------------------------------------
# Phase 3.1b — actuating leaves (ADR 024)
# ---------------------------------------------------------------------------

class _TacticRecorder:
    """start_fn / is_running_fn pair that records starts."""

    def __init__(self):
        self.starts = 0
        self.running = False

    def start(self):
        self.starts += 1

    def is_running(self):
        return self.running


def make_actuated_harness(clock, eject=None, disengage=None):
    actuators = {}
    if eject is not None:
        actuators[TACTIC_EJECT] = (eject.start, eject.is_running)
    if disengage is not None:
        actuators[TACTIC_DISENGAGE] = (disengage.start, disengage.is_running)
    tree = build_tree(dict(BT_CFG), clock=clock, actuators=actuators)
    return tree, make_snapshot_writer()


def test_actuated_eject_fires_on_confirmed_verdict_only(clock):
    """3.1b gate: the actuating Eject leaf consumes the DEBOUNCED verdict —
    a raw missiles==0 read (e.g. stale post-respawn ammo, the 2026-08-08
    shadow finding) must neither select nor actuate."""
    eject = _TacticRecorder()
    harness = make_actuated_harness(clock, eject=eject)

    # Raw zero without confirmation: no selection, no actuation.
    assert tick(harness, make_snap(missiles=0, ring_mid=1)) == TACTIC_ENGAGE
    assert eject.starts == 0

    # Confirmed verdict: selected and started.
    snap = make_snap(missiles=0, missiles_empty_confirmed=True)
    assert tick(harness, snap) == TACTIC_EJECT
    assert eject.starts == 1


def test_actuated_eject_does_not_restart_while_running(clock):
    eject = _TacticRecorder()
    harness = make_actuated_harness(clock, eject=eject)
    tick(harness, make_snap(missiles=0, missiles_empty_confirmed=True))
    assert eject.starts == 1
    eject.running = True
    tick(harness, make_snap(missiles=0, missiles_empty_confirmed=True))
    assert eject.starts == 1  # is_running_fn gates the re-start


def test_actuated_eject_switchaway_does_not_cancel(clock):
    """FSM entering GAME_BATTLE_EJECT flips selection to Idle — that is the
    eject SUCCEEDING; terminate must not cancel anything (no exception, no
    stop call exists to make)."""
    eject = _TacticRecorder()
    harness = make_actuated_harness(clock, eject=eject)
    tick(harness, make_snap(missiles=0, missiles_empty_confirmed=True))
    eject.running = True
    selection = tick(harness, make_snap(
        missiles=0, game_state=GameState.GAME_BATTLE_EJECT))
    assert selection == TACTIC_IDLE
    assert eject.starts == 1


def test_actuated_disengage_fires_once_per_selection(clock):
    disengage = _TacticRecorder()
    harness = make_actuated_harness(clock, disengage=disengage)

    assert tick(harness, make_snap(enemy_absent_seconds=31.0)) == TACTIC_DISENGAGE
    assert disengage.starts == 1
    disengage.running = True
    clock.advance(1.5)
    assert tick(harness, make_snap(enemy_absent_seconds=32.5)) == TACTIC_DISENGAGE
    assert disengage.starts == 1  # roll in progress — no re-fire

    # Absence clock re-armed by the start_fn (handler side): condition drops,
    # hold expires, selection falls through.
    disengage.running = False
    clock.advance(11.0)
    assert tick(harness, make_snap(enemy_absent_seconds=2.0, ring_mid=1)) == TACTIC_ENGAGE
    assert disengage.starts == 1


class _FlagChild(py_trees.behaviour.Behaviour):
    def __init__(self):
        super().__init__("flag")
        self.active = True

    def update(self):
        if self.active:
            return py_trees.common.Status.RUNNING
        return py_trees.common.Status.FAILURE


def test_minimum_hold_decorator_unit():
    clock = FakeClock()
    child = _FlagChild()
    hold = MinimumHold("hold", child, hold_s=10, clock=clock)
    hold.tick_once()
    assert hold.status == py_trees.common.Status.RUNNING
    child.active = False
    clock.advance(5)
    hold.tick_once()
    assert hold.status == py_trees.common.Status.RUNNING   # held
    clock.advance(6)
    hold.tick_once()
    assert hold.status == py_trees.common.Status.FAILURE   # hold expired

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

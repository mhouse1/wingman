"""Phase 3 tactic-selection behavior tree (ADR 024, revised 2026-08-08).

Shadow-first rollout: Phase 3.0 ticks the tree once per loop tick and logs
the selected tactic without actuating anything — the legacy handlers keep
flying while the log accumulates selection-agreement evidence. Phase 3.1
wires the leaves' ``start_fn``/``terminate`` to Controller tactics and
retires the corresponding handlers.

Layout (priority selector, top wins):

    Idle → RespawnWait → Eject → Evade(hold) → Disengage(hold) → Engage → AttackSupport

All leaves read one frozen ``AnalyzerSnapshot`` from the py-trees blackboard;
no leaf holds a reference to the live analyzer. ``MinimumHold`` is the small
custom decorator ADR 024 calls for (``Cooldown`` is not a stock py-trees
decorator): once a tactic is selected it stays selected for a minimum
duration, preventing selection flapping.
"""

import time
from dataclasses import dataclass

import py_trees

from .analyzer import GameState

SNAPSHOT_KEY = "snapshot"

TACTIC_IDLE = "Idle"
TACTIC_RESPAWN_WAIT = "RespawnWait"
TACTIC_EJECT = "Eject"
TACTIC_EVADE = "Evade"
TACTIC_DISENGAGE = "Disengage"
TACTIC_ENGAGE = "Engage"
TACTIC_ATTACK_SUPPORT = "AttackSupport"


@dataclass(frozen=True)
class AnalyzerSnapshot:
    """One tick's perception, frozen before the tree is ticked (ADR 024)."""

    health: "int | None"
    missiles: "int | None"
    flares: "int | None"
    ring_short: int               # minimap red components per ring (Design 003)
    ring_mid: int
    ring_long: int
    enemy_absent_seconds: float   # seconds since any ring was occupied
    altitude: "float | None"      # fresh telemetry stable value (ADR 038)
    is_respawning: bool
    incoming_detected: bool
    mission_running: bool
    game_state: GameState

    @property
    def contacts(self) -> int:
        return self.ring_short + self.ring_mid + self.ring_long


class MinimumHold(py_trees.decorators.Decorator):
    """Keep a selected tactic selected for a minimum duration.

    Once the child returns RUNNING, this decorator keeps returning RUNNING
    for ``hold_s`` even if the child's condition drops to FAILURE — the
    anti-flapping hold ADR 024 assigns to EVADE and DISENGAGE. The child is
    still ticked every cycle, so a genuinely persisting condition simply
    refreshes the hold.
    """

    def __init__(self, name: str, child: py_trees.behaviour.Behaviour,
                 hold_s: float, clock=time.time):
        super().__init__(name=name, child=child)
        self._hold_s = float(hold_s)
        self._clock = clock
        self._held_until = 0.0

    def update(self) -> py_trees.common.Status:
        now = self._clock()
        status = self.decorated.status
        if status == py_trees.common.Status.RUNNING:
            self._held_until = now + self._hold_s
            return status
        if now < self._held_until:
            return py_trees.common.Status.RUNNING
        return status


class ConditionTactic(py_trees.behaviour.Behaviour):
    """Leaf returning RUNNING while its condition holds, else FAILURE.

    Phase 3.0 shadow: ``start_fn``/``is_running_fn`` stay None — the leaf is
    selection only. Phase 3.1 fills them per the TacticAction glue in
    ADR 024: ``update`` starts the Controller tactic when selected, and
    ``terminate(INVALID)`` cancels it when the selector switches away.
    """

    def __init__(self, name: str, condition, start_fn=None, is_running_fn=None):
        super().__init__(name=name)
        self._condition = condition
        self._start_fn = start_fn
        self._is_running_fn = is_running_fn
        self._bb = py_trees.blackboard.Client(name=f"{name}Client")
        self._bb.register_key(key=SNAPSHOT_KEY, access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        snapshot = self._bb.get(SNAPSHOT_KEY)
        if not self._condition(snapshot):
            return py_trees.common.Status.FAILURE
        if self._start_fn is not None and self._is_running_fn is not None:
            if not self._is_running_fn():
                self._start_fn()
        return py_trees.common.Status.RUNNING

    def terminate(self, new_status: py_trees.common.Status) -> None:
        # Phase 3.1: cancel the running Controller tactic when the selector
        # switches away (new_status == INVALID). Shadow phase: nothing to stop.
        pass


def is_idle(snapshot: AnalyzerSnapshot) -> bool:
    """The tree only decides inside GAME_BATTLE; every other state — manual,
    eject, lobby — is owned elsewhere and selects Idle."""
    return snapshot.game_state != GameState.GAME_BATTLE


def is_respawning(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.is_respawning


def is_missiles_empty(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.missiles == 0


def make_evade_condition(health_threshold: "int | None"):
    def evade(snapshot: AnalyzerSnapshot) -> bool:
        if health_threshold is None:
            return False   # ADR 024: disabled until calibrated
        return snapshot.health is not None and snapshot.health < health_threshold
    return evade


def make_disengage_condition(absent_after_s: float):
    def disengage(snapshot: AnalyzerSnapshot) -> bool:
        return snapshot.enemy_absent_seconds >= absent_after_s
    return disengage


def has_contacts(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.contacts > 0


def always(snapshot: AnalyzerSnapshot) -> bool:
    return True


def build_tree(bt_cfg: dict, clock=time.time) -> py_trees.trees.BehaviourTree:
    """Construct the ADR 024 selector. Pure construction — no analyzer refs."""
    disengage_after_s = float(bt_cfg.get("disengage_after_s", 30.0))
    disengage_hold_s = float(bt_cfg.get("disengage_hold_s", 10.0))
    evade_hold_s = float(bt_cfg.get("evade_hold_s", 10.0))
    evade_threshold = bt_cfg.get("evade_health_threshold")

    root = py_trees.composites.Selector(
        name="TacticSelector",
        memory=False,
        children=[
            ConditionTactic(TACTIC_IDLE, is_idle),
            ConditionTactic(TACTIC_RESPAWN_WAIT, is_respawning),
            ConditionTactic(TACTIC_EJECT, is_missiles_empty),
            MinimumHold(
                TACTIC_EVADE,
                ConditionTactic(f"{TACTIC_EVADE}Condition",
                                make_evade_condition(evade_threshold)),
                hold_s=evade_hold_s, clock=clock,
            ),
            MinimumHold(
                TACTIC_DISENGAGE,
                ConditionTactic(f"{TACTIC_DISENGAGE}Condition",
                                make_disengage_condition(disengage_after_s)),
                hold_s=disengage_hold_s, clock=clock,
            ),
            ConditionTactic(TACTIC_ENGAGE, has_contacts),
            ConditionTactic(TACTIC_ATTACK_SUPPORT, always),
        ],
    )
    return py_trees.trees.BehaviourTree(root)


def make_snapshot_writer() -> py_trees.blackboard.Client:
    writer = py_trees.blackboard.Client(name="SnapshotWriter")
    writer.register_key(key=SNAPSHOT_KEY, access=py_trees.common.Access.WRITE)
    return writer


def selected_tactic(tree: py_trees.trees.BehaviourTree) -> str:
    """Name of the tactic the selector chose this tick ('none' before ticks)."""
    for child in tree.root.children:
        if child.status == py_trees.common.Status.RUNNING:
            return child.name
    return "none"

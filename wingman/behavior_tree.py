"""Phase 3 tactic-selection behavior tree (ADR 024, revised 2026-08-08).

Shadow-first rollout: Phase 3.0 ticks the tree once per loop tick and logs
the selected tactic without actuating anything — the legacy handlers keep
flying while the log accumulates selection-agreement evidence. Phase 3.1
wires the leaves' ``start_fn``/``terminate`` to Controller tactics and
retires the corresponding handlers.

Layout (priority selector, top wins):

    Idle → RespawnWait → Eject → MissileEvade → Evade(hold) → Disengage(hold) → [Climb] → Engage → AttackSupport

Climb (ADR 073) joins the selector only when ``behavior_tree.climb.enabled``
is true; while disabled it is shadow-logged by BehaviorTreeHandler instead of
inserted, so live selection is untouched.

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
TACTIC_MISSILE_EVADE = "MissileEvade"
TACTIC_EVADE = "Evade"
TACTIC_DISENGAGE = "Disengage"
TACTIC_CLIMB = "Climb"
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
    # ADR 024 3.1b: AmmoEventsHandler's debounced no-missiles verdict — the
    # streak plus every suppression gate (respawn, grace windows) already
    # applied. The actuating Eject leaf consumes THIS, never the raw
    # ``missiles`` read (the 2026-08-08 shadow-session gate).
    missiles_empty_confirmed: bool = False

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
        # Deliberately a no-op for the 3.1b tactics. Eject: the selector
        # switching to Idle means the FSM entered GAME_BATTLE_EJECT — that is
        # the tactic SUCCEEDING, not being pre-empted, and cancelling would
        # abort the dive just started. Disengage: the roll is a one-shot
        # maneuver that completes on its own. Engage geometry is stopped by
        # the navigator reset on state exit, not by leaf termination.
        pass


def is_idle(snapshot: AnalyzerSnapshot) -> bool:
    """The tree only decides inside GAME_BATTLE; every other state — manual,
    eject, lobby — is owned elsewhere and selects Idle."""
    return snapshot.game_state != GameState.GAME_BATTLE


def is_respawning(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.is_respawning


def is_missiles_empty(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.missiles == 0


def is_eject_confirmed(snapshot: AnalyzerSnapshot) -> bool:
    """The debounced verdict — used instead of the raw read once the Eject
    leaf actuates (ADR 024 3.1b gate)."""
    return snapshot.missiles_empty_confirmed


def make_missile_evade_condition(is_running_fn=None):
    """ADR 070: true on incoming detection, sticky while the evade hold runs.

    The stickiness is what keeps Engage from re-selecting on the first clear
    tick and pulsing the roll axis while the evade thread still owns it. The
    running state is captured (ConditionTactic passes conditions only the
    snapshot); selection-only builds pass no is_running_fn and fall back to
    the bare incoming_detected predicate. No MinimumHold: the anti-flap hold
    lives in the thread's own clear timer, and a second independent hold would
    desynchronise selection from actuation. mission_running is deliberately
    not tested (ADR 070 d9) — a missile is a threat with or without a mission
    thread, and the tactic never touches mission state.
    """
    def missile_evade(snapshot: AnalyzerSnapshot) -> bool:
        if snapshot.incoming_detected:
            return True
        return is_running_fn is not None and is_running_fn()
    return missile_evade


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


def make_climb_condition(enter_below_alt: "float | None",
                         exit_above_alt: "float | None",
                         is_running_fn=None,
                         confirm_reads: int = 1):
    """ADR 073: hysteresis band on the telemetry stable altitude.

    Enters below ``enter_below_alt``, releases only at or above
    ``exit_above_alt`` — a single threshold would flap at the boundary every
    telemetry tick. ``altitude is None`` FREEZES the decision (neither enters
    nor releases): entering blind would command climbs on OCR dropouts, and
    releasing blind would flap selection through telemetry gaps. A long blind
    climb is bounded by the actuating thread's own duration backstop
    (Phase 3.2b), never by this condition. Unset thresholds disable the leaf
    (the Evade precedent). ``is_running_fn`` keeps selection sticky while an
    actuated climb thread owns the pitch axis (the ADR 070 pattern);
    selection-only builds pass none and get the bare band.

    ``confirm_reads`` (Phase 3.2b): band crossings only count after this many
    CONSECUTIVE agreeing reads, in both directions. The 3.2a shadow sessions
    showed single garbage stable-values (alt=1, 8, 73 mid-flight, next read
    1400+) entering the band — one bad read must never command a climb, and
    one bad high must never release a genuine one. None reads neither count
    toward nor reset a streak (the freeze policy applied to the debounce).
    """
    state = {"active": False, "streak": 0}

    def climb(snapshot: AnalyzerSnapshot) -> bool:
        if enter_below_alt is None or exit_above_alt is None:
            return False   # ADR 073: disabled until calibrated
        alt = snapshot.altitude
        if alt is not None:
            if state["active"]:
                crossing = alt >= exit_above_alt
            else:
                crossing = alt < enter_below_alt
            if crossing:
                state["streak"] += 1
                if state["streak"] >= max(1, int(confirm_reads)):
                    state["active"] = not state["active"]
                    state["streak"] = 0
            else:
                state["streak"] = 0
        return state["active"] or (is_running_fn is not None and is_running_fn())
    return climb


def has_contacts(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.contacts > 0


def always(snapshot: AnalyzerSnapshot) -> bool:
    return True


def build_tree(bt_cfg: dict, clock=time.time,
               actuators: "dict | None" = None) -> py_trees.trees.BehaviourTree:
    """Construct the ADR 024 selector. Pure construction — no analyzer refs.

    ``actuators`` (Phase 3.1b) maps tactic name → ``(start_fn, is_running_fn)``
    for the leaves that actuate Controller tactics; absent entries stay
    selection-only. When the Eject leaf actuates, its condition switches from
    the raw missiles read to the debounced ``missiles_empty_confirmed``
    verdict — the shadow-session gate. Evade remains selection-only: no
    Controller tactic exists for it, and its threshold is unset until
    calibrated (ADR 024).
    """
    disengage_after_s = float(bt_cfg.get("disengage_after_s", 30.0))
    disengage_hold_s = float(bt_cfg.get("disengage_hold_s", 10.0))
    evade_hold_s = float(bt_cfg.get("evade_hold_s", 10.0))
    evade_threshold = bt_cfg.get("evade_health_threshold")
    climb_cfg = bt_cfg.get("climb", {}) or {}
    actuators = actuators or {}
    eject_fns = actuators.get(TACTIC_EJECT)
    disengage_fns = actuators.get(TACTIC_DISENGAGE)
    missile_evade_fns = actuators.get(TACTIC_MISSILE_EVADE)
    climb_fns = actuators.get(TACTIC_CLIMB)

    if eject_fns is not None:
        eject_leaf = ConditionTactic(TACTIC_EJECT, is_eject_confirmed,
                                     start_fn=eject_fns[0],
                                     is_running_fn=eject_fns[1])
    else:
        eject_leaf = ConditionTactic(TACTIC_EJECT, is_missiles_empty)

    # ADR 070: the is_running_fn feeds BOTH the actuation gate and the
    # condition's stickiness — the selection must not fall through to Engage
    # while the evade thread still owns the roll axis.
    if missile_evade_fns is not None:
        missile_evade_leaf = ConditionTactic(
            TACTIC_MISSILE_EVADE,
            make_missile_evade_condition(missile_evade_fns[1]),
            start_fn=missile_evade_fns[0],
            is_running_fn=missile_evade_fns[1])
    else:
        missile_evade_leaf = ConditionTactic(
            TACTIC_MISSILE_EVADE, make_missile_evade_condition())

    disengage_kwargs = {}
    if disengage_fns is not None:
        disengage_kwargs = {"start_fn": disengage_fns[0],
                            "is_running_fn": disengage_fns[1]}

    children = [
        ConditionTactic(TACTIC_IDLE, is_idle),
        ConditionTactic(TACTIC_RESPAWN_WAIT, is_respawning),
        eject_leaf,
        missile_evade_leaf,
        MinimumHold(
            TACTIC_EVADE,
            ConditionTactic(f"{TACTIC_EVADE}Condition",
                            make_evade_condition(evade_threshold)),
            hold_s=evade_hold_s, clock=clock,
        ),
        MinimumHold(
            TACTIC_DISENGAGE,
            ConditionTactic(f"{TACTIC_DISENGAGE}Condition",
                            make_disengage_condition(disengage_after_s),
                            **disengage_kwargs),
            hold_s=disengage_hold_s, clock=clock,
        ),
        ConditionTactic(TACTIC_ENGAGE, has_contacts),
        ConditionTactic(TACTIC_ATTACK_SUPPORT, always),
    ]

    # ADR 073: the Climb leaf enters the selector ONLY when enabled. A
    # selection-only leaf here would not be shadow — every selection would
    # pre-empt Engage actuation and silently pause geometry at low altitude.
    # While disabled, BehaviorTreeHandler logs would-select from an
    # independent condition instance instead.
    if bool(climb_cfg.get("enabled", False)):
        climb_kwargs = {}
        if climb_fns is not None:
            climb_kwargs = {"start_fn": climb_fns[0],
                            "is_running_fn": climb_fns[1]}
        climb_leaf = ConditionTactic(
            TACTIC_CLIMB,
            make_climb_condition(
                climb_cfg.get("enter_below_alt"),
                climb_cfg.get("exit_above_alt"),
                is_running_fn=climb_fns[1] if climb_fns is not None else None,
                confirm_reads=int(climb_cfg.get("confirm_reads", 1))),
            **climb_kwargs)
        children.insert(len(children) - 2, climb_leaf)   # above Engage

    root = py_trees.composites.Selector(
        name="TacticSelector",
        memory=False,
        children=children,
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

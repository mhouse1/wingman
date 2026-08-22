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

import logging
import time
from dataclasses import dataclass

import py_trees

logger = logging.getLogger(__name__)

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
    # ADR 075: afterburner fuel percentage (0-100) from the FUEL_100 crop,
    # None when unread or stale. Consumed by the Controller's burner gating,
    # carried here so tactics and logging see the same frozen value.
    fuel_pct: "int | None" = None
    # ADR 086 d2: signed altitude rate in m/s, negative while descending. The
    # emergency recovery trigger needs the RATE, not just the value — an
    # altitude threshold cannot distinguish a cruise through 4000 m from a
    # 560 m/s dive through it, and on 2026-08-21 18:41 it did not.
    altitude_rate: "float | None" = None

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
                         confirm_reads: int = 1,
                         recover_below_time_s: "float | None" = None,
                         confirm_bypass_time_s: "float | None" = None,
                         descent_memory_s: float = 5.0,
                         clock=time.time):
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
    state = {"active": False, "streak": 0, "ttg_streak": 0,
             "last_ttg": None, "last_ttg_ts": 0.0}

    def _time_to_ground(snapshot, now):
        """Seconds to impact at the current descent rate, or None.

        ADR 086 d4: a REJECTED reading during an established descent is
        evidence of rapid change, not of safety, so the last known descent is
        held for ``descent_memory_s``. Absence of perception must not read as
        absence of danger.
        """
        alt = snapshot.altitude
        rate = getattr(snapshot, "altitude_rate", None)
        if alt is not None and rate is not None and rate < 0:
            ttg = alt / -rate
            state["last_ttg"], state["last_ttg_ts"] = ttg, now
            return ttg
        if (state["last_ttg"] is not None
                and now - state["last_ttg_ts"] <= descent_memory_s):
            return state["last_ttg"]
        return None

    def climb(snapshot: AnalyzerSnapshot) -> bool:
        if enter_below_alt is None or exit_above_alt is None:
            return False   # ADR 073: disabled until calibrated

        # ADR 086 d2: the emergency trigger is predicted TIME to ground, not
        # altitude. On 2026-08-21 18:41 the aircraft dived 9203 m -> 2301 m in
        # 27 s with 2 missiles aboard while the tree kept selecting Engage:
        # the altitude band never opened because the smoothed altitude lags
        # ~1500 m in a 560 m/s dive and the aircraft hit the ground first.
        now = clock()
        ttg = _time_to_ground(snapshot, now)
        emergency = False
        if recover_below_time_s is not None and ttg is not None \
                and ttg < float(recover_below_time_s):
            if (confirm_bypass_time_s is not None
                    and ttg < float(confirm_bypass_time_s)):
                # ADR 086 d3: inside the bypass window, waiting for a second
                # read spends the very margin the trigger exists to protect.
                emergency = True
            else:
                state["ttg_streak"] += 1
                emergency = state["ttg_streak"] >= max(1, int(confirm_reads))
            if emergency and not state["active"]:
                logger.warning(
                    "BT: DIVE RECOVERY — %.0fs to ground (alt=%s rate=%s) — "
                    "climb forced (ADR 086 d2)",
                    ttg,
                    "n/a" if snapshot.altitude is None else f"{snapshot.altitude:.0f}m",
                    "held" if getattr(snapshot, "altitude_rate", None) is None
                    else f"{snapshot.altitude_rate:+.0f}m/s")
        else:
            state["ttg_streak"] = 0

        alt = snapshot.altitude
        if emergency:
            # The band must not release a recovery it did not start: in this
            # dive the altitude was far ABOVE exit_above_alt the whole way
            # down, so the ordinary hysteresis would have cleared it instantly.
            state["active"] = True
            state["streak"] = 0
        elif alt is not None:
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


def make_sustain_climb_condition(enter_below_alt: "float | None",
                                 exit_above_alt: "float | None",
                                 confirm_reads: int = 1):
    """ADR 075: climb-while-armed altitude sustain band.

    The adaptive J20 doctrine: as long as the aircraft has missiles and a
    mission is running, it works its way up to the operating altitude while
    search-and-destroy runs. Same hysteresis + confirm-reads debounce as the
    ADR 073 emergency band (delegated to ``make_climb_condition``), gated on:

    - ``missiles`` > 0 — an empty aircraft belongs to the Eject leaf, and an
      unreadable count must not command a climb;
    - ``mission_running`` — sustain is mission doctrine, unlike the emergency
      band which fires regardless (terrain outranks everything).

    No is_running stickiness here: the leaf combines this condition with the
    emergency band's, and that one already carries the stickiness for any
    active climb thread.
    """
    band = make_climb_condition(enter_below_alt, exit_above_alt,
                                is_running_fn=None,
                                confirm_reads=confirm_reads)

    def sustain(snapshot: AnalyzerSnapshot) -> bool:
        if snapshot.missiles is None or snapshot.missiles <= 0:
            return False
        if not snapshot.mission_running:
            return False
        return band(snapshot)
    return sustain


def has_contacts(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.contacts > 0


def always(_snapshot: AnalyzerSnapshot) -> bool:
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
        emergency = make_climb_condition(
            climb_cfg.get("enter_below_alt"),
            climb_cfg.get("exit_above_alt"),
            is_running_fn=climb_fns[1] if climb_fns is not None else None,
            confirm_reads=int(climb_cfg.get("confirm_reads", 1)),
            # ADR 086 d2/d3/d4 — time-to-ground recovery. Unset disables it and
            # leaves the pure ADR 073 altitude band.
            recover_below_time_s=climb_cfg.get("recover_below_time_s"),
            confirm_bypass_time_s=climb_cfg.get("confirm_bypass_time_s"),
            descent_memory_s=float(climb_cfg.get("descent_memory_s", 5.0)))
        # ADR 075: the armed altitude-sustain band shares the leaf with the
        # emergency band. Both closures are evaluated EVERY tick (no
        # short-circuit) so neither hysteresis state machine goes stale while
        # the other holds the selection.
        sustain_cfg = climb_cfg.get("sustain", {}) or {}
        if bool(sustain_cfg.get("enabled", False)):
            sustain = make_sustain_climb_condition(
                sustain_cfg.get("enter_below_alt"),
                sustain_cfg.get("exit_above_alt"),
                confirm_reads=int(climb_cfg.get("confirm_reads", 1)))

            def climb_condition(snapshot, _e=emergency, _s=sustain):
                e = _e(snapshot)
                s = _s(snapshot)
                return e or s
        else:
            climb_condition = emergency
        climb_leaf = ConditionTactic(TACTIC_CLIMB, climb_condition,
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

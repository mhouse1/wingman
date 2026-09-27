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
from collections.abc import Callable
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
TACTIC_BOUNDARY_TURN = "BoundaryTurn"
TACTIC_DISENGAGE = "Disengage"
TACTIC_CLIMB = "Climb"
TACTIC_ENGAGE = "Engage"
TACTIC_REGROUP = "Regroup"
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
    # ADR 028 revision 4: friendly / objective minimap icons. Consumed only by
    # Regroup, which fills the ticks where no enemy renders — 59% of them,
    # measured 2026-08-30 — and where the tree previously flew the mission
    # script with nothing steering toward the battle.
    friendly_contacts: int = 0
    # ADR 107: nearest map-boundary range and its forward component, both in
    # minimap radii, or None when the boundary is not readable. Before this the
    # tree had no boundary input at all, which is why nothing in it could act
    # on an aircraft flying out of the arena.
    # ADR 109: mission_loiter owns the aircraft and its objective is survival.
    # Tactics that trade the airframe for a tactical outcome must yield to it.
    survival_hold: bool = False
    boundary_dist: "float | None" = None
    boundary_forward: "float | None" = None
    # ADR 120: nearest reading in the median window. The release decision uses
    # this rather than the filtered value; entry still uses the filtered one.
    boundary_near: "float | None" = None
    # ADR 122: lateral offset of the nearest boundary point, positive to the
    # RIGHT of the nose. The turn rolls away from it.
    boundary_lateral: "float | None" = None
    # Design 011 (ACS Mode), step 1: which weapon-employment tactic the
    # active airframe supports, read once from jet_profile.active at startup.
    # True today for every configured profile — nothing branches on this yet,
    # it exists so a future BoresightEngage leaf has something to condition
    # on without a second decision framework. See docs/hldd/011-acs-mode-hldd.md.
    has_padlock: bool = True
    # HLDD 001 Phase 1: raw per-tick sky fraction in the TERRAIN_FORWARD
    # crop (analyzer.detect_terrain_ahead), or None when unreadable/disabled.
    # The confirm-reads debounce and threshold live in ClimbCondition, same
    # as every other emergency trigger — this is the frozen measurement
    # only, consistent with boundary_dist/altitude above.
    terrain_sky_frac: "float | None" = None

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

    ADR 137 D9: ``update_fn(snapshot)``, when given, is called on every
    subsequent RUNNING tick instead — never on the same tick as ``start_fn``,
    since that only fires on the FAILURE→RUNNING edge. This is the channel a
    tactic already RUNNING uses to see updated per-tick snapshot data;
    before it existed, a leaf's actuator thread only ever saw the value
    frozen into it at selection time (the gap ADR 137's "Third Live Trial"
    found for Climb's emergency flag).
    """

    def __init__(self, name: str, condition, start_fn=None, is_running_fn=None,
                 update_fn=None):
        super().__init__(name=name)
        self._condition = condition
        self._start_fn = start_fn
        self._is_running_fn = is_running_fn
        self._update_fn = update_fn
        self._bb = py_trees.blackboard.Client(name=f"{name}Client")
        self._bb.register_key(key=SNAPSHOT_KEY, access=py_trees.common.Access.READ)

    def update(self) -> py_trees.common.Status:
        snapshot = self._bb.get(SNAPSHOT_KEY)
        if not self._condition(snapshot):
            return py_trees.common.Status.FAILURE
        if self._start_fn is not None and self._is_running_fn is not None:
            if not self._is_running_fn():
                self._start_fn()
            elif self._update_fn is not None:
                self._update_fn(snapshot)
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
    # ADR 109: never during a survival hold. Eject exists to trade a rearmed
    # aircraft for an empty one; mission_loiter exists to keep THIS one alive,
    # and an empty rack is irrelevant to that. Measured 2026-09-04 09:58:45 —
    # Eject dove a loitering aircraft at -71 degrees, suppressed its climb for
    # four seconds, and killed it.
    return snapshot.missiles == 0 and not snapshot.survival_hold


def is_eject_confirmed(snapshot: AnalyzerSnapshot) -> bool:
    """The debounced verdict — used instead of the raw read once the Eject
    leaf actuates (ADR 024 3.1b gate).

    ADR 109: yields to a survival hold, on the same reasoning as
    ``is_missiles_empty`` — this is the actuating path, so it is the one that
    matters in a live session.
    """
    return snapshot.missiles_empty_confirmed and not snapshot.survival_hold


def make_missile_evade_condition(is_running_fn=None, yields_to_fn=None):
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

    Phase 1 (operator directive: "during evade maneuvers... if altitude
    below [the floor] it should automatically fly up"): ``yields_to_fn``,
    when given, is checked first — identical pattern to BoundaryTurn's own
    yield (ADR 107 D4) — so a live missile-evade maneuver steps aside for
    Climb's emergency verdict rather than ignoring it outright.
    """
    def missile_evade(snapshot: AnalyzerSnapshot) -> bool:
        if yields_to_fn is not None and yields_to_fn():
            return False
        if snapshot.incoming_detected:
            return True
        return is_running_fn is not None and is_running_fn()
    return missile_evade


def make_idle_condition(yields_to_fn=None):
    """``is_idle`` (module function) plus one narrow escape hatch.

    Operator-caught live: "it flew forward horizontal after respawn
    without changing altitude." Idle is first in ``_PRIORITY_ORDER`` and
    had no way to cede the airframe to anything — including a live Climb
    emergency — for the entire time the FSM sits outside GAME_BATTLE.
    Traced: an ``eject_and_dive`` sequence aborted mid-dive (telemetry
    showed the aircraft alive and flying, not actually dead) and handed
    control back while ``game_state`` was still GAME_BATTLE_EJECT, not yet
    GAME_BATTLE. Climb's own emergency verdict fired and logged correctly
    — but ``selected=Idle`` won the very same tick regardless, and Idle
    presses nothing, so the aircraft kept flying whatever heading it
    already had.

    Fixed narrowly: steps aside only when ``game_state ==
    GAME_BATTLE_EJECT`` *and* ``yields_to_fn()`` (Climb's emergency
    verdict) is true — GAME_LOBBY/GAME_STARTING (no aircraft exists to fly
    at all) are untouched, and ordinary tactics get no new access to
    GAME_BATTLE_EJECT either, only Climb's emergency does. No separate "is
    Eject actually still active" check is needed: TACTIC_EJECT already
    outranks TACTIC_CLIMB in ``_PRIORITY_ORDER``, so a genuinely still-true
    Eject condition wins before the selector ever reaches Climb — this
    only matters for the gap where Eject's own condition has already gone
    false but ``game_state`` hasn't caught up.
    """
    def idle(snapshot: AnalyzerSnapshot) -> bool:
        if snapshot.game_state == GameState.GAME_BATTLE_EJECT:
            if yields_to_fn is not None and yields_to_fn():
                return False
        return snapshot.game_state != GameState.GAME_BATTLE
    return idle


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


# Fresh altitude samples required after a respawn before the time-to-ground
# trigger is trusted again (ADR 086 d2). Sessions start already settled — only
# an observed respawn imposes the wait.
_SETTLED = 2


class BoundaryCondition:
    """ADR 107: true while the arena edge is ahead and not yet receding.

    ADR 139 D3: this used to be a closure (``make_boundary_condition``)
    capturing a mutable ``state`` dict — the hysteresis latch, closest
    approach, and blind-tick count were invisible to anything outside the
    closure. Promoted to a plain class (not a ``py_trees.behaviour.Behaviour``
    subclass: this object is called directly as ``cond(snapshot) -> bool``,
    never ticked as a composited tree node, so subclassing would add an
    unused inheritance chain without making anything visible to py-trees'
    own tooling) so ``active``/``min_dist``/``blind`` are inspectable named
    attributes instead of dict keys hidden inside a closure. Logic is
    byte-for-byte identical to the closure it replaces; only ``state["x"]``
    became ``self._x``.

    ENTRY needs a positive ``boundary_forward`` — otherwise any pass within the
    band would turn the aircraft. The HOLD does not, and that distinction is
    measured rather than assumed: over 32 crossing traces on 2026-09-01 the sign
    of ``forward`` flipped between adjacent ticks 27% of the time and read <= 0
    on 51% of the ticks where the aircraft was demonstrably closing. Releasing on
    it made the turn chatter and the heading never moved.

    ``dist`` is the trustworthy channel, so the turn holds until the aircraft is
    RECEDING — range risen ``recede_frac`` above the closest approach OF THIS
    TURN, not above where it started, so an arc that dips and then opens out is
    recognised as working.

    **Recession alone is not enough to let go.** It answers "is the turn
    working", and the first version wrongly used it to answer "are we safe now".
    Measured 2026-09-03 over 105 turns: the median release was 0.34R, 40%
    released inside 0.30R and 27% inside 0.20R — one logged 0.02R to 0.06R,
    which satisfies a 0.06 margin while handing back an aircraft still sitting
    on the edge, free to drift straight back over. Nine of that session's twelve
    crossings happened with the turn running.

    So the release is a hysteresis band, as Climb's altitude band already is:
    let go at ``release_frac`` (above ``turn_frac``, so the leaf cannot chatter
    on the entry threshold), or on recession but only once ``min_clear_frac``
    away. Below that the aircraft is still on the edge whatever the trend says.

    ``yields_to_fn`` lets a higher-priority condition win without reordering the
    selector: ADR 107 D4 uses it for Climb's emergency altitude band, because
    hitting the ground is certain while the boundary is a countdown.

    **Deliberately NOT sticky while the actuation runs.** MissileEvade and Climb
    hold their selection that way because they run to a goal of their own — a
    missile cleared, an altitude reached. This tactic has no goal but the
    reading: the condition IS the closed loop, so making it sticky means the loop
    can never open. It shipped sticky on 2026-09-03 and every one of the nine
    turns that session burned the full 12 s cap, four of them back to back on one
    approach, while the range oscillated 0.216R to 0.514R — clearing the release
    margin repeatedly and never being allowed to act on it.

    A ``turn_frac`` of None or 0 disables the leaf (the Evade precedent), and a
    missing reading never selects — blindness is not an emergency.
    """

    def __init__(self, turn_frac: "float | None",
                recede_frac: float = 0.06,
                yields_to_fn=None,
                release_frac: "float | None" = None,
                min_clear_frac: float = 0.35,
                blind_ticks: int = 3,
                entry_ratio: float = 0.25):
        self._turn_frac = turn_frac
        self._recede_frac = recede_frac
        self._yields_to_fn = yields_to_fn
        self._release_frac = release_frac
        self._min_clear_frac = min_clear_frac
        self._blind_ticks = blind_ticks
        self._entry_ratio = entry_ratio
        self._active = False
        self._min_dist: "float | None" = None
        self._blind = 0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def min_dist(self) -> "float | None":
        return self._min_dist

    @property
    def blind(self) -> int:
        return self._blind

    def _reset(self):
        self._active = False
        self._min_dist = None
        self._blind = 0

    def __call__(self, snapshot) -> bool:
        if not self._turn_frac:
            return False
        # A respawn is a NEW aircraft. Any latched turn state describes where the
        # last one was and must not survive: on 2026-09-04 a turn held through a
        # respawn and re-selected one second after it, with the aircraft freshly
        # spawned and nowhere near an edge. An aircraft never spawns pointing at
        # the boundary, which makes a turn straight after a respawn a reliable
        # indicator that something upstream has latched.
        #
        # ADR 138: is_respawning alone left a gap. Measured live 2026-09-10
        # 03:03:26 — the respawn screen clears (is_respawning -> False) up to
        # ~1.5s BEFORE mission_j20 actually restarts (the ADR 059 respawn-clear
        # stability window), and THAT restart is what arms the ADR 132 turn
        # guard. This condition doesn't wait for mission_running, so it
        # selected and actuated a full 12s, 180-degree-swing turn starting
        # inside that gap — the exact "circling instead of joining the fight"
        # ADR 132 exists to prevent, just from a different timing hole.
        if getattr(snapshot, "is_respawning", False) or \
                snapshot.game_state != GameState.GAME_BATTLE or \
                not snapshot.mission_running:
            self._reset()
            return False
        if self._yields_to_fn is not None and self._yields_to_fn():
            self._reset()
            return False
        dist = getattr(snapshot, "boundary_dist", None)
        forward = getattr(snapshot, "boundary_forward", None)
        # ADR 120: the NEAREST recent reading, for deciding "am I clear".
        # Falls back to `dist` so a snapshot without it behaves as before.
        near = getattr(snapshot, "boundary_near", None)
        if near is None:
            near = dist
        if dist is None or forward is None:
            # Freeze rather than release — a dropped reading mid-turn is a gap
            # in perception, not evidence the aircraft is clear — but BOUNDED.
            # Unbounded it is a latch: the detector is blind ~70% of ticks, so
            # "hold until a reading disagrees" means "hold indefinitely", which
            # is exactly what carried a turn through the 2026-09-04 respawn.
            self._blind += 1
            if self._blind > self._blind_ticks:
                self._reset()
                return False
            return self._active
        self._blind = 0
        clear_at = self._release_frac if self._release_frac else self._turn_frac
        # ADR 120: ENTER on the filtered reading, LEAVE on the nearest one.
        #
        # The two errors do not cost the same. A spurious near reading buys one
        # unnecessary turn, and turns are cheap — 83 in a session, measured
        # break-even. A missed near reading buys a crossing, which is the metric.
        # So noise-reject where it is cheap to be wrong (entry) and be
        # conservative where it is not (release).
        #
        # Measured 2026-09-05: of 67 raw readings inside 0.10R with a fresh
        # predecessor, 58 (87%) arrived via a physically reachable jump — real
        # approaches — against 9 that could not be. Yet the median reported
        # 0.10R or more for 44% of raw readings inside 0.10R, including
        # 0.019 -> 0.516. A single reading above `clear_at` releases the turn
        # outright, so that path released turns with the aircraft at the edge.
        if (near if self._active else dist) >= (
                clear_at if self._active else self._turn_frac):
            # Entering uses turn_frac; leaving uses the wider release_frac, so
            # the two thresholds cannot sit on top of each other and flap.
            self._reset()
            return False
        if not self._active:
            # "Ahead" means ahead, not merely not-behind. `forward` is the
            # component of the range along the nose, so forward/dist is the
            # cosine of the bearing to the nearest boundary point: near 1 the
            # edge is dead ahead, near 0 it is abeam and the aircraft is flying
            # ALONG it, not at it.
            #
            # `forward > 0` alone accepted 0.006/0.281 = 0.02 on 2026-09-04 and
            # turned a freshly respawned aircraft — the operator's point that a
            # turn straight after a respawn cannot be real, since nothing spawns
            # pointed at an edge.
            #
            # Measured over 184 ticks preceding confirmed crossings, genuine
            # approaches run a median of 0.82 with a 10th percentile of 0.24. A
            # 0.25 gate drops about a tenth of those ticks, and an approach
            # produces many, so entry is delayed by a tick rather than missed.
            if forward <= 0 or forward < self._entry_ratio * dist:
                return False
            self._active = True
            self._min_dist = dist
            return True
        self._min_dist = min(self._min_dist, near)
        # Recession is also a clearance claim, so it reads the nearest value
        # too — otherwise a filtered reading can manufacture a recession the
        # aircraft never flew.
        if near >= self._min_clear_frac and near >= self._min_dist + self._recede_frac:
            self._reset()
            return False
        return True


def make_boundary_condition(turn_frac: "float | None",
                            recede_frac: float = 0.06,
                            yields_to_fn=None,
                            release_frac: "float | None" = None,
                            min_clear_frac: float = 0.35,
                            blind_ticks: int = 3,
                            entry_ratio: float = 0.25) -> BoundaryCondition:
    """ADR 139 D3: thin factory kept so every existing call site — production
    and test — is unchanged. See ``BoundaryCondition`` for the logic."""
    return BoundaryCondition(turn_frac, recede_frac, yields_to_fn=yields_to_fn,
                             release_frac=release_frac,
                             min_clear_frac=min_clear_frac,
                             blind_ticks=blind_ticks, entry_ratio=entry_ratio)


class ClimbCondition:
    """ADR 073: hysteresis band on the telemetry stable altitude.

    ADR 139 D3: this used to be a closure (``make_climb_condition``)
    capturing a mutable ``state`` dict, with ``emergency_active`` stapled on
    as a function attribute. Promoted to a plain class (not a
    ``py_trees.behaviour.Behaviour`` subclass — see ``BoundaryCondition`` for
    why) so the hysteresis state is inspectable via named properties. The
    ``emergency_active`` property is named to match exactly, since
    ``_climb_emergency_fn`` (``_build_climb_slot``) reads it via
    ``getattr(obj, "emergency_active", False)``, which works identically
    against an instance property or the old function attribute — that
    closure needs no change. Logic is byte-for-byte identical to the closure
    this replaces; only ``state["x"]`` became ``self._x``.

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

    def __init__(self, enter_below_alt: "float | None",
                exit_above_alt: "float | None",
                is_running_fn=None,
                confirm_reads: int = 1,
                recover_below_time_s: "float | None" = None,
                confirm_bypass_time_s: "float | None" = None,
                descent_memory_s: float = 5.0,
                clock=time.time,
                terrain_enabled: bool = False,
                terrain_shadow: bool = True,
                terrain_sky_min_frac: float = 0.55,
                terrain_confirm_reads: int = 2,
                alt_floor_m: "float | None" = None,
                alt_floor_override_fn: "Callable[[], float | None] | None" = None):
        self._enter_below_alt = enter_below_alt
        self._exit_above_alt = exit_above_alt
        self._is_running_fn = is_running_fn
        self._confirm_reads = confirm_reads
        self._recover_below_time_s = recover_below_time_s
        self._confirm_bypass_time_s = confirm_bypass_time_s
        self._descent_memory_s = descent_memory_s
        self._clock = clock
        self._active = False
        self._streak = 0
        self._ttg_streak = 0
        self._last_ttg: "float | None" = None
        self._last_ttg_ts = 0.0
        self._post_respawn = _SETTLED
        self._emergency_active = False
        self._pending_reevaluation = False
        # ADR 143: last time hard_emergency_active (below) went True — 0.0
        # if it never has. Lets a consumer ask "was a genuine dive/terrain
        # emergency active recently", not just "is it active this instant",
        # to classify a died-armed death as a likely terrain crash.
        self._last_hard_emergency_active_ts = 0.0
        # HLDD 001 Phase 1: forward sky-occlusion terrain-ahead trigger, a
        # second OR-term alongside the ttg emergency above. Own debounce
        # streak, same confirm-reads shape as ttg's, deliberately separate
        # from _active's edge-detection so a terrain onset logs even when
        # ttg already has Climb active for an unrelated reason.
        self._terrain_enabled = terrain_enabled
        self._terrain_shadow = terrain_shadow
        self._terrain_sky_min_frac = terrain_sky_min_frac
        self._terrain_confirm_reads = terrain_confirm_reads
        self._terrain_streak = 0
        self._terrain_ahead_active = False
        # Phase 1 (operator directive): a third, deliberately simple
        # emergency OR-term — a hard altitude floor, independent of rate.
        # No confirm-reads debounce beyond the single check: catches a
        # near-stall the ttg trigger's rate requirement cannot (a stalled
        # aircraft can show a near-zero or briefly positive rate while still
        # critically low).
        self._alt_floor_m = alt_floor_m
        # ADR 147: the floor in force for the mission in play, read every tick.
        # Returns None for "no mission-specific floor — use alt_floor_m".
        self._alt_floor_override_fn = alt_floor_override_fn
        self._alt_floor_active = False
        # ttg (dive recovery) and terrain-ahead are what "hitting the
        # ground is certain" (ADR 107 D4's own reasoning for BoundaryTurn's
        # yield) actually means — the altitude floor is a softer,
        # preventive backstop, not that. See `hard_emergency_active`.
        self._hard_emergency_active = False

    @property
    def active(self) -> bool:
        return self._active

    @property
    def emergency_active(self) -> bool:
        return self._emergency_active

    def _effective_alt_floor_m(self) -> "float | None":
        """The hard altitude floor in force this tick, or None for no floor.

        ADR 147: a mission may set its own floor (mission_su30 levels off at
        3000 m, below the tree's 4000 m, and the tree restarted the climb 1.3 s
        after the script stopped it). The override only REPLACES a floor that
        exists: ``alt_floor_m`` unset still means no floor for every mission.
        """
        if self._alt_floor_m is None:
            return None
        if self._alt_floor_override_fn is not None:
            override = self._alt_floor_override_fn()
            if override is not None:
                return float(override)
        return float(self._alt_floor_m)

    @property
    def terrain_ahead_active(self) -> bool:
        return self._terrain_ahead_active

    @property
    def alt_floor_active(self) -> bool:
        return self._alt_floor_active

    @property
    def hard_emergency_active(self) -> bool:
        """ttg or terrain only — excludes the altitude floor. What
        BoundaryTurn's yields_to_fn reads (operator directive); see the
        __init__ comment for why."""
        return self._hard_emergency_active

    @property
    def last_hard_emergency_active_ts(self) -> float:
        """ADR 143: 0.0 if never — see the __init__ comment."""
        return self._last_hard_emergency_active_ts

    @property
    def streak(self) -> int:
        return self._streak

    @property
    def ttg_streak(self) -> int:
        return self._ttg_streak

    @property
    def last_ttg(self) -> "float | None":
        return self._last_ttg

    @property
    def post_respawn(self) -> int:
        return self._post_respawn

    def _time_to_ground(self, snapshot, now):
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
            self._last_ttg, self._last_ttg_ts = ttg, now
            return ttg
        if (self._last_ttg is not None
                and now - self._last_ttg_ts <= self._descent_memory_s):
            return self._last_ttg
        return None

    def update_emergency(self, snapshot: AnalyzerSnapshot,
                         now: "float | None" = None) -> bool:
        """Refresh the ttg-based emergency verdict — Anomaly 007.

        Split out of ``__call__`` because py-trees' priority Selector never
        ticks a leaf a higher-priority sibling keeps beating: while
        BoundaryTurn (or anything else above Climb) wins every tick, this
        whole condition was simply never invoked, so ``emergency_active``
        (what ``BoundaryTurn``'s ``yields_to_fn`` reads — ADR 107 D4) sat
        frozen at whatever it was before that sibling took over. Live
        2026-09-14: `ttg` measured 6-8s (the emergency threshold is 30s) for
        9+ continuous seconds while BoundaryTurn stayed selected and never
        yielded — the operator had to intervene manually at ~470m still
        descending. The fix: call this unconditionally, every tick, from
        ``BehaviorTreeHandler.tick()`` BEFORE the tree is ticked — the same
        "perceive before select" pattern ``BoundaryPerceptionHandler``
        already uses — so the emergency verdict is always current regardless
        of what wins the selector. ``__call__`` no longer recomputes this
        itself when this has already run for the current tick (tracked by
        ``_pending_reevaluation``); it still does when nothing called this
        first, so every existing direct-call site (all of
        ``TestTimeToGroundRecovery``) is unaffected.
        """
        if self._enter_below_alt is None or self._exit_above_alt is None:
            return False   # ADR 073: disabled until calibrated

        # ADR 086 d2: the emergency trigger is predicted TIME to ground, not
        # altitude. On 2026-08-21 18:41 the aircraft dived 9203 m -> 2301 m in
        # 27 s with 2 missiles aboard while the tree kept selecting Engage:
        # the altitude band never opened because the smoothed altitude lags
        # ~1500 m in a 560 m/s dive and the aircraft hit the ground first.
        if now is None:
            now = self._clock()

        # ADR 086 d2: a respawn is an altitude DISCONTINUITY, not a descent.
        # The smoothed value carries the dead aircraft's numbers across it, so
        # the first post-respawn samples describe a fall that already ended.
        # Observed 2026-08-21 21:28:49 — fired "2s to ground" at a smoothed
        # 324m while the new aircraft was at 10m and climbing away at +513m/s.
        if snapshot.is_respawning:
            self._post_respawn = 0
            self._last_ttg = None
            self._ttg_streak = 0
        elif snapshot.altitude is not None:
            self._post_respawn += 1

        ttg = self._time_to_ground(snapshot, now)
        emergency = False
        # Two clean samples since the respawn before the emergency is trusted:
        # enough to establish a rate that describes the LIVING aircraft.
        settled = self._post_respawn >= _SETTLED
        if settled and self._recover_below_time_s is not None and ttg is not None \
                and ttg < float(self._recover_below_time_s):
            if (self._confirm_bypass_time_s is not None
                    and ttg < float(self._confirm_bypass_time_s)):
                # ADR 086 d3: inside the bypass window, waiting for a second
                # read spends the very margin the trigger exists to protect.
                emergency = True
            else:
                self._ttg_streak += 1
                emergency = self._ttg_streak >= max(1, int(self._confirm_reads))
            if emergency and not self._active:
                logger.warning(
                    "BT: DIVE RECOVERY — %.0fs to ground (alt=%s rate=%s) — "
                    "climb forced (ADR 086 d2)",
                    ttg,
                    "n/a" if snapshot.altitude is None else f"{snapshot.altitude:.0f}m",
                    "held" if getattr(snapshot, "altitude_rate", None) is None
                    else f"{snapshot.altitude_rate:+.0f}m/s")
        else:
            self._ttg_streak = 0

        # HLDD 001 Phase 1: forward sky-occlusion, same confirm-reads
        # debounce shape as the ttg trigger above. `terrain_sky_frac` is a
        # raw per-tick measurement (analyzer.detect_terrain_ahead) carried
        # on the snapshot exactly like boundary_dist/altitude — the streak
        # and threshold live here, not in the perception layer, matching
        # where every other emergency debounce in this class already lives.
        terrain_ahead = False
        if self._terrain_enabled:
            sky_frac = getattr(snapshot, "terrain_sky_frac", None)
            if sky_frac is not None and sky_frac < self._terrain_sky_min_frac:
                self._terrain_streak += 1
            else:
                self._terrain_streak = 0
            terrain_ahead = self._terrain_streak >= max(1, int(self._terrain_confirm_reads))
            if terrain_ahead and not self._terrain_ahead_active:
                logger.warning(
                    "BT: TERRAIN AHEAD — sky fraction %.2f below %.2f "
                    "threshold — climb forced (HLDD 001 phase 1)%s",
                    sky_frac if sky_frac is not None else -1.0,
                    self._terrain_sky_min_frac,
                    " [SHADOW - not actuating]" if self._terrain_shadow else "")
            self._terrain_ahead_active = terrain_ahead
            if not self._terrain_shadow:
                emergency = emergency or terrain_ahead

        # ttg or terrain only, captured BEFORE the altitude floor below is
        # folded in — see hard_emergency_active's docstring.
        self._hard_emergency_active = bool(emergency)
        if self._hard_emergency_active:
            # ADR 143: deliberately the hard signal, not the broader
            # emergency_active below — "hitting the ground is certain" is
            # the terrain-crash evidence this exists for; the softer
            # altitude-floor case (folded into emergency_active only) is a
            # preventive backstop, not that (see hard_emergency_active's
            # own docstring).
            self._last_hard_emergency_active_ts = now

        # Phase 1 (operator directive): hard altitude floor, no rate
        # involved. Deliberately the simplest possible check — snapshot.
        # altitude below the floor is an emergency, full stop, regardless
        # of whether the aircraft is diving, level, or even climbing too
        # slowly. This is what catches the near-stall case the other two
        # triggers cannot: ttg needs a negative rate to compute anything,
        # and a stalled aircraft can show a near-zero or even briefly
        # positive rate while still critically low and unable to climb
        # away in time.
        # Live 2026-09-20: a single blind tick (snapshot.altitude is None —
        # an ordinary OCR gap, not a respawn) reset `_alt_floor_active` to
        # False even while the aircraft was still genuinely below the
        # floor, because the old code re-derived it fresh (`alt_floor =
        # False`) every call instead of freezing across a missing reading —
        # the ONE trigger in this class that didn't already follow its own
        # documented "altitude is None FREEZES the decision" policy (see
        # the class docstring; ttg and terrain both already freeze/hold
        # their own state on a gap). Cosmetic only — confirmed by direct
        # trace that `_active`'s own selection latch does not depend on
        # this flag — but it produced a spurious repeat "ALTITUDE FLOOR"
        # WARNING on every gap-then-reading cycle instead of once per
        # genuine crossing. Fixed: only ever write `_alt_floor_active` when
        # this tick actually has an altitude to judge.
        floor_m = self._effective_alt_floor_m()
        if floor_m is None:
            self._alt_floor_active = False
        elif snapshot.altitude is not None:
            alt_floor = snapshot.altitude < floor_m
            if alt_floor and not self._alt_floor_active:
                logger.warning(
                    "BT: ALTITUDE FLOOR — %.0fm below %.0fm — climb forced "
                    "(operator directive)",
                    snapshot.altitude, floor_m)
            self._alt_floor_active = alt_floor
        emergency = emergency or self._alt_floor_active

        self._emergency_active = bool(emergency)
        self._pending_reevaluation = True
        return self._emergency_active

    def __call__(self, snapshot: AnalyzerSnapshot) -> bool:
        if self._enter_below_alt is None or self._exit_above_alt is None:
            return False   # ADR 073: disabled until calibrated

        if not self._pending_reevaluation:
            # Nobody called update_emergency for this tick — a direct/test
            # call site, or a real tick where the pre-tick hook didn't run.
            # Compute it inline, exactly as the pre-split __call__ always did.
            self.update_emergency(snapshot)
        self._pending_reevaluation = False
        emergency = self._emergency_active

        alt = snapshot.altitude
        # ADR 107 D4: BoundaryTurn outranks the ordinary altitude-recovery climb
        # but not this. Published as a property rather than reordering the
        # selector, because the emergency is a MODE of the climb condition, not
        # a separate leaf — splitting it would duplicate the hysteresis state
        # that decides it.
        if emergency:
            # The band must not release a recovery it did not start: in this
            # dive the altitude was far ABOVE exit_above_alt the whole way
            # down, so the ordinary hysteresis would have cleared it instantly.
            self._active = True
            self._streak = 0
        elif alt is not None:
            if self._active:
                crossing = alt >= self._exit_above_alt
            else:
                crossing = alt < self._enter_below_alt
            if crossing:
                self._streak += 1
                if self._streak >= max(1, int(self._confirm_reads)):
                    self._active = not self._active
                    self._streak = 0
            else:
                self._streak = 0
        return self._active or (self._is_running_fn is not None and self._is_running_fn())


def make_climb_condition(enter_below_alt: "float | None",
                         exit_above_alt: "float | None",
                         is_running_fn=None,
                         confirm_reads: int = 1,
                         recover_below_time_s: "float | None" = None,
                         confirm_bypass_time_s: "float | None" = None,
                         descent_memory_s: float = 5.0,
                         clock=time.time,
                         terrain_enabled: bool = False,
                         terrain_shadow: bool = True,
                         terrain_sky_min_frac: float = 0.55,
                         terrain_confirm_reads: int = 2,
                         alt_floor_m: "float | None" = None,
                         alt_floor_override_fn: "Callable[[], float | None] | None" = None
                         ) -> ClimbCondition:
    """ADR 139 D3: thin factory kept so every existing call site — production
    and test — is unchanged. See ``ClimbCondition`` for the logic."""
    return ClimbCondition(enter_below_alt, exit_above_alt,
                          is_running_fn=is_running_fn,
                          confirm_reads=confirm_reads,
                          recover_below_time_s=recover_below_time_s,
                          confirm_bypass_time_s=confirm_bypass_time_s,
                          descent_memory_s=descent_memory_s, clock=clock,
                          terrain_enabled=terrain_enabled,
                          terrain_shadow=terrain_shadow,
                          terrain_sky_min_frac=terrain_sky_min_frac,
                          terrain_confirm_reads=terrain_confirm_reads,
                          alt_floor_m=alt_floor_m,
                          alt_floor_override_fn=alt_floor_override_fn)


def make_sustain_climb_condition(enter_below_alt: "float | None",
                                 exit_above_alt: "float | None",
                                 confirm_reads: int = 1,
                                 suppressed_fn: "Callable[[], bool] | None" = None):
    """ADR 075: climb-while-armed altitude sustain band.

    The adaptive J20 doctrine: as long as the aircraft has missiles and a
    mission is running, it works its way up to the operating altitude while
    search-and-destroy runs. Same hysteresis + confirm-reads debounce as the
    ADR 073 emergency band (delegated to ``make_climb_condition``), gated on:

    - ``missiles`` > 0 — an empty aircraft belongs to the Eject leaf, and an
      unreadable count must not command a climb;
    - ``mission_running`` — sustain is mission doctrine, unlike the emergency
      band which fires regardless (terrain outranks everything).

    ``suppressed_fn`` (ADR 147): true while the mission in play flies its own
    altitude and is not the adaptive doctrine (mission_su30). The band is still
    evaluated so its hysteresis state tracks the altitude as it always does; only
    the verdict is withheld.

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
        climbing = band(snapshot)
        if suppressed_fn is not None and suppressed_fn():
            return False
        return climbing
    return sustain


def has_contacts(snapshot: AnalyzerSnapshot) -> bool:
    return snapshot.contacts > 0


def has_friendlies(snapshot: AnalyzerSnapshot) -> bool:
    """Friendly or objective icons visible with no enemy on the minimap.

    ADR 028 revision 4. The enemy check is not redundant: Engage sits above
    Regroup in the selector and would win anyway, but making the exclusion
    explicit keeps the condition true to its name if the order is ever
    changed.
    """
    return snapshot.contacts == 0 and snapshot.friendly_contacts > 0


def always(_snapshot: AnalyzerSnapshot) -> bool:
    return True


# ADR 139 D1: declared priority order — top wins. Replaces the earlier
# imperative `children.insert(pos, leaf)` calls, which found `pos` by name
# specifically because an offset-based version (`len(children) - 2`) once
# broke silently: it meant "above Engage" only while exactly two leaves
# followed it, and adding Regroup pushed Climb below Engage.
_PRIORITY_ORDER = (
    TACTIC_IDLE, TACTIC_RESPAWN_WAIT, TACTIC_EJECT, TACTIC_MISSILE_EVADE,
    TACTIC_BOUNDARY_TURN, TACTIC_EVADE, TACTIC_DISENGAGE, TACTIC_CLIMB,
    TACTIC_ENGAGE, TACTIC_REGROUP, TACTIC_ATTACK_SUPPORT,
)


@dataclass
class _BuildContext:
    """Threaded through the slot-build functions (ADR 139 D1).

    ``climb_emergency_fn`` is set by ``_build_climb_slot`` and read by
    ``_build_boundary_slot`` — a real cross-slot dependency that does NOT
    follow priority order (BoundaryTurn outranks Climb in ``_PRIORITY_ORDER``
    but reads Climb's emergency closure), so slot BUILD order is declared
    separately from priority order, in ``_build_slots`` below.
    """

    bt_cfg: dict
    clock: "Callable[[], float]"
    actuators: dict
    regroup_enabled: bool
    climb_emergency_fn: "Callable[[], bool] | None" = None
    climb_emergency_update_fn: "Callable[[AnalyzerSnapshot, float | None], bool] | None" = None
    climb_terrain_ahead_fn: "Callable[[], bool] | None" = None
    # Phase 1 (operator directive): the narrower "ttg or terrain only"
    # signal — distinct from climb_emergency_fn above (which MissileEvade
    # and Idle read; the operator's own directive wants both to respect the
    # altitude floor too). Only BoundaryTurn reads this one. See
    # _build_boundary_slot's docstring for why.
    climb_hard_emergency_fn: "Callable[[], bool] | None" = None
    # ADR 143: RespawnHandler reads this to classify a died-armed death as a
    # likely terrain crash — was the hard emergency active recently, not
    # just this instant (a death detected a tick or two after the dive
    # itself would otherwise always read False here).
    climb_last_hard_emergency_ts_fn: "Callable[[], float] | None" = None
    # ADR 147: the mission in play's own altitude doctrine, read by the Climb
    # slot. Both default to None — the tree's configured floor and sustain band
    # apply unchanged — so every mission but mission_su30 is untouched.
    alt_floor_override_fn: "Callable[[], float | None] | None" = None
    sustain_suppressed_fn: "Callable[[], bool] | None" = None


def climb_tactic_enabled(bt_cfg: dict) -> bool:
    """ADR 139 D2: the single predicate deciding whether Climb is in the
    tree at all AND whether its actuator gets wired — shared between
    ``_build_climb_slot`` (this module) and ``BehaviorTreeHandler.__init__``
    (``tick_handlers.py``), which previously wrote this check twice."""
    climb_cfg = bt_cfg.get("climb", {}) or {}
    return bool(climb_cfg.get("enabled", False))


def boundary_tactic_enabled(bt_cfg: dict) -> bool:
    """ADR 139 D2: the single predicate deciding whether BoundaryTurn is in
    the tree at all AND whether its actuator gets wired — shared between
    ``_build_boundary_slot`` (this module) and ``BehaviorTreeHandler.__init__``
    (``tick_handlers.py``), which previously wrote this check twice."""
    boundary_cfg = bt_cfg.get("boundary", {}) or {}
    return bool(boundary_cfg.get("turn_frac"))


def _build_idle_slot(ctx: "_BuildContext"):
    """Reads ``ctx.climb_emergency_fn`` — built by ``_build_climb_slot``,
    which ``_build_slots`` therefore builds before this one, same
    build-order dependency ``_build_boundary_slot`` already has."""
    return ConditionTactic(
        TACTIC_IDLE, make_idle_condition(yields_to_fn=ctx.climb_emergency_fn))


def _build_respawn_wait_slot(_ctx: "_BuildContext"):
    return ConditionTactic(TACTIC_RESPAWN_WAIT, is_respawning)


def _build_eject_slot(ctx: "_BuildContext"):
    eject_fns = ctx.actuators.get(TACTIC_EJECT)
    if eject_fns is not None:
        return ConditionTactic(TACTIC_EJECT, is_eject_confirmed,
                               start_fn=eject_fns[0],
                               is_running_fn=eject_fns[1])
    return ConditionTactic(TACTIC_EJECT, is_missiles_empty)


def _build_missile_evade_slot(ctx: "_BuildContext"):
    # ADR 070: the is_running_fn feeds BOTH the actuation gate and the
    # condition's stickiness — the selection must not fall through to Engage
    # while the evade thread still owns the roll axis.
    #
    # Phase 1: reads ``ctx.climb_emergency_fn`` — built by
    # ``_build_climb_slot``, which ``_build_slots`` therefore builds before
    # this one.
    missile_evade_fns = ctx.actuators.get(TACTIC_MISSILE_EVADE)
    if missile_evade_fns is not None:
        return ConditionTactic(
            TACTIC_MISSILE_EVADE,
            make_missile_evade_condition(
                missile_evade_fns[1], yields_to_fn=ctx.climb_emergency_fn),
            start_fn=missile_evade_fns[0],
            is_running_fn=missile_evade_fns[1])
    return ConditionTactic(
        TACTIC_MISSILE_EVADE,
        make_missile_evade_condition(yields_to_fn=ctx.climb_emergency_fn))


def _build_evade_slot(ctx: "_BuildContext"):
    evade_threshold = ctx.bt_cfg.get("evade_health_threshold")
    evade_hold_s = float(ctx.bt_cfg.get("evade_hold_s", 10.0))
    return MinimumHold(
        TACTIC_EVADE,
        ConditionTactic(f"{TACTIC_EVADE}Condition",
                        make_evade_condition(evade_threshold)),
        hold_s=evade_hold_s, clock=ctx.clock,
    )


def _build_disengage_slot(ctx: "_BuildContext"):
    disengage_after_s = float(ctx.bt_cfg.get("disengage_after_s", 30.0))
    disengage_hold_s = float(ctx.bt_cfg.get("disengage_hold_s", 10.0))
    disengage_fns = ctx.actuators.get(TACTIC_DISENGAGE)
    disengage_kwargs = {}
    if disengage_fns is not None:
        disengage_kwargs = {"start_fn": disengage_fns[0],
                            "is_running_fn": disengage_fns[1]}
    return MinimumHold(
        TACTIC_DISENGAGE,
        ConditionTactic(f"{TACTIC_DISENGAGE}Condition",
                        make_disengage_condition(disengage_after_s),
                        **disengage_kwargs),
        hold_s=disengage_hold_s, clock=ctx.clock,
    )


def _build_engage_slot(_ctx: "_BuildContext"):
    return ConditionTactic(TACTIC_ENGAGE, has_contacts)


def _build_attack_support_slot(_ctx: "_BuildContext"):
    return ConditionTactic(TACTIC_ATTACK_SUPPORT, always)


def _build_climb_slot(ctx: "_BuildContext"):
    """ADR 073: absent entirely unless enabled. A selection-only leaf here
    would not be shadow — every selection would pre-empt Engage actuation and
    silently pause geometry at low altitude. While disabled,
    BehaviorTreeHandler logs would-select from an independent condition
    instance instead. Sets ``ctx.climb_emergency_fn`` as a side effect,
    consumed by ``_build_boundary_slot``.
    """
    if not climb_tactic_enabled(ctx.bt_cfg):
        return None
    climb_cfg = ctx.bt_cfg.get("climb", {}) or {}
    climb_fns = ctx.actuators.get(TACTIC_CLIMB)
    climb_kwargs = {}
    if climb_fns is not None:
        climb_kwargs = {"start_fn": climb_fns[0], "is_running_fn": climb_fns[1]}
        # ADR 137 D9: optional third element wires the RUNNING-tick update
        # channel — absent (2-tuple) leaves the leaf without one, unchanged
        # from before D9.
        if len(climb_fns) > 2 and climb_fns[2] is not None:
            climb_kwargs["update_fn"] = climb_fns[2]
    # HLDD 001 Phase 1: forward sky-occlusion terrain-ahead trigger. Lives
    # under climb (not its own top-level bt_cfg sibling) because it feeds
    # ClimbCondition directly, same relationship recover_below_time_s has.
    # The HSV/crop half of this feature lives in analyzer.py's top-level
    # `terrain_avoidance:` block instead (mirrors minimap.boundary_hsv vs
    # behavior_tree.boundary — detection config near the detector, trigger
    # config near the condition it feeds).
    _terrain_cfg = climb_cfg.get("terrain_avoidance", {}) or {}
    emergency = make_climb_condition(
        climb_cfg.get("enter_below_alt"),
        climb_cfg.get("exit_above_alt"),
        is_running_fn=climb_fns[1] if climb_fns is not None else None,
        confirm_reads=int(climb_cfg.get("confirm_reads", 1)),
        # ADR 086 d2/d3/d4 — time-to-ground recovery. Unset disables it and
        # leaves the pure ADR 073 altitude band.
        recover_below_time_s=climb_cfg.get("recover_below_time_s"),
        confirm_bypass_time_s=climb_cfg.get("confirm_bypass_time_s"),
        descent_memory_s=float(climb_cfg.get("descent_memory_s", 5.0)),
        terrain_enabled=bool(_terrain_cfg.get("enabled", False)),
        terrain_shadow=bool(_terrain_cfg.get("shadow", True)),
        terrain_sky_min_frac=float(_terrain_cfg.get("sky_min_frac", 0.55)),
        terrain_confirm_reads=int(_terrain_cfg.get("confirm_reads", 2)),
        alt_floor_m=climb_cfg.get("alt_floor_m"),
        alt_floor_override_fn=ctx.alt_floor_override_fn)
    # ADR 075: the armed altitude-sustain band shares the leaf with the
    # emergency band. Both closures are evaluated EVERY tick (no
    # short-circuit) so neither hysteresis state machine goes stale while
    # the other holds the selection.
    sustain_cfg = climb_cfg.get("sustain", {}) or {}
    if bool(sustain_cfg.get("enabled", False)):
        sustain = make_sustain_climb_condition(
            sustain_cfg.get("enter_below_alt"),
            sustain_cfg.get("exit_above_alt"),
            confirm_reads=int(climb_cfg.get("confirm_reads", 1)),
            suppressed_fn=ctx.sustain_suppressed_fn)

        def climb_condition(snapshot, _e=emergency, _s=sustain):
            e = _e(snapshot)
            s = _s(snapshot)
            return e or s
    else:
        climb_condition = emergency
    climb_leaf = ConditionTactic(TACTIC_CLIMB, climb_condition, **climb_kwargs)

    # ADR 107 D4, corrected by Anomaly 007: reading this lazily only sees the
    # CURRENT tick's verdict if something refreshes it every tick regardless
    # of selection — py-trees never ticks Climb's own condition while a
    # higher-priority sibling keeps winning, so without the update_fn below
    # this flag went stale the entire time BoundaryTurn was selected. See
    # ClimbCondition.update_emergency for the incident and the fix.
    def _climb_emergency_fn(_e=emergency):
        return bool(getattr(_e, "emergency_active", False))
    ctx.climb_emergency_fn = _climb_emergency_fn

    # Phase 1: the narrower "ttg or terrain only" signal — see
    # _BuildContext.climb_hard_emergency_fn.
    def _climb_hard_emergency_fn(_e=emergency):
        return bool(getattr(_e, "hard_emergency_active", False))
    ctx.climb_hard_emergency_fn = _climb_hard_emergency_fn

    # ADR 143: exposed the same way — a property read, not a re-derivation.
    def _climb_last_hard_emergency_ts_fn(_e=emergency):
        return float(getattr(_e, "last_hard_emergency_active_ts", 0.0))
    ctx.climb_last_hard_emergency_ts_fn = _climb_last_hard_emergency_ts_fn

    def _climb_emergency_update_fn(snapshot, now=None, _e=emergency):
        return _e.update_emergency(snapshot, now)
    ctx.climb_emergency_update_fn = _climb_emergency_update_fn

    # HLDD 001 Phase 1: exposed so BehaviorTreeHandler can capture evidence
    # on the FALSE→TRUE edge — same "read a named property, don't re-derive
    # it" relationship climb_emergency_fn already has to emergency_active.
    def _climb_terrain_ahead_fn(_e=emergency):
        return bool(getattr(_e, "terrain_ahead_active", False))
    ctx.climb_terrain_ahead_fn = _climb_terrain_ahead_fn
    return climb_leaf


def _build_boundary_slot(ctx: "_BuildContext"):
    """ADR 107: absent unless configured. Reads
    ``ctx.climb_hard_emergency_fn`` — built by ``_build_climb_slot``, which
    ``_build_slots`` therefore always runs first regardless of priority
    order, since BoundaryTurn outranks Climb but depends on it.

    Phase 1: deliberately the HARD-only signal (ttg or terrain), not the
    broader ``climb_emergency_fn`` MissileEvade and Idle read. ADR 107 D4's
    own reasoning for this yield — "hitting the ground is certain while the
    boundary is a countdown" — is true of ttg and terrain, not of the
    altitude floor, which is a softer, preventive backstop, not a
    ground-impact-imminent signal. A floor-only emergency that stays true
    for an extended climb (recovering from a genuinely low respawn, say)
    must not lock BoundaryTurn out of the map edge for the whole climb —
    BoundaryTurn now only yields to the former.
    """
    if not boundary_tactic_enabled(ctx.bt_cfg):
        return None
    boundary_cfg = ctx.bt_cfg.get("boundary", {}) or {}
    boundary_fns = ctx.actuators.get(TACTIC_BOUNDARY_TURN)
    boundary_kwargs = {}
    if boundary_fns is not None:
        boundary_kwargs = {"start_fn": boundary_fns[0],
                           "is_running_fn": boundary_fns[1]}
    return MinimumHold(
        TACTIC_BOUNDARY_TURN,
        ConditionTactic(
            f"{TACTIC_BOUNDARY_TURN}Condition",
            make_boundary_condition(
                float(boundary_cfg["turn_frac"]),
                float(boundary_cfg.get("recede_frac", 0.06)),
                yields_to_fn=ctx.climb_hard_emergency_fn,
                release_frac=boundary_cfg.get("release_frac"),
                min_clear_frac=float(
                    boundary_cfg.get("min_clear_frac", 0.35)),
                blind_ticks=int(boundary_cfg.get("blind_ticks", 3)),
                entry_ratio=float(boundary_cfg.get("entry_ratio", 0.25))),
            **boundary_kwargs),
        hold_s=float(boundary_cfg.get("hold_s", 3.0)), clock=ctx.clock,
    )


def _build_regroup_slot(ctx: "_BuildContext"):
    # ADR 028 revision 4. Absent unless enabled, so `minimap.regroup_enabled:
    # false` disables the FEATURE rather than half of it — gating the
    # navigator mode alone left the leaf still being selected (21 selections
    # in a nine-minute run with the flag off), which silently invalidates any
    # A/B comparison the flag is used for.
    #
    # Below Engage: a real target always outranks regrouping. Above
    # AttackSupport: that leaf is `always`, so anything below it is
    # unreachable — both encoded once, in `_PRIORITY_ORDER`, not here.
    if not ctx.regroup_enabled:
        return None
    return ConditionTactic(TACTIC_REGROUP, has_friendlies)


def _build_slots(ctx: "_BuildContext") -> dict:
    """Build every leaf, returning ``{name: Behaviour}`` for the present ones.

    Build order here is deliberately NOT ``_PRIORITY_ORDER`` — it only has to
    satisfy real data dependencies (Climb built before Boundary).
    ``build_tree`` assembles the final children list by walking
    ``_PRIORITY_ORDER`` and looking each name up here, so priority rank is
    declared once, in one place, independent of this function's build
    sequence.
    """
    built = {
        TACTIC_RESPAWN_WAIT: _build_respawn_wait_slot(ctx),
        TACTIC_EJECT: _build_eject_slot(ctx),
        TACTIC_EVADE: _build_evade_slot(ctx),
        TACTIC_DISENGAGE: _build_disengage_slot(ctx),
        TACTIC_ENGAGE: _build_engage_slot(ctx),
        TACTIC_ATTACK_SUPPORT: _build_attack_support_slot(ctx),
    }
    climb_leaf = _build_climb_slot(ctx)        # sets ctx.climb_emergency_fn,
                                                # ctx.climb_hard_emergency_fn
    if climb_leaf is not None:
        built[TACTIC_CLIMB] = climb_leaf
    boundary_leaf = _build_boundary_slot(ctx)  # reads ctx.climb_hard_emergency_fn
    if boundary_leaf is not None:
        built[TACTIC_BOUNDARY_TURN] = boundary_leaf
    # Phase 1: both read ctx.climb_emergency_fn, same build-order dependency
    # on _build_climb_slot as _build_boundary_slot above — moved here from
    # the dict literal so that dependency is real, not incidental.
    built[TACTIC_MISSILE_EVADE] = _build_missile_evade_slot(ctx)
    built[TACTIC_IDLE] = _build_idle_slot(ctx)
    regroup_leaf = _build_regroup_slot(ctx)
    if regroup_leaf is not None:
        built[TACTIC_REGROUP] = regroup_leaf
    return built


def build_tree(bt_cfg: dict, clock=time.time,
               actuators: "dict | None" = None,
               regroup_enabled: bool = False,
               alt_floor_override_fn: "Callable[[], float | None] | None" = None,
               sustain_suppressed_fn: "Callable[[], bool] | None" = None,
               ) -> py_trees.trees.BehaviourTree:
    """Construct the ADR 024 selector. Pure construction — no analyzer refs.

    ``actuators`` (Phase 3.1b) maps tactic name → ``(start_fn, is_running_fn)``
    for the leaves that actuate Controller tactics; absent entries stay
    selection-only. When the Eject leaf actuates, its condition switches from
    the raw missiles read to the debounced ``missiles_empty_confirmed``
    verdict — the shadow-session gate. Evade remains selection-only: no
    Controller tactic exists for it, and its threshold is unset until
    calibrated (ADR 024).

    ``alt_floor_override_fn`` / ``sustain_suppressed_fn`` (ADR 147) are the
    mission in play's own altitude doctrine: a replacement hard floor and
    whether the armed sustain band stands aside. Read every tick, so a mission
    launched after the tree was built takes effect at once.

    ADR 139 D1: children are assembled from the declared ``_PRIORITY_ORDER``
    rather than imperative ``list.insert(...)`` calls at named positions.
    """
    ctx = _BuildContext(bt_cfg=bt_cfg, clock=clock, actuators=actuators or {},
                        regroup_enabled=regroup_enabled,
                        alt_floor_override_fn=alt_floor_override_fn,
                        sustain_suppressed_fn=sustain_suppressed_fn)
    built = _build_slots(ctx)
    children = [built[name] for name in _PRIORITY_ORDER if name in built]

    root = py_trees.composites.Selector(
        name="TacticSelector",
        memory=False,
        children=children,
    )
    tree = py_trees.trees.BehaviourTree(root)
    # Exposed so the Climb actuator (tick_handlers.py's _start_climb) can read
    # THIS tick's ADR 086 emergency verdict without a new actuator-contract
    # parameter — same closure BoundaryTurn's yields_to_fn already reads.
    tree.climb_emergency_fn = ctx.climb_emergency_fn
    # Phase 1: the narrower ttg-or-terrain-only signal BoundaryTurn reads.
    tree.climb_hard_emergency_fn = ctx.climb_hard_emergency_fn
    # ADR 143: RespawnHandler's died-armed classifier reads this.
    tree.climb_last_hard_emergency_ts_fn = ctx.climb_last_hard_emergency_ts_fn
    # Anomaly 007: called once per tick, BEFORE tree.tick(), so the emergency
    # verdict above is never stale when a higher-priority tactic (chiefly
    # BoundaryTurn) is the one winning selection.
    tree.climb_emergency_update_fn = ctx.climb_emergency_update_fn
    # HLDD 001 Phase 1: exposed so BehaviorTreeHandler can save an evidence
    # frame on the FALSE->TRUE edge of the terrain-ahead trigger, whether or
    # not it is enabled/shadowed — the same reasoning as climb_emergency_fn
    # above, one property read instead of a new actuator-contract parameter.
    tree.climb_terrain_ahead_fn = ctx.climb_terrain_ahead_fn
    return tree


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


def tree_status_text(tree: py_trees.trees.BehaviourTree) -> str:
    """Per-node status dump of the last tick — Research 013's live-status
    view. `py_trees` persists `.status` on every node after a tick, so this
    needs no separate visitor/snapshot bookkeeping; it renders the same
    RUNNING/FAILURE/SUCCESS the priority selector just acted on, down through
    decorators (MinimumHold) to the condition leaf underneath, not just which
    top-level tactic won."""
    return py_trees.display.ascii_tree(tree.root, show_status=True)


def tree_status_dict(tree: py_trees.trees.BehaviourTree) -> dict[str, str]:
    """{node name: Status name} for every node — Design 012's structured
    sibling of `tree_status_text`, for a JSONL trace rather than a log
    line. `py_trees.behaviour.Behaviour.iterate()` walks root then every
    descendant, so this covers leaves nested under decorators too."""
    return {node.name: node.status.name for node in tree.root.iterate()}

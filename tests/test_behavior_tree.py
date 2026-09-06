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
    TACTIC_BOUNDARY_TURN,
    TACTIC_CLIMB,
    TACTIC_DISENGAGE,
    TACTIC_EJECT,
    TACTIC_ENGAGE,
    TACTIC_EVADE,
    TACTIC_IDLE,
    TACTIC_MISSILE_EVADE,
    TACTIC_RESPAWN_WAIT,
    build_tree,
    make_boundary_condition,
    make_climb_condition,
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


def make_actuated_harness(clock, eject=None, disengage=None, missile_evade=None):
    actuators = {}
    if eject is not None:
        actuators[TACTIC_EJECT] = (eject.start, eject.is_running)
    if disengage is not None:
        actuators[TACTIC_DISENGAGE] = (disengage.start, disengage.is_running)
    if missile_evade is not None:
        actuators[TACTIC_MISSILE_EVADE] = (missile_evade.start,
                                           missile_evade.is_running)
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


# ---------------------------------------------------------------------------
# ADR 070 — MissileEvade leaf
# ---------------------------------------------------------------------------

def test_missile_evade_beats_engage(harness):
    snap = make_snap(incoming_detected=True, ring_short=2, ring_mid=1)
    assert tick(harness, snap) == TACTIC_MISSILE_EVADE


def test_eject_beats_missile_evade(harness):
    """d1: eject_and_dive owns AFTERBURNER through its closed-loop descent —
    two owners on one key is the ADR 069 release-ordering fault."""
    snap = make_snap(missiles=0, incoming_detected=True)
    assert tick(harness, snap) == TACTIC_EJECT


def test_respawn_beats_missile_evade(harness):
    snap = make_snap(is_respawning=True, incoming_detected=True)
    assert tick(harness, snap) == TACTIC_RESPAWN_WAIT


def test_missile_evade_ignores_mission_running(harness):
    """d9: a missile is a threat with or without a mission thread."""
    snap = make_snap(incoming_detected=True, mission_running=False)
    assert tick(harness, snap) == TACTIC_MISSILE_EVADE


def test_missile_evade_not_selected_when_clear(harness):
    assert tick(harness, make_snap(ring_mid=1)) == TACTIC_ENGAGE


def test_actuated_missile_evade_sticky_while_running(clock):
    """The condition holds selection while the evade thread owns the keys —
    Engage must not re-select on the first clear tick and pulse the roll axis."""
    evade = _TacticRecorder()
    harness = make_actuated_harness(clock, missile_evade=evade)

    assert tick(harness, make_snap(incoming_detected=True, ring_mid=1)) == TACTIC_MISSILE_EVADE
    assert evade.starts == 1
    evade.running = True

    # Incoming clears but the hold is still live: selection stays, no re-fire.
    assert tick(harness, make_snap(incoming_detected=False, ring_mid=1)) == TACTIC_MISSILE_EVADE
    assert evade.starts == 1

    # Hold ends: selection falls through to Engage.
    evade.running = False
    assert tick(harness, make_snap(incoming_detected=False, ring_mid=1)) == TACTIC_ENGAGE
    assert evade.starts == 1


def test_actuated_missile_evade_does_not_restart_while_running(clock):
    evade = _TacticRecorder()
    harness = make_actuated_harness(clock, missile_evade=evade)
    tick(harness, make_snap(incoming_detected=True))
    assert evade.starts == 1
    evade.running = True
    tick(harness, make_snap(incoming_detected=True))
    assert evade.starts == 1  # is_running_fn gates the re-start


def test_selection_only_missile_evade_not_sticky(harness):
    """Without an actuator there is no running state to hold on — the leaf
    falls back to the bare incoming_detected predicate (shadow build)."""
    assert tick(harness, make_snap(incoming_detected=True)) == TACTIC_MISSILE_EVADE
    assert tick(harness, make_snap(incoming_detected=False, ring_mid=1)) == TACTIC_ENGAGE


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


# ---------------------------------------------------------------------------
# ADR 073 — Climb tactic


CLIMB_CFG = dict(BT_CFG, climb={
    "enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000})


@pytest.fixture
def climb_harness(clock):
    tree = build_tree(dict(CLIMB_CFG), clock=clock)
    writer = make_snapshot_writer()
    return tree, writer


def test_climb_absent_from_default_tree(harness):
    """With climb.enabled false (or missing) the leaf is not in the selector —
    a selection-only leaf would pre-empt Engage actuation, so shadow means
    absent (ADR 073)."""
    assert tick(harness, make_snap(altitude=100.0, ring_mid=1)) == TACTIC_ENGAGE
    assert tick(harness, make_snap(altitude=100.0)) == TACTIC_ATTACK_SUPPORT


def test_climb_selected_below_enter_threshold(climb_harness):
    assert tick(climb_harness, make_snap(altitude=499.0)) == TACTIC_CLIMB
    # Beats Engage even with contacts on every ring.
    snap = make_snap(altitude=499.0, ring_short=1, ring_mid=1, ring_long=1)
    assert tick(climb_harness, snap) == TACTIC_CLIMB


def test_climb_hysteresis_band(climb_harness):
    """Enter below 500; inside the band selection depends on prior state;
    release only at/above 1000."""
    assert tick(climb_harness, make_snap(altitude=700.0)) == TACTIC_ATTACK_SUPPORT
    assert tick(climb_harness, make_snap(altitude=499.0)) == TACTIC_CLIMB
    assert tick(climb_harness, make_snap(altitude=700.0)) == TACTIC_CLIMB    # still climbing
    assert tick(climb_harness, make_snap(altitude=999.0)) == TACTIC_CLIMB    # still inside band
    assert tick(climb_harness, make_snap(altitude=1000.0)) == TACTIC_ATTACK_SUPPORT
    assert tick(climb_harness, make_snap(altitude=700.0)) == TACTIC_ATTACK_SUPPORT


def test_climb_freezes_on_missing_altitude(climb_harness):
    """altitude=None neither enters nor releases (ADR 073): OCR dropouts must
    not flap selection in either direction."""
    assert tick(climb_harness, make_snap(altitude=None)) == TACTIC_ATTACK_SUPPORT
    assert tick(climb_harness, make_snap(altitude=400.0)) == TACTIC_CLIMB
    assert tick(climb_harness, make_snap(altitude=None)) == TACTIC_CLIMB     # frozen active
    assert tick(climb_harness, make_snap(altitude=1200.0)) == TACTIC_ATTACK_SUPPORT
    assert tick(climb_harness, make_snap(altitude=None)) == TACTIC_ATTACK_SUPPORT


def test_climb_yields_to_defensive_tactics(climb_harness):
    low = dict(altitude=100.0)
    assert tick(climb_harness, make_snap(is_respawning=True, **low)) == TACTIC_RESPAWN_WAIT
    assert tick(climb_harness, make_snap(missiles=0, **low)) == TACTIC_EJECT
    assert tick(climb_harness, make_snap(incoming_detected=True, **low)) == TACTIC_MISSILE_EVADE
    assert tick(climb_harness, make_snap(game_state=GameState.GAME_LOBBY, **low)) == TACTIC_IDLE


def test_climb_disabled_without_thresholds(clock):
    """enabled with unset thresholds = leaf present but inert (Evade
    precedent: disabled until calibrated)."""
    cfg = dict(BT_CFG, climb={"enabled": True})
    tree = build_tree(cfg, clock=clock)
    harness = (tree, make_snapshot_writer())
    assert tick(harness, make_snap(altitude=100.0)) == TACTIC_ATTACK_SUPPORT


def test_climb_condition_sticky_while_actuated():
    """is_running_fn keeps the condition true while the climb thread owns the
    pitch axis, exactly like the ADR 070 evade stickiness."""
    running = {"flag": True}
    cond = make_climb_condition(500, 1000, is_running_fn=lambda: running["flag"])
    assert cond(make_snap(altitude=2000.0)) is True          # sticky on running
    running["flag"] = False
    assert cond(make_snap(altitude=2000.0)) is False


class TestClimbDebounce:
    """ADR 073 3.2b: confirm_reads — band crossings need consecutive
    agreement in both directions (shadow sessions showed single garbage
    stable-values like alt=8 mid-flight)."""

    def _cond(self, confirm_reads=2):
        return make_climb_condition(500, 1000, confirm_reads=confirm_reads)

    def test_single_garbage_low_does_not_enter(self):
        cond = self._cond()
        assert cond(make_snap(altitude=8.0)) is False       # streak 1
        assert cond(make_snap(altitude=1500.0)) is False    # streak reset

    def test_two_consecutive_lows_enter(self):
        cond = self._cond()
        assert cond(make_snap(altitude=400.0)) is False
        assert cond(make_snap(altitude=420.0)) is True

    def test_none_neither_counts_nor_resets(self):
        cond = self._cond()
        assert cond(make_snap(altitude=400.0)) is False
        assert cond(make_snap(altitude=None)) is False      # freeze
        assert cond(make_snap(altitude=420.0)) is True      # streak survived

    def test_single_garbage_high_does_not_release(self):
        cond = self._cond()
        cond(make_snap(altitude=400.0))
        assert cond(make_snap(altitude=400.0)) is True      # active
        assert cond(make_snap(altitude=5000.0)) is True     # streak 1, still active
        assert cond(make_snap(altitude=400.0)) is True      # reset, still active
        assert cond(make_snap(altitude=1200.0)) is True     # streak 1
        assert cond(make_snap(altitude=1200.0)) is False    # released

    def test_default_confirm_reads_is_immediate(self):
        cond = make_climb_condition(500, 1000)
        assert cond(make_snap(altitude=400.0)) is True


# ---------------------------------------------------------------------------
# ADR 075: armed altitude-sustain climb band
# ---------------------------------------------------------------------------

SUSTAIN_CLIMB_CFG = dict(
    BT_CFG,
    climb={"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
           "confirm_reads": 1,
           "sustain": {"enabled": True, "enter_below_alt": 6000,
                       "exit_above_alt": 7000}},
)


@pytest.fixture
def sustain_harness(clock):
    tree = build_tree(dict(SUSTAIN_CLIMB_CFG), clock=clock)
    return tree, make_snapshot_writer()


def test_sustain_climb_selected_below_operating_alt_while_armed(sustain_harness):
    assert tick(sustain_harness, make_snap(altitude=5000.0)) == TACTIC_CLIMB


def test_sustain_climb_beats_engage(sustain_harness):
    """Armed and low → climb outranks engage geometry; S&D keeps firing from
    the mission loops, so altitude work costs no trigger time."""
    snap = make_snap(altitude=5000.0, ring_short=2)
    assert tick(sustain_harness, snap) == TACTIC_CLIMB


def test_sustain_requires_missiles(sustain_harness):
    assert tick(sustain_harness,
                make_snap(altitude=5000.0, missiles=None)) == TACTIC_ATTACK_SUPPORT
    assert tick(sustain_harness,
                make_snap(altitude=5000.0, missiles=0)) == TACTIC_EJECT


def test_sustain_requires_mission_running(sustain_harness):
    snap = make_snap(altitude=5000.0, mission_running=False)
    assert tick(sustain_harness, snap) == TACTIC_ATTACK_SUPPORT


def test_sustain_hysteresis_band(sustain_harness):
    # Between enter (6000) and exit (7000) without having entered: no climb.
    assert tick(sustain_harness, make_snap(altitude=6500.0)) == TACTIC_ATTACK_SUPPORT
    # Below enter: climb.
    assert tick(sustain_harness, make_snap(altitude=5900.0)) == TACTIC_CLIMB
    # Back inside the band: still climbing (hysteresis holds to exit alt).
    assert tick(sustain_harness, make_snap(altitude=6500.0)) == TACTIC_CLIMB
    # At/above exit: released.
    assert tick(sustain_harness, make_snap(altitude=7100.0)) == TACTIC_ATTACK_SUPPORT


def test_emergency_band_ignores_mission_and_missiles(sustain_harness):
    """Terrain avoidance fires regardless of the armed/mission gates that
    scope the sustain band (missiles empty is outranked by Eject, so test the
    unknown-missiles + no-mission case)."""
    snap = make_snap(altitude=300.0, mission_running=False, missiles=None)
    assert tick(sustain_harness, snap) == TACTIC_CLIMB


def test_incoming_beats_sustain_climb(sustain_harness):
    snap = make_snap(altitude=5000.0, incoming_detected=True)
    assert tick(sustain_harness, snap) == TACTIC_MISSILE_EVADE


class TestTimeToGroundRecovery:
    """ADR 086 d2/d3/d4 — dive recovery triggers on predicted time to ground.

    Replays the 2026-08-21 18:41 crash: the aircraft mushed at 9203 m and dived
    to 2301 m in 27 s with 2 missiles aboard while the tree kept selecting
    Engage. The altitude band never opened, because every altitude on the way
    down was far ABOVE `enter_below_alt` until it was much too late.
    """

    @staticmethod
    def _cond(clock, **kw):
        opts = dict(recover_below_time_s=20.0, confirm_bypass_time_s=10.0,
                    descent_memory_s=5.0, clock=clock)
        opts.update(kw)
        return make_climb_condition(500, 1000, **opts)

    def test_altitude_band_alone_never_fires_in_the_observed_dive(self):
        """The regression: altitude alone is blind to a dive through it."""
        cond = make_climb_condition(500, 1000, confirm_reads=1)
        for alt in (9203, 8226, 5669, 4096, 2301):
            assert cond(make_snap(altitude=float(alt), altitude_rate=-560.0)) is False, \
                f"altitude band should not fire at {alt} m (it never did)"

    def test_fires_on_time_to_ground_while_still_high(self):
        """6636 m at -338 m/s is ~19.6 s from impact — inside the 20 s window,
        and ~10 s before the observed impact rather than 4 s."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=6636.0, altitude_rate=-338.0)) is True

    def test_does_not_fire_in_a_gentle_descent(self):
        """Same altitude, ordinary rate: 6636 m at -50 m/s is 133 s away."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=6636.0, altitude_rate=-50.0)) is False

    def test_does_not_fire_while_climbing(self):
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=3000.0, altitude_rate=+200.0)) is False

    def test_single_read_bypass_inside_the_margin(self):
        """d3: with confirm_reads=2, a 6 s time-to-ground must not wait for a
        second read — the wait spends the margin the trigger protects."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=2)
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is True

    def test_outside_bypass_still_debounces(self):
        """A 15 s time-to-ground is urgent but not immediate: honour the
        confirm count so one bad reading cannot command a climb."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=2)
        snap = make_snap(altitude=7500.0, altitude_rate=-500.0)
        assert cond(snap) is False, "fired on a single read outside the bypass"
        assert cond(snap) is True

    def test_rejected_telemetry_holds_the_descent(self):
        """d4: a rejected reading mid-dive is evidence of rapid change, not of
        safety. The plausibility filter rejected twice during the real dive."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is True
        clock.advance(2.0)
        assert cond(make_snap(altitude=None, altitude_rate=None)) is True, \
            "blind read cleared an established dive"

    def test_descent_memory_expires(self):
        """The hold is bounded — it must not latch a climb forever."""
        clock = FakeClock()
        cond = make_climb_condition(500, 1000, recover_below_time_s=20.0,
                                    confirm_bypass_time_s=10.0,
                                    descent_memory_s=5.0, confirm_reads=1,
                                    clock=clock)
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is True
        clock.advance(30.0)
        # Recovered and climbing again: the band may now release normally.
        assert cond(make_snap(altitude=6000.0, altitude_rate=+50.0)) is False

    def test_emergency_survives_the_band_release(self):
        """The dive happens far ABOVE exit_above_alt, so ordinary hysteresis
        would clear the recovery on the very tick it started."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=8000.0, altitude_rate=-500.0)) is True
        assert cond(make_snap(altitude=7000.0, altitude_rate=-500.0)) is True

    def test_disabled_when_unconfigured(self):
        """Unset thresholds leave the pure ADR 073 altitude band."""
        cond = make_climb_condition(500, 1000, confirm_reads=1)
        assert cond(make_snap(altitude=3000.0, altitude_rate=-900.0)) is False


class TestDiveRecoveryRespawnGuard:
    """ADR 086 d2: a respawn is an altitude discontinuity, not a descent.

    Live false positive 2026-08-21 21:28:49 — DIVE RECOVERY fired "2s to
    ground" at a smoothed 324 m while the newly respawned aircraft was at 10 m
    and climbing away at +513 m/s. The smoothed value had carried the dead
    aircraft's fall across the respawn boundary.
    """

    @staticmethod
    def _cond(clock):
        return make_climb_condition(500, 1000, recover_below_time_s=20.0,
                                    confirm_bypass_time_s=10.0,
                                    descent_memory_s=5.0, confirm_reads=1,
                                    clock=clock)

    def test_does_not_fire_on_the_first_samples_after_respawn(self):
        clock = FakeClock()
        cond = self._cond(clock)
        cond(make_snap(is_respawning=True, altitude=None, altitude_rate=None))
        # Above enter_below_alt, so ONLY the time-to-ground trigger could fire.
        # Stale carry-over: looks like a dive, is actually a dead aircraft.
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is False, \
            "fired on the respawn discontinuity"

    def test_still_fires_once_settled_after_a_respawn(self):
        """The guard must delay the trigger, never disable it."""
        clock = FakeClock()
        cond = self._cond(clock)
        cond(make_snap(is_respawning=True, altitude=None, altitude_rate=None))
        cond(make_snap(altitude=5000.0, altitude_rate=+100.0))   # settling 1
        cond(make_snap(altitude=5200.0, altitude_rate=+100.0))   # settling 2
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is True, \
            "guard suppressed a genuine dive after the aircraft had settled"

    def test_a_later_respawn_re_arms_the_guard(self):
        clock = FakeClock()
        cond = self._cond(clock)
        for _ in range(3):
            cond(make_snap(altitude=5000.0, altitude_rate=+50.0))
        cond(make_snap(is_respawning=True, altitude=None, altitude_rate=None))
        assert cond(make_snap(altitude=3000.0, altitude_rate=-500.0)) is False, \
            "guard did not re-arm on the second respawn"


# --- ADR 028 revision 4: the Regroup leaf ------------------------------------
#
# The revision-4 change was first wired inside _actuate_engage only, which is
# reachable solely when the Engage leaf is selected — and Engage requires
# contacts. So regroup, whose entire purpose is the no-contact case, sat behind
# a condition demanding contacts, and fired 5 times in a 2-hour session. The
# leaf is what makes it reachable.

from wingman.behavior_tree import (TACTIC_REGROUP, has_friendlies)


def _battle_snap(**kw):
    base = dict(health=100, missiles=4, flares=4, ring_short=0, ring_mid=0,
                ring_long=0, enemy_absent_seconds=0.0, altitude=5000.0,
                is_respawning=False, incoming_detected=False,
                mission_running=True, game_state=GameState.GAME_BATTLE)
    base.update(kw)
    return AnalyzerSnapshot(**base)


def _select(snap):
    tree = build_tree({}, regroup_enabled=True)
    writer = make_snapshot_writer()
    writer.set("snapshot", snap)
    tree.tick()
    return selected_tactic(tree)


def test_regroup_is_selected_when_only_friendlies_are_visible():
    assert _select(_battle_snap(friendly_contacts=3)) == TACTIC_REGROUP


def test_an_enemy_contact_outranks_regroup():
    assert _select(_battle_snap(ring_long=1, friendly_contacts=3)) == TACTIC_ENGAGE


def test_regroup_yields_to_attack_support_when_nothing_is_visible():
    assert _select(_battle_snap()) == TACTIC_ATTACK_SUPPORT


def test_regroup_sits_above_attack_support_in_the_selector():
    """AttackSupport is `always`, so anything below it is unreachable."""
    tree = build_tree({}, regroup_enabled=True)
    names = [c.name for c in tree.root.children]
    assert names.index(TACTIC_REGROUP) < names.index(TACTIC_ATTACK_SUPPORT)
    assert names.index(TACTIC_ENGAGE) < names.index(TACTIC_REGROUP)


def test_has_friendlies_excludes_the_enemy_case_explicitly():
    assert has_friendlies(_battle_snap(friendly_contacts=2)) is True
    assert has_friendlies(_battle_snap(ring_mid=1, friendly_contacts=2)) is False
    assert has_friendlies(_battle_snap()) is False


def test_friendly_contacts_defaults_to_zero_for_existing_callers():
    assert _battle_snap().friendly_contacts == 0


def test_climb_stays_above_engage_when_leaves_are_added():
    """Regression for a positional insert. Climb was placed at
    `len(children) - 2`, which meant "above Engage" only while exactly two
    leaves followed. Adding Regroup silently inverted the ADR 073 priority and
    broke two climb tests — the ordering must hold by name, not by count."""
    tree = build_tree({"climb": {"enabled": True}}, regroup_enabled=True)
    names = [c.name for c in tree.root.children]
    if TACTIC_CLIMB in names:
        assert names.index(TACTIC_CLIMB) < names.index(TACTIC_ENGAGE)
        assert names.index(TACTIC_ENGAGE) < names.index(TACTIC_REGROUP)


def test_the_flag_disables_the_whole_feature_not_half_of_it():
    """Live 2026-08-30: `regroup_enabled: false` still produced 21 Regroup
    selections in nine minutes, because the flag gated only the navigator mode
    while the leaf was added unconditionally. A flag that half-disables a
    feature silently invalidates any A/B run against it."""
    off = [c.name for c in build_tree({}, regroup_enabled=False).root.children]
    on = [c.name for c in build_tree({}, regroup_enabled=True).root.children]
    assert TACTIC_REGROUP not in off
    assert TACTIC_REGROUP in on


def test_disabling_regroup_leaves_the_rest_of_the_selector_intact():
    off = [c.name for c in build_tree({}, regroup_enabled=False).root.children]
    assert TACTIC_ENGAGE in off and TACTIC_ATTACK_SUPPORT in off
    assert off.index(TACTIC_ENGAGE) < off.index(TACTIC_ATTACK_SUPPORT)


# --- ADR 107: BoundaryTurn ----------------------------------------------------

def _bsnap(dist, fwd, **kw):
    return make_snap(boundary_dist=dist, boundary_forward=fwd, **kw)


def _bcond(turn_frac=0.50, recede=0.06, **kw):
    kw.setdefault("min_clear_frac", 0.0)   # older tests predate the clearance rule
    return make_boundary_condition(turn_frac, recede, **kw)


def test_entry_needs_the_edge_ahead():
    """Without it, any pass within the band would roll the aircraft."""
    c = _bcond()
    assert c(_bsnap(0.40, -0.30)) is False
    assert c(_bsnap(0.40, +0.30)) is True


def test_a_negative_forward_read_does_not_drop_the_turn():
    """Measured over 32 crossing traces on 2026-09-01: the sign of `forward`
    flipped between adjacent ticks 27% of the time and read <= 0 on 51% of the
    ticks where the aircraft was demonstrably closing. Releasing on it made the
    turn chatter and the heading never moved."""
    c = _bcond()
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(0.40, -0.35)) is True
    assert c(_bsnap(0.30, -0.25)) is True


def test_the_turn_releases_once_the_aircraft_recedes():
    """Recession is measured from the CLOSEST approach of this turn, so an arc
    that dips and then opens out is recognised as working."""
    c = _bcond()
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(0.30, +0.20)) is True
    assert c(_bsnap(0.34, -0.10)) is True, "inside the 0.06 margin"
    assert c(_bsnap(0.37, -0.10)) is False, "0.30 + 0.06 exceeded"


def test_leaving_the_band_releases_the_turn():
    c = _bcond()
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(0.80, +0.70)) is False


def test_a_dropped_reading_freezes_rather_than_releasing():
    """A gap in perception is not evidence the aircraft is clear."""
    c = _bcond()
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(None, None)) is True
    assert c(_bsnap(None, +0.4)) is True


def test_blindness_never_starts_a_turn():
    assert _bcond()(_bsnap(None, None)) is False


def test_a_zero_threshold_disables_the_leaf():
    assert _bcond(turn_frac=0.0)(_bsnap(0.01, +0.99)) is False


def test_it_yields_to_the_climb_emergency_band():
    """ADR 107 D4: hitting the ground is certain, the boundary is a countdown."""
    emergency = {"on": False}
    c = _bcond(yields_to_fn=lambda: emergency["on"])
    assert c(_bsnap(0.40, +0.30)) is True
    emergency["on"] = True
    assert c(_bsnap(0.40, +0.30)) is False


def test_selection_beats_climb_engage_and_regroup(clock):
    cfg = dict(BT_CFG, boundary={"turn_frac": 0.50, "recede_frac": 0.06, "hold_s": 0.0},
               climb={"enabled": True, "enter_below_alt": 1000, "exit_above_alt": 2000})
    tree = build_tree(cfg, clock=clock)
    writer = make_snapshot_writer()
    snap = make_snap(boundary_dist=0.30, boundary_forward=0.25,
                     ring_long=5, altitude=500.0, friendly_contacts=4)
    writer.set("snapshot", snap)
    tree.tick()
    assert selected_tactic(tree) == TACTIC_BOUNDARY_TURN


def test_it_yields_to_the_defensive_tactics(clock):
    cfg = dict(BT_CFG, boundary={"turn_frac": 0.50, "recede_frac": 0.06, "hold_s": 0.0})
    tree = build_tree(cfg, clock=clock)
    writer = make_snapshot_writer()
    for field, expected in (("is_respawning", TACTIC_RESPAWN_WAIT),
                            ("missiles", TACTIC_EJECT)):
        kw = {field: True if field == "is_respawning" else 0}
        writer.set("snapshot", make_snap(boundary_dist=0.10,
                                         boundary_forward=0.09, **kw))
        tree.tick()
        assert selected_tactic(tree) == expected, field


def test_the_leaf_is_absent_when_unconfigured(clock):
    tree = build_tree(dict(BT_CFG), clock=clock)
    assert TACTIC_BOUNDARY_TURN not in [c.name for c in tree.root.children]


def test_the_condition_is_not_sticky_while_the_turn_runs():
    """Regression on 2026-09-03. The condition IS the closed loop, so it must
    stay free to open it.

    Shipped sticky, every one of the nine turns that session burned the full
    12 s cap — four back to back on a single approach — while the range
    oscillated 0.216R to 0.514R, clearing the release margin repeatedly and
    never being allowed to act on it. MissileEvade and Climb are sticky because
    they run to a goal of their own; this tactic has no goal but the reading."""
    import inspect
    src = inspect.getsource(make_boundary_condition)
    assert "is_running_fn" not in src, \
        "a running actuation must not force the condition true"

    c = _bcond()
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(0.30, +0.20)) is True
    # Receding: releases on the reading alone, with no reference to whether the
    # controller thread happens to still be mid-manoeuvre.
    assert c(_bsnap(0.37, -0.10)) is False


def test_deselection_stops_the_actuation():
    """The other half of the same fix: nothing else ends the thread early, so
    the handler has to ask when the leaf stops being selected."""
    import inspect
    from wingman.tick_handlers import BehaviorTreeHandler
    src = inspect.getsource(BehaviorTreeHandler)
    assert "stop_boundary_turn()" in src
    assert "selection != TACTIC_BOUNDARY_TURN" in src


def test_recession_does_not_release_while_still_on_the_edge():
    """Regression on 2026-09-03, 105 turns. Recession answers "is the turn
    working"; the first version wrongly used it to answer "are we safe now".
    Median release was 0.34R, 27% inside 0.20R — one logged 0.02R to 0.06R,
    which clears a 0.06 margin while handing back an aircraft still on the edge.
    Nine of that session's twelve crossings had the turn running."""
    c = _bcond(release_frac=0.60, min_clear_frac=0.35)
    assert c(_bsnap(0.10, +0.09)) is True
    # Receded by well over the margin, but 0.16R is still the boundary.
    assert c(_bsnap(0.16, -0.05)) is True
    assert c(_bsnap(0.30, -0.05)) is True, "0.30R is inside min_clear_frac"
    assert c(_bsnap(0.42, -0.05)) is False, "clear of 0.35R and receding"


def test_the_release_threshold_is_wider_than_the_entry():
    """Enter at turn_frac, leave at release_frac — the band-exit backstop for a
    turn that never satisfies the recession rule. Equal thresholds would flap on
    the entry boundary, which is why Climb's altitude band has two.

    recede_frac is set high here to isolate the backstop: with the production
    0.06 the recession rule almost always fires first, and this branch only
    matters when it does not."""
    c = _bcond(recede=0.50, release_frac=0.60, min_clear_frac=0.35)
    assert c(_bsnap(0.48, +0.45)) is True
    assert c(_bsnap(0.55, +0.10)) is True, "between the two thresholds — hold"
    assert c(_bsnap(0.62, +0.10)) is False


def test_the_hysteresis_is_wired_from_config():
    import inspect
    from wingman.behavior_tree import build_tree
    src = inspect.getsource(build_tree)
    assert "release_frac=boundary_cfg.get(\"release_frac\")" in src
    assert "min_clear_frac" in src


def test_a_respawn_clears_a_held_turn():
    """2026-09-04: a turn held through a respawn and re-selected one second
    after it, with the aircraft freshly spawned. An aircraft never spawns
    pointing at the boundary, so a turn straight after a respawn is a reliable
    indicator that something upstream has latched."""
    c = _bcond(min_clear_frac=0.0)
    assert c(_bsnap(0.20, +0.18)) is True
    assert c(_bsnap(None, None, is_respawning=True)) is False
    # And the latch is gone, not merely masked for that tick.
    assert c(_bsnap(None, None)) is False


def test_the_freeze_on_blindness_is_bounded():
    """The freeze is right for a dropped tick and wrong as a latch. The detector
    is blind on ~70% of ticks, so "hold until a reading disagrees" means "hold
    indefinitely" — which is what carried a turn through the respawn above."""
    c = _bcond(min_clear_frac=0.0, blind_ticks=3)
    assert c(_bsnap(0.20, +0.18)) is True
    for _ in range(3):
        assert c(_bsnap(None, None)) is True, "a short gap must not release"
    assert c(_bsnap(None, None)) is False, "sustained blindness must release"


def test_a_reading_resets_the_blind_counter():
    c = _bcond(min_clear_frac=0.0, blind_ticks=2)
    assert c(_bsnap(0.20, +0.18)) is True
    assert c(_bsnap(None, None)) is True
    assert c(_bsnap(0.18, +0.15)) is True
    for _ in range(2):
        assert c(_bsnap(None, None)) is True
    assert c(_bsnap(None, None)) is False


def test_outside_battle_clears_the_turn():
    from wingman.analyzer import GameState
    c = _bcond(min_clear_frac=0.0)
    assert c(_bsnap(0.20, +0.18)) is True
    assert c(_bsnap(0.20, +0.18, game_state=GameState.GAME_LOBBY)) is False


def test_a_boundary_abeam_does_not_start_a_turn():
    """2026-09-04, one second after a respawn: dist=0.281 fwd=+0.006. The
    forward component is 2% of the range, so the edge is essentially
    perpendicular — the aircraft is flying ALONG it, not at it. `forward > 0`
    alone called that "ahead" and turned a freshly spawned aircraft.

    Measured over 184 ticks preceding confirmed crossings, genuine approaches
    run a median forward/dist of 0.82 with a 10th percentile of 0.24."""
    c = _bcond(min_clear_frac=0.0, entry_ratio=0.25)
    assert c(_bsnap(0.281, +0.006)) is False, "abeam is not ahead"
    assert c(_bsnap(0.281, +0.060)) is False, "0.21 ratio is still abeam"
    assert c(_bsnap(0.281, +0.230)) is True, "0.82 is a genuine approach"


def test_the_bearing_gate_applies_only_to_entry():
    """The hold keys on range, not bearing — that is ADR 107 D5, measured. A
    turn already running must not be dropped because the bearing swings, which
    is exactly what it is trying to make happen."""
    c = _bcond(min_clear_frac=0.0, entry_ratio=0.25)
    assert c(_bsnap(0.30, +0.28)) is True
    assert c(_bsnap(0.28, +0.001)) is True, "bearing swung, but hold on range"


# --- ADR 109: Eject yields to a survival hold ---------------------------------

def test_eject_does_not_fire_during_a_survival_hold(harness):
    """2026-09-04 09:58:45. With the rack empty during mission_loiter, Eject dove
    the aircraft at -71 degrees and -660 m/s, logged "climb suppressed — eject in
    progress" for four seconds while loiter tried to climb, and killed it.

    Loiter's entire objective is staying alive; an empty rack is irrelevant to
    that. Eject exists to trade a spent aircraft for a rearmed one, which is the
    opposite trade."""
    assert tick(harness, make_snap(missiles=0)) == TACTIC_EJECT
    assert tick(harness, make_snap(missiles=0, survival_hold=True)) != TACTIC_EJECT


def test_the_confirmed_eject_path_yields_too():
    """The debounced condition is what runs once the leaf actuates, so gating
    only the raw read would leave the live path unchanged."""
    from wingman.behavior_tree import is_eject_confirmed, is_missiles_empty
    assert is_missiles_empty(make_snap(missiles=0)) is True
    assert is_missiles_empty(make_snap(missiles=0, survival_hold=True)) is False
    assert is_eject_confirmed(make_snap(missiles_empty_confirmed=True)) is True
    assert is_eject_confirmed(
        make_snap(missiles_empty_confirmed=True, survival_hold=True)) is False


def test_a_survival_hold_does_not_disarm_the_defensive_tactics(harness):
    """Yielding is specific to Eject. RespawnWait and MissileEvade protect the
    aircraft rather than spend it, so they still outrank the hold."""
    assert tick(harness, make_snap(is_respawning=True,
                                   survival_hold=True)) == TACTIC_RESPAWN_WAIT


# --- ADR 120: release on the nearest reading, enter on the filtered one -------

def test_the_turn_does_not_release_on_a_filtered_reading_while_a_near_one_stands():
    """ADR 120. The live failure path.

    A single reading at or above `release_frac` releases the turn outright.
    Measured 2026-09-05: the median filter reported 0.10R or more for 44% of
    RAW readings inside 0.10R, including 0.019 -> 0.516 — so the release read a
    comfortable range while the aircraft sat on the edge.
    """
    c = _bcond(turn_frac=0.30, release_frac=0.45)
    assert c(_bsnap(0.20, +0.19)) is True                     # turn starts
    # Filtered says clear; the nearest recent reading says otherwise.
    assert c(make_snap(boundary_dist=0.52, boundary_forward=+0.40,
                       boundary_near=0.02)) is True, \
        "released the turn with a 0.02R reading still in the window"


def test_the_turn_still_releases_when_the_aircraft_is_genuinely_clear():
    """The guard must not become a latch — if every recent reading is far, the
    turn has to end or it never stops."""
    c = _bcond(turn_frac=0.30, release_frac=0.45)
    assert c(_bsnap(0.20, +0.19)) is True
    assert c(make_snap(boundary_dist=0.52, boundary_forward=+0.40,
                       boundary_near=0.50)) is False


def test_entry_still_uses_the_filtered_reading():
    """Entry keeps the noise rejection: a spurious near value must not start a
    turn, because entry is where a false positive is cheap and common."""
    c = _bcond(turn_frac=0.30, release_frac=0.45)
    # Filtered says far (no turn) even though a near value sits in the window.
    assert c(make_snap(boundary_dist=0.60, boundary_forward=+0.55,
                       boundary_near=0.01)) is False


def test_recession_is_judged_on_the_nearest_reading_too():
    """Recession is a clearance claim. Judged on the filtered value it can
    manufacture a recession the aircraft never flew."""
    c = _bcond(turn_frac=0.30, recede=0.06, min_clear_frac=0.35,
               release_frac=0.90)
    assert c(_bsnap(0.20, +0.19)) is True
    assert c(make_snap(boundary_dist=0.80, boundary_forward=+0.70,
                       boundary_near=0.05)) is True, \
        "recession declared while the nearest reading was 0.05R"


def test_a_snapshot_without_the_near_field_behaves_as_before():
    """Back-compatibility: `boundary_near` absent falls back to `boundary_dist`,
    so nothing that does not supply it changes behaviour."""
    c = _bcond(turn_frac=0.30, release_frac=0.45)
    assert c(_bsnap(0.20, +0.19)) is True
    assert c(_bsnap(0.52, +0.40)) is False

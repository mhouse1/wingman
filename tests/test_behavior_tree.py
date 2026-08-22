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
    TACTIC_CLIMB,
    TACTIC_DISENGAGE,
    TACTIC_EJECT,
    TACTIC_ENGAGE,
    TACTIC_EVADE,
    TACTIC_IDLE,
    TACTIC_MISSILE_EVADE,
    TACTIC_RESPAWN_WAIT,
    build_tree,
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

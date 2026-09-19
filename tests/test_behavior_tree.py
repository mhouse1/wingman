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
    tree_status_dict,
    tree_status_text,
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


def test_tree_status_text_names_the_running_leaf(harness):
    """Research 013's live-status view: the selected tactic must show RUNNING
    and every other top-level slot must appear too, not just the winner —
    the whole point is seeing what did NOT run, not only what did."""
    tick(harness, make_snap(ring_long=1))
    text = tree_status_text(harness[0])
    assert "Engage [*]" in text          # '*' is py_trees' RUNNING glyph
    assert "AttackSupport [-]" in text   # never reached: still INVALID
    for name in (TACTIC_IDLE, TACTIC_EJECT):
        assert name in text


def test_tree_status_text_before_any_tick_does_not_raise(harness):
    # A freshly built tree has no status yet (py_trees.common.Status.INVALID)
    # — this must render, not crash, since nothing here has ticked once.
    text = tree_status_text(harness[0])
    assert "TacticSelector" in text


def test_tree_status_dict_is_the_structured_sibling(harness):
    """Design 012: same data as tree_status_text, JSON-serializable instead
    of ascii art — the JSONL trace writer's actual payload."""
    tick(harness, make_snap(ring_long=1))
    statuses = tree_status_dict(harness[0])
    assert statuses["Engage"] == "RUNNING"
    assert statuses["Idle"] == "FAILURE"
    assert statuses["AttackSupport"] == "INVALID"
    assert statuses["TacticSelector"] == "RUNNING"


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
    """start_fn / is_running_fn / update_fn trio that records calls."""

    def __init__(self):
        self.starts = 0
        self.running = False
        self.updates = 0

    def start(self):
        self.starts += 1

    def is_running(self):
        return self.running

    def update(self, _snapshot):
        # ADR 137 D9: the RUNNING-tick update channel.
        self.updates += 1


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
        # ADR 137 D4: 30.0/15.0 — the values actually shipped in
        # config.yaml, not the pre-D4 20.0/10.0 (code-review finding,
        # 2026-09-11: this fixture previously never exercised the real
        # production thresholds).
        opts = dict(recover_below_time_s=30.0, confirm_bypass_time_s=15.0,
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
        """8700 m at -300 m/s is 29 s from impact — inside the ADR 137 D4
        30 s window (and would have been OUTSIDE the pre-D4 20 s one)."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=1)
        assert cond(make_snap(altitude=8700.0, altitude_rate=-300.0)) is True

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
        """d3, ADR 137 D4: with confirm_reads=2, a 12 s time-to-ground (inside
        the 15 s bypass shipped by D4, outside the pre-D4 10 s one) must not
        wait for a second read — the wait spends the margin the trigger
        protects."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=2)
        assert cond(make_snap(altitude=1800.0, altitude_rate=-150.0)) is True

    def test_outside_bypass_still_debounces(self):
        """A 20 s time-to-ground is urgent but not immediate (outside the
        ADR 137 D4 15 s bypass, inside its 30 s recovery band): honour the
        confirm count so one bad reading cannot command a climb."""
        clock = FakeClock()
        cond = self._cond(clock, confirm_reads=2)
        snap = make_snap(altitude=8000.0, altitude_rate=-400.0)
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
        cond = make_climb_condition(500, 1000, recover_below_time_s=30.0,
                                    confirm_bypass_time_s=15.0,
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


class TestTerrainAheadTrigger:
    """HLDD 001 Phase 1 — forward sky-occlusion terrain-ahead OR-term.

    Same confirm-reads debounce shape as the ttg trigger above, driven by
    ``snapshot.terrain_sky_frac`` (a raw per-tick fraction from
    ``analyzer.detect_terrain_ahead``) rather than altitude/rate, and gated
    on ``snapshot.padlock_state`` being CONFIRMED False (ADR 140 / Open
    Question 6, resolved 2026-09-18) — the padlock camera re-points away
    from forward-looking whenever engaged, which can corrupt the sky
    reading in either direction. Unless a test is specifically exercising
    that gate, ``_cond``/``_snap`` below default padlock_state=False so the
    rest of the debounce logic can be tested in isolation, the same way
    these tests already isolate sky_frac from altitude.
    """

    @staticmethod
    def _cond(clock, **kw):
        opts = dict(terrain_enabled=True, terrain_shadow=False,
                    terrain_sky_min_frac=0.55, terrain_confirm_reads=2,
                    confirm_reads=1, clock=clock)
        opts.update(kw)
        return make_climb_condition(500, 1000, **opts)

    @staticmethod
    def _snap(**kw):
        kw.setdefault("padlock_state", False)
        return make_snap(**kw)

    def test_clear_sky_never_triggers(self):
        clock = FakeClock()
        cond = self._cond(clock)
        for _ in range(5):
            cond(self._snap(altitude=5000.0, terrain_sky_frac=0.95))
        assert cond.terrain_ahead_active is False
        assert cond.emergency_active is False

    def test_low_sky_fraction_triggers_after_confirm_reads(self):
        clock = FakeClock()
        cond = self._cond(clock)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.20)
        cond(snap)
        assert cond.emergency_active is False, \
            "fired on the first read, before terrain_confirm_reads"
        cond(snap)
        assert cond.emergency_active is True, \
            "did not fire on the confirming second read"

    def test_single_tick_dropout_does_not_trigger(self):
        """A single bad low read followed by a recovered high read must not
        command a climb — the streak resets on the intervening good read."""
        clock = FakeClock()
        cond = self._cond(clock, terrain_confirm_reads=2)
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.95))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20))
        assert cond.emergency_active is False, \
            "streak should have reset on the intervening high-sky read"

    def test_missing_reading_resets_the_streak(self):
        """Unlike the ttg trigger's blind-read memory (a perception gap holds
        the last known descent), a missing terrain_sky_frac resets the
        streak to zero rather than freezing it — see update_emergency's
        ``if sky_frac is not None`` branch."""
        clock = FakeClock()
        cond = self._cond(clock)
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=None))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20))
        assert cond.emergency_active is False

    def test_shadow_mode_logs_but_does_not_actuate(self):
        """Production config.yaml ships terrain_shadow: true — the streak and
        terrain_ahead_active still track reality (for the WARNING log and
        future evidence capture), but emergency_active must stay False so
        nothing actually climbs on it yet."""
        clock = FakeClock()
        cond = self._cond(clock, terrain_shadow=True)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.20)
        cond(snap)
        cond(snap)
        assert cond.terrain_ahead_active is True
        assert cond.emergency_active is False

    def test_disabled_by_default(self):
        """terrain_enabled defaults to False: a low sky fraction must not
        affect the condition at all unless explicitly turned on."""
        clock = FakeClock()
        cond = make_climb_condition(500, 1000, confirm_reads=1, clock=clock)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.05)
        cond(snap)
        cond(snap)
        assert cond.emergency_active is False
        assert cond.terrain_ahead_active is False

    # -- ADR 140 / Open Question 6: the padlock gate itself -----------------

    def test_padlock_engaged_blocks_the_terrain_trigger(self):
        """Low sky fraction while padlock is confirmed True (camera is
        actually looking at a locked target, not the flight path) must not
        accumulate toward a climb — the reading isn't trustworthy."""
        clock = FakeClock()
        cond = self._cond(clock)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=True)
        cond(snap)
        cond(snap)
        assert cond.terrain_ahead_active is False
        assert cond.emergency_active is False

    def test_padlock_unknown_blocks_the_terrain_trigger(self):
        """Unknown must never be treated as good enough — only a CONFIRMED
        False padlock_state makes the sky reading trustworthy. Guessing
        ahead of a real measurement is exactly what this design avoids
        elsewhere (ADR 140 Non-Goal 1); the terrain gate holds to the same
        standard."""
        clock = FakeClock()
        cond = self._cond(clock)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=None)
        cond(snap)
        cond(snap)
        assert cond.terrain_ahead_active is False
        assert cond.emergency_active is False

    def test_padlock_confirmed_off_allows_the_terrain_trigger(self):
        """The positive case: padlock_state False plus a real low sky
        fraction accumulates and fires exactly like the un-gated tests
        above — the gate adds a precondition, it doesn't change the
        underlying debounce behavior."""
        clock = FakeClock()
        cond = self._cond(clock)
        snap = self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=False)
        cond(snap)
        assert cond.emergency_active is False
        cond(snap)
        assert cond.emergency_active is True

    def test_padlock_engaging_mid_streak_resets_it(self):
        """Same policy as a missing reading: padlock engaging partway
        through an accumulating streak must reset it, not freeze or ignore
        it — a tick where the camera view can't be trusted is not evidence
        either way."""
        clock = FakeClock()
        cond = self._cond(clock, terrain_confirm_reads=2)
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=False))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=True))
        cond(self._snap(altitude=5000.0, terrain_sky_frac=0.20, padlock_state=False))
        assert cond.emergency_active is False, \
            "streak should have reset while padlock was engaged mid-sequence"

    def test_respawn_confirms_padlock_off_so_the_gate_is_satisfiable(self):
        """HLDD 001's original motivating case: flying forward into terrain
        right after a respawn. ADR 140 D2 sets padlock_state() False on
        every respawn detection (Controller.stop_eject_sequence), so the
        gate this test exercises is exactly what makes the terrain trigger
        live during that specific vulnerable window — this test pins the
        integration point (padlock confirmed False + real low sky
        fraction fires), not the respawn detection itself (that's
        Controller's own tests)."""
        clock = FakeClock()
        cond = self._cond(clock)
        snap = self._snap(altitude=100.0, terrain_sky_frac=0.15, padlock_state=False)
        cond(snap)
        cond(snap)
        assert cond.terrain_ahead_active is True
        assert cond.emergency_active is True


class TestDiveRecoveryRespawnGuard:
    """ADR 086 d2: a respawn is an altitude discontinuity, not a descent.

    Live false positive 2026-08-21 21:28:49 — DIVE RECOVERY fired "2s to
    ground" at a smoothed 324 m while the newly respawned aircraft was at 10 m
    and climbing away at +513 m/s. The smoothed value had carried the dead
    aircraft's fall across the respawn boundary.
    """

    @staticmethod
    def _cond(clock):
        return make_climb_condition(500, 1000, recover_below_time_s=30.0,
                                    confirm_bypass_time_s=15.0,
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


# --- Anomaly 007: the yield above never fires in real tree-ticking order -----
#
# The test above proves the yield MECHANISM works when fed a mock
# `yields_to_fn` under direct, isolated control. It does not, and never did,
# prove the real one stays current — `yields_to_fn` reads
# `ClimbCondition.emergency_active`, which is only computed as a side effect
# of py-trees actually ticking Climb's own leaf. py-trees' priority Selector
# never ticks a leaf a higher-priority sibling keeps beating, so while
# BoundaryTurn wins every tick, Climb's condition was never invoked at all —
# `emergency_active` sat frozen at whatever it was before BoundaryTurn took
# over. Live 2026-09-14: ttg measured 6-8s (threshold 30s) for 9+ continuous
# seconds while BoundaryTurn stayed selected and never yielded; the operator
# intervened manually at ~470m still descending. These two tests reproduce it
# with the real tree, not a mock, and pin the fix (`update_emergency`, called
# once per tick before `tree.tick()`, same as `BehaviorTreeHandler.tick()`
# now does in production).

_DIVE_BT_CFG = dict(
    BT_CFG,
    boundary={"turn_frac": 0.50, "recede_frac": 0.06, "hold_s": 0.0,
             "min_clear_frac": 0.0},
    climb={"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
          "recover_below_time_s": 30.0, "confirm_bypass_time_s": 15.0,
          "confirm_reads": 1},
)


def test_reproduces_the_incident_without_the_pre_tick_update(clock):
    """Pins the bug: omit the fix's pre-tick call and BoundaryTurn never
    yields, no matter how deep the emergency, because Climb's own condition
    is simply never asked while BoundaryTurn keeps winning."""
    tree = build_tree(dict(_DIVE_BT_CFG), clock=clock)
    writer = make_snapshot_writer()
    # Approaching the edge AND in a 6s-to-ground dive (well inside the 30s
    # emergency window) — the exact live shape, altitude/rate chosen to
    # match the incident's own readings (2439m at -424m/s -> ttg=5.75s).
    snap = make_snap(altitude=2439.0, altitude_rate=-424.0,
                     boundary_dist=0.40, boundary_forward=+0.30)
    for _ in range(5):
        writer.set("snapshot", snap)
        tree.tick()   # the old production call site: no pre-tick update
        clock.advance(1.5)
        assert selected_tactic(tree) == TACTIC_BOUNDARY_TURN, \
            "reproduction failed — the bug this test pins may already differ"


def test_yields_to_climb_when_the_pre_tick_update_runs(clock):
    """The fix: call tree.climb_emergency_update_fn(snap, now) every tick,
    before tree.tick() — exactly what BehaviorTreeHandler.tick() does now.
    Climb must win within the same tick the emergency becomes current."""
    tree = build_tree(dict(_DIVE_BT_CFG), clock=clock)
    writer = make_snapshot_writer()
    snap = make_snap(altitude=2439.0, altitude_rate=-424.0,
                     boundary_dist=0.40, boundary_forward=+0.30)
    writer.set("snapshot", snap)
    tree.climb_emergency_update_fn(snap, clock())
    tree.tick()
    assert selected_tactic(tree) == TACTIC_CLIMB, \
        "BoundaryTurn still won — the emergency flag was not fresh in time"


# HLDD 001 Phase 1: the terrain-ahead trigger is a second OR-term on the same
# `emergency` flag Anomaly 007 fixed the freshness of — it must yield
# BoundaryTurn exactly as the ttg trigger does, through the identical
# pre-tick `climb_emergency_update_fn` pipeline. `enter_below_alt`/
# `exit_above_alt` are still set (update_emergency short-circuits entirely
# when either is None — ADR 073's "disabled until calibrated" path) but
# `altitude=5000.0` below stays well clear of the band, and
# `recover_below_time_s` is left unset so the ttg trigger is structurally
# inert, isolating this test to the terrain OR-term alone.
_TERRAIN_DIVE_BT_CFG = dict(
    BT_CFG,
    boundary={"turn_frac": 0.50, "recede_frac": 0.06, "hold_s": 0.0,
             "min_clear_frac": 0.0},
    climb={"enabled": True, "enter_below_alt": 500, "exit_above_alt": 1000,
          "confirm_reads": 1,
          "terrain_avoidance": {"enabled": True, "shadow": False,
                                "sky_min_frac": 0.55, "confirm_reads": 1}},
)


def test_boundary_turn_yields_to_a_terrain_emergency(clock):
    """The terrain OR-term must win selection through the same pre-tick
    update channel the ttg trigger uses — not a parallel mechanism."""
    tree = build_tree(dict(_TERRAIN_DIVE_BT_CFG), clock=clock)
    writer = make_snapshot_writer()
    snap = make_snap(altitude=5000.0, altitude_rate=0.0,
                     boundary_dist=0.40, boundary_forward=+0.30,
                     terrain_sky_frac=0.20, padlock_state=False)
    writer.set("snapshot", snap)
    tree.climb_emergency_update_fn(snap, clock())
    tree.tick()
    assert selected_tactic(tree) == TACTIC_CLIMB, \
        "BoundaryTurn still won — the terrain emergency was not fresh in time"


def test_boundary_turn_keeps_selection_when_padlock_is_not_confirmed_off(clock):
    """ADR 140 / Open Question 6, through the real tree: a real low sky
    fraction must not force Climb while the padlock camera isn't confirmed
    pointed forward — the same case test_padlock_engaged_blocks_the_
    terrain_trigger proves at the ClimbCondition level, exercised here
    through full selection."""
    tree = build_tree(dict(_TERRAIN_DIVE_BT_CFG), clock=clock)
    writer = make_snapshot_writer()
    snap = make_snap(altitude=5000.0, altitude_rate=0.0,
                     boundary_dist=0.40, boundary_forward=+0.30,
                     terrain_sky_frac=0.20, padlock_state=True)
    writer.set("snapshot", snap)
    tree.climb_emergency_update_fn(snap, clock())
    tree.tick()
    assert selected_tactic(tree) == TACTIC_BOUNDARY_TURN, \
        "Climb won despite padlock not being confirmed off"


def test_boundary_turn_keeps_selection_when_terrain_is_shadowed(clock):
    """shadow: true (the production default) must compute and log the
    terrain trigger without ever letting it win selection."""
    cfg = dict(_TERRAIN_DIVE_BT_CFG)
    cfg["climb"] = dict(cfg["climb"])
    cfg["climb"]["terrain_avoidance"] = dict(
        cfg["climb"]["terrain_avoidance"], shadow=True)
    tree = build_tree(cfg, clock=clock)
    writer = make_snapshot_writer()
    snap = make_snap(altitude=5000.0, altitude_rate=0.0,
                     boundary_dist=0.40, boundary_forward=+0.30,
                     terrain_sky_frac=0.20, padlock_state=False)
    writer.set("snapshot", snap)
    tree.climb_emergency_update_fn(snap, clock())
    tree.tick()
    assert selected_tactic(tree) == TACTIC_BOUNDARY_TURN, \
        "shadow mode must not actuate — Climb should not have won"


def test_climb_terrain_ahead_fn_is_exposed_and_tracks_the_edge(clock):
    """tick_handlers.py's evidence-capture edge-detector reads this exactly
    like climb_emergency_fn — it must be present on the tree and reflect
    the FALSE->TRUE transition, in shadow mode too (capture is not gated on
    actuation)."""
    tree = build_tree(dict(_TERRAIN_DIVE_BT_CFG), clock=clock)
    assert tree.climb_terrain_ahead_fn is not None
    assert tree.climb_terrain_ahead_fn() is False, "should start False"
    writer = make_snapshot_writer()
    clear_snap = make_snap(altitude=5000.0, altitude_rate=0.0,
                           boundary_dist=0.40, boundary_forward=+0.30,
                           terrain_sky_frac=0.95, padlock_state=False)
    writer.set("snapshot", clear_snap)
    tree.climb_emergency_update_fn(clear_snap, clock())
    tree.tick()
    assert tree.climb_terrain_ahead_fn() is False
    low_snap = make_snap(altitude=5000.0, altitude_rate=0.0,
                         boundary_dist=0.40, boundary_forward=+0.30,
                         terrain_sky_frac=0.20, padlock_state=False)
    writer.set("snapshot", low_snap)
    tree.climb_emergency_update_fn(low_snap, clock())
    tree.tick()
    assert tree.climb_terrain_ahead_fn() is True, \
        "confirm_reads=1 in this fixture — one low read should latch it"


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
    """ADR 139 D1: this logic lives in `_build_boundary_slot` now, not
    inline in `build_tree` — the slot table moved it, not the wiring."""
    import inspect
    from wingman.behavior_tree import _build_boundary_slot
    src = inspect.getsource(_build_boundary_slot)
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


def test_mission_not_running_clears_the_turn():
    """ADR 138. Measured live 2026-09-10 03:03:26: the respawn screen cleared
    (is_respawning -> False) up to ~1.5s before mission_j20 actually
    restarted (the ADR 059 stability window) — and mission_j20 restarting is
    what arms the ADR 132 turn guard. is_respawning alone did not close this
    gap: a full 12s, 180-degree boundary turn selected and ran starting
    inside it, unguarded. mission_running is the same "is this a live,
    commanded aircraft" question is_respawning already answers, just closing
    the later half of the window."""
    c = _bcond(min_clear_frac=0.0)
    assert c(_bsnap(0.20, +0.18)) is True
    assert c(_bsnap(0.20, +0.18, mission_running=False)) is False
    # And the latch is gone, not merely masked for that tick (same guarantee
    # test_a_respawn_clears_a_held_turn makes for is_respawning).
    assert c(_bsnap(0.20, +0.18)) is True
    assert c(_bsnap(None, None, mission_running=False)) is False


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


# ---------------------------------------------------------------------------
# Design 011 (ACS Mode) step 1: jet_profile -> AnalyzerSnapshot.has_padlock.
# Config plumbing only — nothing branches on this yet, so these tests only
# guard the resolution logic, not any tactic behaviour.
# ---------------------------------------------------------------------------

def test_snapshot_has_padlock_defaults_true():
    """Every profile shipped today is has_padlock: true; a snapshot built
    without the field must not silently read as boresight-only."""
    assert make_snap().has_padlock is True


def test_snapshot_has_padlock_can_be_overridden():
    assert make_snap(has_padlock=False).has_padlock is False


def _handler(jet_profile_cfg=None):
    from wingman.tick_handlers import BehaviorTreeHandler
    return BehaviorTreeHandler(None, None, {}, jet_profile_cfg=jet_profile_cfg)


def test_handler_resolves_has_padlock_true_for_the_active_profile():
    h = _handler({"active": "j20", "profiles": {"j20": {"has_padlock": True}}})
    assert h._has_padlock is True


def test_handler_resolves_has_padlock_false_for_the_active_profile():
    h = _handler({"active": "generic_boresight",
                  "profiles": {"j20": {"has_padlock": True},
                               "generic_boresight": {"has_padlock": False}}})
    assert h._has_padlock is False


def test_handler_defaults_to_padlock_true_with_no_jet_profile_config():
    """A config predating Design 011 has no jet_profile block at all — must
    resolve to today's only real airframe behaviour, not crash or guess
    boresight-only."""
    assert _handler(None)._has_padlock is True
    assert _handler({})._has_padlock is True


def test_handler_defaults_to_padlock_true_for_an_unknown_active_profile():
    """A typo'd or not-yet-defined active profile must not silently resolve
    to boresight-only — the safe default matches every profile shipped
    today."""
    h = _handler({"active": "does_not_exist", "profiles": {}})
    assert h._has_padlock is True


# --- ADR 139: golden-master child order, pre slot-table refactor ------------
#
# Captured against the pre-refactor `build_tree` (imperative `children.insert`
# calls) across the full climb/boundary/regroup/sustain flag matrix. This must
# stay green, unmodified, once ADR 139 D1 replaces the imperative inserts with
# a declared slot table — it is the only thing that makes "zero behavior
# change" verified rather than asserted.

_CLIMB_BAND = {"enter_below_alt": 1000, "exit_above_alt": 2000}
_SUSTAIN_BAND = {"enabled": True, "enter_below_alt": 3000, "exit_above_alt": 4000}

_CHILD_ORDER_MATRIX = [
    # (climb_enabled, boundary_configured, regroup_enabled, sustain_enabled) -> names
    ((False, False, False, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Engage", "AttackSupport")),
    ((False, False, False, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Engage", "AttackSupport")),
    ((False, False, True, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Engage", "Regroup", "AttackSupport")),
    ((False, False, True, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Engage", "Regroup", "AttackSupport")),
    ((False, True, False, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Engage", "AttackSupport")),
    ((False, True, False, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Engage", "AttackSupport")),
    ((False, True, True, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Engage", "Regroup", "AttackSupport")),
    ((False, True, True, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Engage", "Regroup", "AttackSupport")),
    ((True, False, False, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Climb", "Engage", "AttackSupport")),
    ((True, False, False, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Climb", "Engage", "AttackSupport")),
    ((True, False, True, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Climb", "Engage", "Regroup", "AttackSupport")),
    ((True, False, True, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "Evade", "Disengage",
      "Climb", "Engage", "Regroup", "AttackSupport")),
    ((True, True, False, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Climb", "Engage", "AttackSupport")),
    ((True, True, False, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Climb", "Engage", "AttackSupport")),
    ((True, True, True, False),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Climb", "Engage", "Regroup", "AttackSupport")),
    ((True, True, True, True),
     ("Idle", "RespawnWait", "Eject", "MissileEvade", "BoundaryTurn", "Evade",
      "Disengage", "Climb", "Engage", "Regroup", "AttackSupport")),
]


@pytest.mark.parametrize("flags,expected_names", _CHILD_ORDER_MATRIX)
def test_build_tree_child_order_across_flag_matrix(flags, expected_names):
    climb_enabled, boundary_configured, regroup_enabled, sustain_enabled = flags
    climb_cfg = dict(_CLIMB_BAND, enabled=climb_enabled)
    if sustain_enabled:
        climb_cfg["sustain"] = dict(_SUSTAIN_BAND)
    bt_cfg = dict(BT_CFG, climb=climb_cfg)
    if boundary_configured:
        bt_cfg["boundary"] = {"turn_frac": 0.50, "recede_frac": 0.06, "hold_s": 0.0}
    tree = build_tree(bt_cfg, regroup_enabled=regroup_enabled)
    names = tuple(c.name for c in tree.root.children)
    assert names == expected_names


# --- ADR 139 D2: consolidated Climb / BoundaryTurn enablement predicates ----

from wingman.behavior_tree import climb_tactic_enabled, boundary_tactic_enabled


def test_climb_tactic_enabled():
    assert climb_tactic_enabled({"climb": {"enabled": True}}) is True
    assert climb_tactic_enabled({"climb": {"enabled": False}}) is False
    assert climb_tactic_enabled({"climb": {}}) is False
    assert climb_tactic_enabled({}) is False


def test_boundary_tactic_enabled():
    assert boundary_tactic_enabled({"boundary": {"turn_frac": 0.5}}) is True
    assert boundary_tactic_enabled({"boundary": {"turn_frac": 0.0}}) is False
    assert boundary_tactic_enabled({"boundary": {}}) is False
    assert boundary_tactic_enabled({}) is False


def test_climb_and_boundary_enabled_checks_share_one_predicate():
    """ADR 139 D2: getting Climb/BoundaryTurn to actuate live used to require
    two independently-written boolean expressions (one in `build_tree`
    deciding tree insertion, one in `BehaviorTreeHandler.__init__` deciding
    actuator wiring) to agree. Assert both sites call the same named
    predicate instead of each spelling out its own `.get(...)` check."""
    import inspect
    from wingman.behavior_tree import _build_climb_slot, _build_boundary_slot
    from wingman.tick_handlers import BehaviorTreeHandler

    bt_src = inspect.getsource(_build_climb_slot) + inspect.getsource(_build_boundary_slot)
    handler_src = inspect.getsource(BehaviorTreeHandler.__init__)

    assert "climb_tactic_enabled(" in bt_src
    assert "boundary_tactic_enabled(" in bt_src
    assert "climb_tactic_enabled(" in handler_src
    assert "boundary_tactic_enabled(" in handler_src
    for src in (bt_src, handler_src):
        assert 'climb_cfg.get("enabled"' not in src
        assert 'boundary_cfg.get("turn_frac"' not in src


# --- ADR 139 D3: hysteresis state is inspectable via named attributes ------

def test_climb_condition_state_is_introspectable_via_named_attributes():
    clock = FakeClock()
    cond = make_climb_condition(1000, 2000, clock=clock)
    assert cond.active is False
    assert cond.emergency_active is False
    # Below enter_below_alt: crosses the band.
    assert cond(make_snap(altitude=500.0)) is True
    assert cond.active is True
    assert cond.emergency_active is False


def test_climb_update_fn_called_only_while_already_running():
    """ADR 137 D9: `update_fn` fires on every RUNNING tick AFTER the first
    — never on the same tick as `start_fn`, and never before selection."""
    climb = _TacticRecorder()
    cfg = dict(BT_CFG, climb={"enabled": True, "enter_below_alt": 1000,
                              "exit_above_alt": 2000})
    tree = build_tree(cfg, actuators={
        TACTIC_CLIMB: (climb.start, climb.is_running, climb.update)})
    writer = make_snapshot_writer()

    def _tick(snap):
        writer.set("snapshot", snap)
        tree.tick()
        return selected_tactic(tree)

    # Tick 1: newly selected — start_fn fires, update_fn does not.
    assert _tick(make_snap(altitude=500.0)) == TACTIC_CLIMB
    assert climb.starts == 1
    assert climb.updates == 0

    # Tick 2: still selected (is_running_fn now True) — update_fn fires,
    # start_fn does not fire again.
    climb.running = True
    assert _tick(make_snap(altitude=500.0)) == TACTIC_CLIMB
    assert climb.starts == 1
    assert climb.updates == 1


def test_boundary_condition_state_is_introspectable_via_named_attributes():
    cond = make_boundary_condition(0.50, min_clear_frac=0.0)
    assert cond.active is False
    assert cond.min_dist is None
    assert cond(_bsnap(0.40, +0.30)) is True
    assert cond.active is True
    assert cond.min_dist == 0.40

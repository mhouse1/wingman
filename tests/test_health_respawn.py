"""Unit tests for ADR 061 (observed-death eject termination), ADR 062 Phase A
(shadow respawn detector), and ADR 063 (health value confirmation filter).

Drives GameStateAnalyzer._process_health_reading directly with synthetic health
values — no OCR, no screenshots.

Canonical sequences under the ADR 063 recurrence filter (window 3, tol 15):
  establish alive        : 240, 240          (first read alone never confirms)
  confirmed/observed death: 0, 0, 0          (value-confirm x2 → evidence-confirm)
  death via overlay      : 0, 0, None        (confirmed 0 then digits vanish)
  respawn recovery       : 250, 250          (window still holds pre-death 0s)

Usage: uv run pytest tests/test_health_respawn.py -q
"""

import copy
import time
from pathlib import Path

import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer, GameState
from wingman.main import _alive_transition_disposition
from wingman.mission_stats import MissionStatsTracker
from constants import CONFIG_PATH


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def _make_analyzer(**overrides) -> GameStateAnalyzer:
    cfg = copy.deepcopy(load_config())
    for dotted, value in overrides.items():
        section, key = dotted.split(".")
        cfg.setdefault(section, {})[key] = value
    a = GameStateAnalyzer(cfg)
    a.state = GameState.GAME_BATTLE.name
    return a


def _feed(a: GameStateAnalyzer, *values):
    for v in values:
        a._process_health_reading(v)


@pytest.fixture
def analyzer():
    # Pin shadow mode: these tests exercise shadow semantics regardless of the
    # shipped config default (dual since Phase B-prime, 2026-08-02).
    a = _make_analyzer(**{"respawn_detection.mode": "shadow"})
    try:
        yield a
    finally:
        a.cleanup()


@pytest.fixture
def fast_window_analyzer():
    """Analyzer with tiny evidence windows so weak-tier tests don't sleep 6-8s."""
    a = _make_analyzer(**{
        "respawn_detection.mode": "shadow",
        "health.death_no_digits_s": 0.05,
        "health.death_no_confirmed_s": 0.05,
    })
    try:
        yield a
    finally:
        a.cleanup()


# ---------------------------------------------------------------------------
# ADR 063 — value confirmation filter
# ---------------------------------------------------------------------------

# Raw accepted reads logged 2026-08-01 17:34 session (true health ~250-264,
# ~50% garbage: fragments, concatenations, one false 0). The filter must ride
# through this with no dead transition, no death evidence, no shadow activity.
GARBAGE_SESSION_READS = [
    250, 264, 250, 264, 26, 250, 350, 20, 250, 0, 260, 64, 250, 64,
    260, 60, 250, 64, 6, 250, 64, 254, 6, 250, 254, 64, 250, 25,
]


class TestValueConfirmation:
    def test_first_read_alone_never_confirms(self, analyzer):
        _feed(analyzer, 240)
        assert analyzer.game_battle_alive is False  # unconfirmed — held

    def test_second_agreeing_read_confirms(self, analyzer):
        _feed(analyzer, 240, 240)
        assert analyzer.game_battle_alive is True

    def test_tolerance_allows_near_values(self, analyzer):
        _feed(analyzer, 250, 264)  # |264-250|=14 <= tol 15
        assert analyzer.game_battle_alive is True

    def test_fragment_never_confirms(self, analyzer):
        _feed(analyzer, 240, 240, 64)
        assert analyzer._health == 240  # fragment held out

    def test_max_plausible_discarded_before_window(self, analyzer):
        _feed(analyzer, 240, 240, 9250)
        assert 9250 not in analyzer._health_raw_window
        assert analyzer._health == 240

    def test_replay_real_garbage_session(self, analyzer):
        _feed(analyzer, *GARBAGE_SESSION_READS[:2])   # legit battle-entry confirm
        analyzer.alive_event.clear()
        _feed(analyzer, *GARBAGE_SESSION_READS[2:])
        assert analyzer.game_battle_alive is True     # never flapped to dead
        assert not analyzer.alive_event.is_set()      # no spurious transitions
        assert analyzer._death_pending is False
        assert analyzer._death_observed is False
        assert analyzer._shadow_mark_tier is None
        assert analyzer._shadow_fires == []
        assert 200 <= analyzer._health <= 280         # tracked the true band

    def test_window_flushed_on_respawn_reset(self, analyzer):
        _feed(analyzer, 240, 240)
        analyzer.reset_health_for_respawn()
        assert list(analyzer._health_raw_window) == []
        _feed(analyzer, 250)
        assert analyzer.game_battle_alive is False    # single post-reset read holds
        _feed(analyzer, 250)
        assert analyzer.game_battle_alive is True

    def test_unconfirmed_reads_do_not_block_the_weak_tier(self, fast_window_analyzer):
        """ADR 064: hallucinated overlay digits never confirm, so the
        confirmed-absence clock runs straight through them (the 03:33 miss class)."""
        a = fast_window_analyzer
        _feed(a, 240, 240)              # anchor
        time.sleep(0.06)
        _feed(a, 64)                    # garbage on the overlay — unconfirmed
        assert a._shadow_mark_tier == "weak"   # clock ran through the garbage


# ---------------------------------------------------------------------------
# ADR 061 — death provenance (observed vs synthetic)
# ---------------------------------------------------------------------------

class TestDeathProvenance:
    def test_confirmed_zero_reads_set_observed_death(self, analyzer):
        _feed(analyzer, 240, 240, 0)
        assert analyzer._death_observed is False  # value-unconfirmed 0
        _feed(analyzer, 0)
        assert analyzer._death_observed is False  # first CONFIRMED 0 only pends
        assert analyzer._death_pending is True
        _feed(analyzer, 0)
        assert analyzer._death_observed is True   # second confirmed 0 confirms
        assert analyzer._death_pending is False   # pending consumed

    def test_synthetic_reset_does_not_set_observed_death(self, analyzer):
        _feed(analyzer, 240, 240)
        analyzer.mark_health_dead_synthetic()
        assert analyzer._death_observed is False
        assert analyzer.game_battle_alive is False

    def test_synthetic_reset_clears_prior_observed_flag(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0)
        assert analyzer._death_observed is True
        analyzer.mark_health_dead_synthetic()
        assert analyzer._death_observed is False

    def test_no_digits_fallback_does_not_set_observed_death(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a.game_battle_alive is False
        assert a._death_observed is False

    def test_alive_transition_latches_observed_death(self, analyzer):
        _feed(analyzer, 240, 240)
        analyzer.alive_event.clear()
        _feed(analyzer, 0, 0, 0, 250, 250)
        assert analyzer.alive_event.is_set()
        assert analyzer.alive_after_observed_death is True
        # the pre-latch flag resets so the next transition re-evaluates
        assert analyzer._death_observed is False

    def test_alive_transition_after_synthetic_death_latches_false(self, analyzer):
        _feed(analyzer, 240, 240)
        analyzer.alive_event.clear()
        analyzer.mark_health_dead_synthetic()
        _feed(analyzer, 250)   # confirms against the 240s still in the window
        assert analyzer.alive_event.is_set()
        assert analyzer.alive_after_observed_death is False

    def test_single_zero_bounce_rejected_at_value_layer(self, analyzer):
        """A lone 0 read mid-combat never becomes evidence (11:01/17:34 sessions)."""
        _feed(analyzer, 240, 240, 0, 250)
        assert analyzer._death_pending is False
        assert analyzer.alive_after_observed_death is False
        assert analyzer._shadow_mark_tier is None
        assert analyzer._shadow_fires == []

    def test_zero_followed_by_no_digits_confirms_death(self, analyzer):
        """Confirmed 0 then digits vanish (death animation → overlay)."""
        _feed(analyzer, 240, 240, 0, 0, None)
        assert analyzer._death_observed is True
        assert analyzer._shadow_mark_tier == "strong"


# ---------------------------------------------------------------------------
# ADR 061 — alive-transition disposition (main loop classification)
# ---------------------------------------------------------------------------

class TestAliveTransitionDisposition:
    def test_battle_state_takes_restart_path(self):
        assert _alive_transition_disposition(GameState.GAME_BATTLE, True) == "restart_path"
        assert _alive_transition_disposition(GameState.GAME_BATTLE, False) == "restart_path"

    def test_eject_with_observed_death_terminates_eject(self):
        assert _alive_transition_disposition(GameState.GAME_BATTLE_EJECT, True) == "terminate_eject"

    def test_eject_without_observed_death_is_spurious(self):
        assert _alive_transition_disposition(GameState.GAME_BATTLE_EJECT, False) == "consume_spurious"

    def test_manual_and_other_states_consume(self):
        assert _alive_transition_disposition(GameState.GAME_BATTLE_MANUAL, True) == "consume_other"
        assert _alive_transition_disposition(GameState.GAME_LOBBY, True) == "consume_other"


# ---------------------------------------------------------------------------
# ADR 062 — shared no-digits window
# ---------------------------------------------------------------------------

class TestNoDigitsWindow:
    def test_default_window_is_6s_from_config(self, analyzer):
        assert analyzer._death_no_digits_s == 6.0

    def test_dropout_shorter_than_window_keeps_alive(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        assert a.game_battle_alive is True

    def test_dropout_past_window_clears_alive(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a.game_battle_alive is False

    def test_recovery_before_window_never_fires_alive_event(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240)
        a.alive_event.clear()
        _feed(a, None, 238)  # digits back before window crossed
        assert not a.alive_event.is_set()  # no dead→alive transition occurred


# ---------------------------------------------------------------------------
# ADR 062 Phase A — shadow respawn detector
# ---------------------------------------------------------------------------

class TestShadowDetector:
    def test_strong_tier_fire_on_alive_after_confirmed_zero(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0)
        assert analyzer._shadow_mark_tier == "strong"
        _feed(analyzer, 250, 250)
        assert analyzer._shadow_mark_tier is None
        assert len(analyzer._shadow_fires) == 1
        assert analyzer._shadow_fires[0][1] == "strong"

    def test_weak_tier_fire_on_alive_after_no_digits_window(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a._shadow_mark_tier == "weak"
        _feed(a, 250)  # confirms against the 240s still in the window
        assert len(a._shadow_fires) == 1
        assert a._shadow_fires[0][1] == "weak"

    def test_strong_upgrades_weak_mark(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a._shadow_mark_tier == "weak"
        _feed(a, 0, 0, 0)
        assert a._shadow_mark_tier == "strong"

    def test_no_fire_without_mark(self, analyzer):
        _feed(analyzer, 240, 240, 238)
        assert analyzer._shadow_fires == []

    # -- ADR 079: telemetry liveness gate on the weak tier ------------------

    def test_weak_mark_suppressed_while_telemetry_live(self, fast_window_analyzer, monkeypatch):
        """Fresh telemetry at mark time = the HUD is rendering = the aircraft
        exists — the confirmed-read gap is OCR dropout, not death (four false
        fires 2026-08-17)."""
        a = fast_window_analyzer

        class _LiveSnap:
            def altitude_fresh(self):
                return True

        monkeypatch.setattr(a, "get_telemetry", lambda: _LiveSnap())
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a._shadow_mark_tier is None, \
            "weak mark formed despite live telemetry"
        _feed(a, 250)
        assert a._shadow_fires == [], "suppressed mark still fired"

    def test_weak_mark_forms_when_telemetry_stale(self, fast_window_analyzer, monkeypatch):
        """A real death silences telemetry — the weak tier fires as before."""
        a = fast_window_analyzer

        class _StaleSnap:
            def altitude_fresh(self):
                return False

        monkeypatch.setattr(a, "get_telemetry", lambda: _StaleSnap())
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)
        assert a._shadow_mark_tier == "weak"
        _feed(a, 250)
        assert len(a._shadow_fires) == 1

    def test_strong_tier_unaffected_by_live_telemetry(self, analyzer, monkeypatch):
        """Strong evidence (confirmed sub-1 then digit loss) is intrinsic —
        the ADR 079 gate applies to the weak tier only."""

        class _LiveSnap:
            def altitude_fresh(self):
                return True

        monkeypatch.setattr(analyzer, "get_telemetry", lambda: _LiveSnap())
        _feed(analyzer, 240, 240, 0, 0, 0)
        assert analyzer._shadow_mark_tier == "strong"

    def test_synthetic_death_never_marks_or_fires(self, analyzer):
        _feed(analyzer, 240, 240)
        analyzer.mark_health_dead_synthetic()
        assert analyzer._shadow_mark_tier is None
        _feed(analyzer, 250)
        assert analyzer._shadow_fires == []

    def test_mark_cleared_when_leaving_battle(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0)
        assert analyzer._shadow_mark_tier == "strong"
        analyzer._shadow_clear_mark()
        _feed(analyzer, 250, 250)
        assert analyzer._shadow_fires == []

    def test_lobby_entry_clears_mark(self, analyzer):
        """Marks must not survive a battle→lobby→battle cycle (2026-08-01 10:01 bug)."""
        _feed(analyzer, 240, 240, 0, 0, 0)
        assert analyzer._shadow_mark_tier == "strong"
        analyzer.on_enter_GAME_LOBBY()
        assert analyzer._shadow_mark_tier is None

    def test_stale_mark_discarded_instead_of_firing(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0)
        analyzer._shadow_mark_ts = time.time() - 31.0  # age the mark past the 30s cap
        _feed(analyzer, 250, 250)
        assert analyzer._shadow_fires == []
        assert analyzer._shadow_mark_tier is None  # discarded, not retained

    def test_mode_ocr_disables_shadow(self):
        a = _make_analyzer(**{"respawn_detection.mode": "ocr"})
        try:
            _feed(a, 240, 240, 0, 0, 0)
            assert a._shadow_mark_tier is None
            _feed(a, 250, 250)
            assert a._shadow_fires == []
            assert a.shadow_respawn_summary() is None
            # ADR 061 provenance is independent of the shadow detector mode
            assert a.alive_after_observed_death is True
        finally:
            a.cleanup()

    def test_unimplemented_modes_fall_back_to_shadow(self):
        for mode in ("health", "health_only", "bogus"):
            a = _make_analyzer(**{"respawn_detection.mode": mode})
            try:
                assert a._respawn_detection_mode == "shadow"
            finally:
                a.cleanup()

    def test_reset_health_for_respawn_does_not_clear_shadow_clock(self, fast_window_analyzer):
        """OCR respawn plumbing must not wipe the shadow detector's weak-tier evidence."""
        a = fast_window_analyzer
        _feed(a, 240, 240, None)          # shadow clock starts
        a.reset_health_for_respawn()      # zeroes _health_no_digits_since only
        time.sleep(0.06)
        _feed(a, None)                    # shadow clock crosses its window
        assert a._shadow_mark_tier == "weak"


class TestCompositeEvidence:
    """ADR 064 — confirmed-absence clock, decline prior, dual-mode firing."""

    def test_garbage_overlay_marks_but_needs_transition_to_fire(self, fast_window_analyzer):
        """The 03:33 class (overlay hallucinating digits so the alive flag never
        drops): weak evidence forms but is withheld without the dead→alive
        transition — the trade accepted by the 2026-08-02 amendment after the
        05:37 session showed transition-less weak fires are 9-for-9 false."""
        a = fast_window_analyzer
        _feed(a, 250, 250)              # anchor established
        time.sleep(0.06)
        _feed(a, 44, None, 6)           # overlay: garbage singles and absence — none confirm
        assert a._shadow_mark_tier == "weak"
        _feed(a, 250, 250)              # recovery confirms, but alive never dropped
        assert a._shadow_fires == []    # withheld — strong tier / respawn OCR cover this class

    def test_fire_on_first_confirmed_read_after_gap(self, fast_window_analyzer):
        """The gap is evaluated at confirm time too — a recovery read arriving
        after a death-length absence must fire even with no interim evaluations."""
        a = fast_window_analyzer
        _feed(a, 240, 240, None)        # absence drops the alive flag (real overlay shape)
        time.sleep(0.06)
        _feed(a, None)                  # raw window crossed → alive False
        _feed(a, 250)                   # confirms; gap exceeded window; dead→alive transition
        assert len(a._shadow_fires) == 1

    def test_weak_fire_suppressed_outside_game_battle(self, fast_window_analyzer):
        """ADR 064 amendment 2: eject onset thrashes health state — all six
        07:58-session false fires triggered 1-2s into GAME_BATTLE_EJECT."""
        a = fast_window_analyzer
        _feed(a, 240, 240, None)
        time.sleep(0.06)
        _feed(a, None)                          # weak mark + alive False
        a.state = GameState.GAME_BATTLE_EJECT.name
        _feed(a, 250)                           # transition, but wrong state
        assert a._shadow_fires == []
        a.state = GameState.GAME_BATTLE.name

    def test_sub1_values_excluded_from_decline_prior(self):
        a = _make_analyzer(**{
            "health.death_no_confirmed_s": 0.2,
            "health.decline_evidence_drop": 80,
        })
        try:
            _feed(a, 250, 250, 0, 0)            # confirmed garbage-zero dip
            time.sleep(0.12)                    # exceeds half-window only
            _feed(a, 44)                        # evaluation tick
            # 250→0 must NOT count as decline (0 is a death claim, not a trend)
            assert a._shadow_mark_tier is None
        finally:
            a.cleanup()

    def test_mid_combat_gap_without_transition_never_fires(self, fast_window_analyzer):
        """Regression for the 05:37 session's 9 false fires: a confirmed-read
        gap with digits present throughout (alive never drops) is a garbage
        stretch, not a respawn — weak evidence without the dead→alive
        transition is discarded."""
        a = fast_window_analyzer
        _feed(a, 240, 240)              # anchor; alive True
        time.sleep(0.06)
        _feed(a, 64)                    # garbage — unconfirmed, digits present, marks weak
        assert a._shadow_mark_tier == "weak"
        _feed(a, 240)                   # confirms; NO transition (alive stayed True)
        assert a._shadow_fires == []    # discarded, not fired
        assert a._shadow_mark_tier is None

    def test_confirmed_stream_never_marks(self, analyzer):
        _feed(analyzer, 240, 240, 240, 238, 240)
        assert analyzer._shadow_mark_tier is None

    def test_decline_halves_the_window(self):
        a = _make_analyzer(**{
            "health.death_no_confirmed_s": 0.2,
            "health.decline_evidence_drop": 80,
        })
        try:
            _feed(a, 250, 250, 160, 160)   # confirmed decline of 90 within window
            time.sleep(0.12)                # exceeds half (0.1) but not full (0.2)
            _feed(a, 44)                    # unconfirmed — evaluation tick
            assert a._shadow_mark_tier == "weak"
        finally:
            a.cleanup()

    def test_no_decline_needs_full_window(self):
        a = _make_analyzer(**{
            "health.death_no_confirmed_s": 0.2,
            "health.decline_evidence_drop": 80,
        })
        try:
            _feed(a, 250, 250)              # steady — no decline prior
            time.sleep(0.12)
            _feed(a, 44)
            assert a._shadow_mark_tier is None    # half-window does not apply
            time.sleep(0.12)
            _feed(a, 6)                     # non-agreeing garbage — stays unconfirmed
            assert a._shadow_mark_tier == "weak"  # full window reached
        finally:
            a.cleanup()

    def test_confirmed_gap_instrumentation(self, fast_window_analyzer):
        a = fast_window_analyzer
        _feed(a, 240, 240)
        time.sleep(0.06)
        _feed(a, 250)
        s = a.shadow_respawn_summary()
        assert s["max_confirmed_gap_s"] >= 0.05
        assert s["confirmed_gaps_over_threshold"] >= 1


class TestDualMode:
    def _dual(self, **extra):
        overrides = {
            "respawn_detection.mode": "dual",
            "health.death_no_confirmed_s": 0.05,
            "health.death_no_digits_s": 0.05,
        }
        overrides.update(extra)
        return _make_analyzer(**overrides)

    def test_dual_fire_sets_health_respawn_event(self):
        a = self._dual()
        try:
            _feed(a, 240, 240, 0, 0, 0, 250, 250)
            assert a.health_respawn_event.is_set()
            assert len(a._shadow_fires) == 1
            assert a.shadow_respawn_summary()["mode"] == "dual"
        finally:
            a.cleanup()

    def test_dual_stands_down_when_ocr_owns_episode(self):
        a = self._dual()
        try:
            with a._ocr_cache_lock:
                a._ocr_cache['result'] = (True, 1.0, "ocr")
            _feed(a, 240, 240, 0, 0, 0, 250, 250)
            assert not a.health_respawn_event.is_set()
            assert len(a._shadow_fires) == 1   # still scored
        finally:
            a.cleanup()

    def test_dual_stands_down_after_recent_ocr_edge(self):
        """A slow post-overlay health confirm must not double-fire the plumbing
        when OCR already detected this episode (seen in the ADR 044 replay lane)."""
        a = self._dual()
        try:
            a._shadow_record_ocr_respawn(True)   # OCR edge ~now
            a._shadow_record_ocr_respawn(False)
            _feed(a, 240, 240, 0, 0, 0, 250, 250)
            assert not a.health_respawn_event.is_set()  # edge within 20s window
            assert len(a._shadow_fires) == 1            # still scored for the summary
        finally:
            a.cleanup()

    def test_shadow_mode_never_sets_event(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0, 250, 250)
        assert not analyzer.health_respawn_event.is_set()

    def test_battle_exit_clears_pending_event(self):
        a = self._dual()
        try:
            _feed(a, 240, 240, 0, 0, 0, 250, 250)
            assert a.health_respawn_event.is_set()
            a._shadow_clear_mark()
            assert not a.health_respawn_event.is_set()
        finally:
            a.cleanup()


class TestShadowSummary:
    def test_ocr_edge_latency_recorded(self, analyzer):
        """Each OCR rising edge snapshots how stale health evidence already
        was — the headroom measurement gating any ADR 064 extension."""
        _feed(analyzer, 240, 240)          # confirmed read stamps the clock
        time.sleep(0.06)
        analyzer._shadow_record_ocr_respawn(True)
        analyzer._shadow_record_ocr_respawn(False)
        s = analyzer.shadow_respawn_summary()
        assert len(s["ocr_edge_latencies"]) == 1
        assert s["ocr_edge_latencies"][0]["since_confirmed_s"] >= 0.05
        assert s["edge_since_confirmed_max_s"] >= 0.05
        assert s["edge_since_confirmed_mean_s"] >= 0.05

    def test_ocr_edge_latency_none_before_any_confirmed_read(self, analyzer):
        analyzer._shadow_record_ocr_respawn(True)
        s = analyzer.shadow_respawn_summary()
        assert s["ocr_edge_latencies"][0]["since_confirmed_s"] is None
        assert s["edge_since_confirmed_mean_s"] is None
        assert s["edge_since_confirmed_max_s"] is None

    def test_matched_fire_within_5s(self, analyzer):
        analyzer._shadow_record_ocr_respawn(True)   # rising edge now
        _feed(analyzer, 240, 240, 0, 0, 0, 250, 250)
        s = analyzer.shadow_respawn_summary()
        assert s["shadow_fires"] == 1
        assert s["ocr_respawns"] == 1
        assert s["matched"] == 1
        assert s["matched_within_5s"] == 1
        assert s["false_fires"] == 0
        assert s["missed_ocr_respawns"] == 0

    def test_fire_without_edge_is_false_fire(self, analyzer):
        _feed(analyzer, 240, 240, 0, 0, 0, 250, 250)
        s = analyzer.shadow_respawn_summary()
        assert s["false_fires"] == 1
        assert s["matched"] == 0

    def test_edge_without_fire_is_missed(self, analyzer):
        analyzer._shadow_record_ocr_respawn(True)
        analyzer._shadow_record_ocr_respawn(False)
        analyzer._shadow_record_ocr_respawn(True)   # second edge needs prior False
        s = analyzer.shadow_respawn_summary()
        assert s["ocr_respawns"] == 2
        assert s["missed_ocr_respawns"] == 2

    def test_continuous_detection_counts_one_edge(self, analyzer):
        analyzer._shadow_record_ocr_respawn(True)
        analyzer._shadow_record_ocr_respawn(True)   # still high — no new edge
        analyzer._shadow_record_ocr_respawn(True)
        s = analyzer.shadow_respawn_summary()
        assert s["ocr_respawns"] == 1


class TestStatsExtraSection:
    def test_finalize_embeds_extra_section(self, tmp_path):
        t = MissionStatsTracker(version="test", output_dir=str(tmp_path))
        shadow = {"mode": "shadow", "shadow_fires": 3, "false_fires": 0}
        summary = t.finalize(run_id="unittest", extra={"respawn_shadow": shadow})
        assert summary["respawn_shadow"] == shadow

    def test_extra_cannot_clobber_builtin_fields(self, tmp_path):
        t = MissionStatsTracker(version="test", output_dir=str(tmp_path))
        summary = t.finalize(run_id="unittest", extra={"missions_started": 999})
        assert summary["missions_started"] == 0


class TestStartingHealthProbe:
    """ADR 032 battle-alive probe, made reachable 2026-08-05.

    It was dead code: _detect_respawn_ocr returned early for GAME_STARTING before
    scheduling any OCR, so the probe branch never ran (measured: "0 attempts over
    18.8s"). These tests pin that it now runs, and that it stays narrow.
    """

    def _analyzer(self):
        a = _make_analyzer()
        a.state = GameState.GAME_STARTING.name
        return a

    def test_probe_does_not_run_until_armed(self):
        a = self._analyzer()
        try:
            a._detect_respawn_ocr(object())
            assert a._starting_probe_last_ts == 0.0   # never scheduled
        finally:
            a.cleanup()

    def test_arming_allows_the_probe_to_schedule(self):
        a = self._analyzer()
        try:
            a.arm_starting_health_scan()
            assert a._game_starting_health_scan_enabled.is_set()
            assert a._starting_probe_last_ts == 0.0   # throttle reset so first tick probes
        finally:
            a.cleanup()

    def test_probe_never_reports_a_respawn(self):
        """The probe must not leak a stale battle respawn result into GAME_STARTING."""
        a = self._analyzer()
        try:
            with a._ocr_cache_lock:
                a._ocr_cache['result'] = (True, 1.0, "ocr")   # stale battle detection
                a._ocr_cache['timestamp'] = time.time()
            a.arm_starting_health_scan()
            detected, conf, method = a._detect_respawn_ocr(object())
            assert detected is False and conf == 0.0 and method is None
        finally:
            a.cleanup()

    def test_lobby_and_waiting_still_skip_ocr_entirely(self):
        a = _make_analyzer()
        try:
            a.arm_starting_health_scan()   # armed, but wrong state
            for st in (GameState.GAME_LOBBY, GameState.GAME_WAITING):
                a.state = st.name
                a._starting_probe_last_ts = 0.0
                a._detect_respawn_ocr(object())
                assert a._starting_probe_last_ts == 0.0, f"probe must not run in {st.name}"
        finally:
            a.cleanup()

    def test_disarm_reports_a_summary_and_stops_the_probe(self):
        a = self._analyzer()
        try:
            a.arm_starting_health_scan()
            a.disarm_starting_health_scan()
            assert not a._game_starting_health_scan_enabled.is_set()
            a._starting_probe_last_ts = 0.0
            a._detect_respawn_ocr(object())
            assert a._starting_probe_last_ts == 0.0   # disarmed → no scheduling
        finally:
            a.cleanup()


# ---------------------------------------------------------------------------
# ADR 080 — live-flight dropout histogram
# ---------------------------------------------------------------------------

class TestDropoutHistogram:
    """Confirmed-read gaps enter the histogram only when the whole gap ran
    with live telemetry in GAME_BATTLE — death/menu gaps stay out."""

    class _LiveSnap:
        def altitude_fresh(self):
            return True

    class _StaleSnap:
        def altitude_fresh(self):
            return False

    def test_live_gap_lands_in_the_right_bucket(self, analyzer, monkeypatch):
        monkeypatch.setattr(analyzer, "get_telemetry", lambda: self._LiveSnap())
        _feed(analyzer, 240, 240)                       # anchor + clean gap window
        analyzer._last_confirmed_read_ts = time.time() - 7.0
        _feed(analyzer, 240)                            # confirms → closes a ~7s gap
        s = analyzer.health_dropout_summary()
        assert s["buckets"]["5to10s"] == 1
        assert s["over_5s"] == 1
        assert s["max_s"] >= 6.9

    def test_stale_seen_gap_is_excluded(self, analyzer, monkeypatch):
        monkeypatch.setattr(analyzer, "get_telemetry", lambda: self._StaleSnap())
        _feed(analyzer, 240, 240, None)                 # stale sampled mid-gap
        analyzer._last_confirmed_read_ts = time.time() - 7.0
        _feed(analyzer, 240)
        s = analyzer.health_dropout_summary()
        assert s["buckets"]["5to10s"] == 0
        assert s["count"] <= 1                          # only the pre-taint gap, if any

    def test_respawn_reset_taints_the_open_gap(self, analyzer, monkeypatch):
        monkeypatch.setattr(analyzer, "get_telemetry", lambda: self._LiveSnap())
        _feed(analyzer, 240, 240)
        analyzer.reset_health_for_respawn()
        analyzer._last_confirmed_read_ts = time.time() - 12.0
        _feed(analyzer, 240, 240)                       # post-respawn confirm
        s = analyzer.health_dropout_summary()
        assert s["buckets"]["10to20s"] == 0

    def test_non_battle_gap_is_excluded(self, analyzer, monkeypatch):
        monkeypatch.setattr(analyzer, "get_telemetry", lambda: self._LiveSnap())
        _feed(analyzer, 240, 240)
        analyzer.state = GameState.GAME_LOBBY.name
        analyzer._last_confirmed_read_ts = time.time() - 7.0
        _feed(analyzer, 240)
        assert analyzer.health_dropout_summary()["buckets"]["5to10s"] == 0

    def test_summary_shape_when_empty(self, analyzer):
        s = analyzer.health_dropout_summary()
        assert s["count"] == 0 and s["p95_s"] is None and s["max_s"] is None

    def test_gap_accessor(self, analyzer):
        assert analyzer.health_confirmed_gap_s() is None   # no anchor yet
        _feed(analyzer, 240, 240)
        gap = analyzer.health_confirmed_gap_s()
        assert gap is not None and gap < 2.0

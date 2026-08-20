"""Unit tests for MissionStatsTracker."""

import json

import pytest

from wingman.mission_stats import MissionStatsTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tracker(tmp_path):
    return MissionStatsTracker(version="test", output_dir=str(tmp_path))


def _enter_battle(t, ts=0.0):
    t.on_fsm_transition("battle_start", "GAME_LOBBY", "GAME_BATTLE", ts)


def _leave_battle(t, next_state="GAME_END_B", ts=10.0):
    t.on_fsm_transition("end_b", "GAME_BATTLE", next_state, ts)


# ---------------------------------------------------------------------------
# Startup guard
# ---------------------------------------------------------------------------

class TestStartupGuard:
    def test_ignores_battle_before_startup_done(self, tmp_path):
        t = _tracker(tmp_path)
        # GAME_UNKNOWN → GAME_BATTLE: startup not done yet, must not count
        t.on_fsm_transition("x", "GAME_UNKNOWN", "GAME_BATTLE", 0.0)
        result = t.finalize()
        assert result["missions_started"] == 0

    def test_counts_after_startup_done(self, tmp_path):
        t = _tracker(tmp_path)
        # First transition out of GAME_UNKNOWN clears startup hold
        t.on_fsm_transition("x", "GAME_UNKNOWN", "GAME_LOBBY", 0.0)
        _enter_battle(t, ts=1.0)
        _leave_battle(t, ts=5.0)
        result = t.finalize()
        assert result["missions_started"] == 1


# ---------------------------------------------------------------------------
# Mission boundary detection
# ---------------------------------------------------------------------------

class TestMissionBoundary:
    def test_single_complete_mission(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=100.0)
        _leave_battle(t, ts=200.0)
        result = t.finalize()
        assert result["missions_started"] == 1
        assert result["missions"][0]["duration_s"] == pytest.approx(100.0, abs=0.1)

    def test_multiple_missions(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        for i in range(3):
            _enter_battle(t, ts=float(i * 200))
            _leave_battle(t, ts=float(i * 200 + 100))
        result = t.finalize()
        assert result["missions_started"] == 3
        assert len(result["missions"]) == 3

    def test_mission_indices_sequential(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        for i in range(3):
            _enter_battle(t, ts=float(i * 100))
            _leave_battle(t, ts=float(i * 100 + 50))
        result = t.finalize()
        assert [m["index"] for m in result["missions"]] == [0, 1, 2]

    def test_avg_duration(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        _leave_battle(t, ts=100.0)
        _enter_battle(t, ts=200.0)
        _leave_battle(t, ts=400.0)
        result = t.finalize()
        assert result["avg_mission_duration_s"] == pytest.approx(150.0, abs=0.1)


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------

class TestOutcomeClassification:
    def test_click_to_outcome(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("click_to_detected", 5.0)
        _leave_battle(t, next_state="GAME_END_B", ts=10.0)
        result = t.finalize()
        assert result["missions"][0]["outcome"] == "click_to"
        assert result["missions_click_to"] == 1

    def test_click_to_outcome_ordering_race(self, tmp_path):
        """FSM transition fires from background OCR thread before on_event sets _pending_outcome."""
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        # Simulate race: transition arrives with trigger_name="click_to_detected" but
        # _pending_outcome is still None because on_event hasn't run yet.
        t.on_fsm_transition("click_to_detected", "GAME_BATTLE", "GAME_END_B", ts=10.0)
        # on_event fires after (too late — mission already ended)
        t.on_event("click_to_detected", 10.0)
        result = t.finalize()
        assert result["missions"][0]["outcome"] == "click_to"
        assert result["missions_click_to"] == 1

    def test_missiles_empty_outcome(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("missiles_empty", 5.0)
        _leave_battle(t, next_state="GAME_BATTLE", ts=10.0)  # leaves via non-battle transition next cycle
        # Simulate the state machine going to GAME_LOBBY after eject
        t.on_fsm_transition("eject", "GAME_BATTLE", "GAME_LOBBY", 12.0)
        result = t.finalize()
        assert result["missions"][0]["outcome"] == "missiles_empty"
        assert result["missions_missiles_empty"] == 1

    def test_lobby_exit_outcome(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_fsm_transition("waiting_timeout", "GAME_BATTLE", "GAME_LOBBY", 50.0)
        result = t.finalize()
        assert result["missions"][0]["outcome"] == "lobby_exit"
        assert result["missions_lobby_exit"] == 1

    def test_unknown_outcome_on_finalize(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        # No end transition — session ends mid-mission
        result = t.finalize()
        assert result["missions"][0]["outcome"] == "unknown"
        assert result["missions_unknown_outcome"] == 1


# ---------------------------------------------------------------------------
# Event counting
# ---------------------------------------------------------------------------

class TestEventCounting:
    def test_respawn_counted_per_mission_and_session(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("respawn_detected", 1.0)
        t.on_event("respawn_detected", 2.0)
        _leave_battle(t, ts=10.0)
        result = t.finalize()
        assert result["total_respawns"] == 2
        assert result["missions"][0]["respawn_count"] == 2

    def test_flare_burst_counted(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("flare_burst_deployed", 1.0)
        t.on_event("flare_burst_deployed", 2.0)
        t.on_event("flare_burst_deployed", 3.0)
        _leave_battle(t, ts=10.0)
        result = t.finalize()
        assert result["total_flare_bursts"] == 3
        assert result["missions"][0]["flare_burst_count"] == 3

    def test_flare_reload_counted(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("flare_reload", 1.0)
        _leave_battle(t, ts=10.0)
        result = t.finalize()
        assert result["total_flare_reloads"] == 1
        assert result["missions"][0]["flare_reload_count"] == 1

    def test_no_missiles_abort_flag(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("missiles_empty", 5.0)
        _leave_battle(t, next_state="GAME_LOBBY", ts=10.0)
        result = t.finalize()
        assert result["missions"][0]["no_missiles_abort"] is True

    def test_manual_takeover_counted(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_fsm_transition("manual", "GAME_BATTLE", "GAME_BATTLE_MANUAL", 2.0)
        _leave_battle(t, ts=10.0)
        result = t.finalize()
        assert result["total_manual_takeovers"] == 1
        assert result["missions"][0]["manual_takeover_count"] == 1

    def test_eject_excursion_is_one_mission_not_three(self, tmp_path):
        """GAME_BATTLE_EJECT is a mid-mission excursion, not a mission boundary.

        Omitting it from _BATTLE_STATES made every missiles-empty eject end a
        mission and the return from it start a new one: the 2026-07-30 16:27
        session reported 7 missions for 3 real rounds, with a bogus 1m30s
        average.
        """
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_fsm_transition("eject_started", "GAME_BATTLE", "GAME_BATTLE_EJECT", 20.0)
        t.on_fsm_transition("eject_complete", "GAME_BATTLE_EJECT", "GAME_BATTLE", 40.0)
        _leave_battle(t, ts=300.0)
        result = t.finalize()
        assert result["missions_started"] == 1
        assert len(result["missions"]) == 1
        # Duration spans the whole round, not a fragment either side of the eject.
        assert result["missions"][0]["duration_s"] == pytest.approx(300.0)

    def test_takeover_from_eject_state_is_counted(self, tmp_path):
        """Takeover entered from GAME_BATTLE_EJECT must still count.

        3 of the 4 takeovers in the 2026-07-30 16:27 session arrived as
        EJECT -> MANUAL and were dropped, because the counter checked
        _in_mission before the mission-start handling ran.
        """
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_fsm_transition("eject_started", "GAME_BATTLE", "GAME_BATTLE_EJECT", 20.0)
        t.on_fsm_transition("manual_takeover", "GAME_BATTLE_EJECT", "GAME_BATTLE_MANUAL", 25.0)
        _leave_battle(t, next_state="GAME_END_B", ts=60.0)
        result = t.finalize()
        assert result["total_manual_takeovers"] == 1
        assert result["missions"][0]["manual_takeover_count"] == 1

    def test_takeover_that_opens_a_mission_is_counted(self, tmp_path):
        """A takeover can be the transition that starts the mission."""
        t = _tracker(tmp_path)
        t._startup_done = True
        t.on_fsm_transition("manual_takeover", "GAME_LOBBY", "GAME_BATTLE_MANUAL", 0.0)
        t.on_fsm_transition("end_b", "GAME_BATTLE_MANUAL", "GAME_END_B", 30.0)
        result = t.finalize()
        assert result["missions_started"] == 1
        assert result["total_manual_takeovers"] == 1

    def test_repeated_manual_state_does_not_double_count(self, tmp_path):
        """Only the ENTRY into manual counts, not every transition landing there."""
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_fsm_transition("manual_takeover", "GAME_BATTLE", "GAME_BATTLE_MANUAL", 2.0)
        t.on_fsm_transition("noop", "GAME_BATTLE_MANUAL", "GAME_BATTLE_MANUAL", 3.0)
        _leave_battle(t, next_state="GAME_END_B", ts=10.0)
        result = t.finalize()
        assert result["total_manual_takeovers"] == 1

    def test_click_to_finish_after_an_eject_is_not_booked_as_missiles_empty(self, tmp_path):
        """The terminal trigger wins over a stale mid-mission pending outcome.

        Since GAME_BATTLE_EJECT became an in-mission state, "missiles_empty" is
        a mid-mission signal that survives to the end of the round. Checking it
        before trigger_name made the 2026-07-30 18:51 session report
        "Missiles empty 10 (100%), Click-to finish 0" despite 10 logged
        CLICK TO CONTINUE finishes.
        """
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        # Missiles run out mid-round: eject, die, respawn, keep playing.
        t.on_event("missiles_empty", 60.0)
        t.on_fsm_transition("eject_started", "GAME_BATTLE", "GAME_BATTLE_EJECT", 61.0)
        t.on_fsm_transition("eject_complete", "GAME_BATTLE_EJECT", "GAME_BATTLE", 80.0)
        # Round actually ends on the click-to-continue screen.
        t.on_fsm_transition("click_to_detected", "GAME_BATTLE", "GAME_END_B", 300.0)
        result = t.finalize()
        assert result["missions_click_to"] == 1
        assert result["missions_missiles_empty"] == 0
        # The mid-round fact is still recorded on the mission itself.
        assert result["missions"][0]["no_missiles_abort"] is True

    def test_missiles_empty_still_used_when_nothing_supersedes_it(self, tmp_path):
        """_pending_outcome remains the fallback for non-click_to endings."""
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("missiles_empty", 60.0)
        t.on_fsm_transition("some_other_trigger", "GAME_BATTLE", "GAME_UNKNOWN", 120.0)
        result = t.finalize()
        assert result["missions_missiles_empty"] == 1

    def test_events_outside_mission_only_go_to_totals(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        # Events before any mission starts
        t.on_event("respawn_detected", 0.0)
        t.on_event("flare_burst_deployed", 0.0)
        t.on_event("flare_reload", 0.0)
        result = t.finalize()
        assert result["total_respawns"] == 1
        assert result["total_flare_bursts"] == 1
        assert result["total_flare_reloads"] == 1
        assert result["missions_started"] == 0

    def test_events_accumulate_across_missions(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        for _ in range(3):
            _enter_battle(t, ts=0.0)
            t.on_event("respawn_detected", 1.0)
            _leave_battle(t, ts=5.0)
        result = t.finalize()
        assert result["total_respawns"] == 3


# ---------------------------------------------------------------------------
# Session aggregates
# ---------------------------------------------------------------------------

class TestSessionAggregates:
    def test_outcome_counts_sum_to_missions_started(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("click_to_detected", 5.0)
        _leave_battle(t, next_state="GAME_END_B", ts=10.0)
        _enter_battle(t, ts=20.0)
        t.on_event("missiles_empty", 25.0)
        _leave_battle(t, next_state="GAME_LOBBY", ts=30.0)
        result = t.finalize()
        total = (
            result["missions_click_to"]
            + result["missions_missiles_empty"]
            + result["missions_lobby_exit"]
            + result["missions_unknown_outcome"]
        )
        assert total == result["missions_started"]

    def test_zero_missions(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        result = t.finalize()
        assert result["missions_started"] == 0
        assert result["avg_mission_duration_s"] is None
        assert result["missions"] == []


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------

class TestJsonSerialisation:
    def test_json_written_to_output_dir(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        _leave_battle(t, ts=10.0)
        t.finalize(run_id="test_run_001")
        out_dir = tmp_path / "current"
        assert out_dir.exists()
        stats_files = list(out_dir.glob("run_*_stats.json"))
        assert len(stats_files) == 1
        assert stats_files[0].name == "run_test_run_001_stats.json"

    def test_json_content_valid(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        t.on_event("click_to_detected", 5.0)
        _leave_battle(t, next_state="GAME_END_B", ts=50.0)
        t.finalize(run_id="test_run_002")
        stats_file = tmp_path / "current" / "run_test_run_002_stats.json"
        data = json.loads(stats_file.read_text())
        assert data["missions_started"] == 1
        assert data["missions_click_to"] == 1
        assert data["wingman_version"] == "test"
        assert data["run_id"] == "test_run_002"
        assert len(data["missions"]) == 1
        assert data["missions"][0]["outcome"] == "click_to"

    def test_run_id_defaults_to_session_timestamp(self, tmp_path):
        t = _tracker(tmp_path)
        result = t.finalize()
        assert result["run_id"] != ""
        # Should look like YYYYMMDD_HHMMSS
        assert len(result["run_id"]) == 15
        assert result["run_id"][8] == "_"

    def test_finalize_idempotent_mission_count(self, tmp_path):
        t = _tracker(tmp_path)
        t._startup_done = True
        _enter_battle(t, ts=0.0)
        # Session ends mid-mission
        r1 = t.finalize()
        assert r1["missions_started"] == 1
        assert r1["missions"][0]["outcome"] == "unknown"


# ---------------------------------------------------------------------------
# ADR 070 V5 — per-engagement survival
# ---------------------------------------------------------------------------

class TestMissileEngagements:
    """Per-ENGAGEMENT survival, the measure a per-mission death rate cannot
    give: missions mix engagements the evade touched with ones it never saw
    (2026-08-12: 8 evades across 12 missions), so per-mission deaths are
    dominated by deaths the evade had no part in."""

    def test_volley_alerts_group_into_one_engagement(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        # Alerts ~1.5s apart are ONE volley (measured cadence 1.3-1.7s).
        for ts in (100.0, 101.5, 103.0, 104.5):
            t.on_event("flare_burst_deployed", ts)
        eng = t.finalize()["missile_engagements"]
        assert eng["engagements"] == 1
        assert eng["detail"][0]["alerts"] == 4

    def test_separate_volleys_are_separate_engagements(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("flare_burst_deployed", 160.0)   # a minute later
        assert t.finalize()["missile_engagements"]["engagements"] == 2

    def test_evade_attributed_to_its_alert(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("missile_evade", 101.3)          # BT tick lag
        eng = t.finalize()["missile_engagements"]
        assert eng["evaded_total"] == 1
        assert eng["not_evaded_total"] == 0

    def test_death_in_window_counts_against_its_engagement(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("missile_evade", 101.3)
        t.on_event("respawn_detected", 104.0)
        eng = t.finalize()["missile_engagements"]
        assert eng["evaded_died"] == 1
        assert eng["evaded_survival"] == 0.0

    def test_death_outside_window_is_not_attributed(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("respawn_detected", 140.0)       # 40s later, unrelated
        eng = t.finalize()["missile_engagements"]
        assert eng["not_evaded_died"] == 0
        assert eng["not_evaded_survival"] == 1.0

    def test_survival_split_by_evade(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        # Evaded, survived.
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("missile_evade", 101.3)
        # Evaded, died.
        t.on_event("flare_burst_deployed", 200.0)
        t.on_event("missile_evade", 201.3)
        t.on_event("respawn_detected", 205.0)
        # Not evaded, died.
        t.on_event("flare_burst_deployed", 300.0)
        t.on_event("respawn_detected", 304.0)
        eng = t.finalize()["missile_engagements"]
        assert (eng["evaded_total"], eng["evaded_died"]) == (2, 1)
        assert (eng["not_evaded_total"], eng["not_evaded_died"]) == (1, 1)
        assert eng["evaded_survival"] == 0.5
        assert eng["not_evaded_survival"] == 0.0

    def test_no_engagements_reports_none_not_zero(self, tmp_path):
        """No data must not read as 0% survival."""
        t = _tracker(tmp_path)
        _enter_battle(t)
        eng = t.finalize()["missile_engagements"]
        assert eng["engagements"] == 0
        assert eng["evaded_survival"] is None
        assert eng["not_evaded_survival"] is None

    def test_print_summary_with_engagements(self, tmp_path, caplog):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("flare_burst_deployed", 100.0)
        t.on_event("missile_evade", 101.3)
        t.finalize()
        with caplog.at_level("INFO"):
            t.print_summary()
        assert "Missile engagements" in caplog.text


# ---------------------------------------------------------------------------
# ADR 076 — spawn-crash instrument
# ---------------------------------------------------------------------------

class TestSpawnCrashes:
    """Deaths in [min, window] after a post-respawn restart — the
    before/after measure for the ADR 076 spawn-attitude guard. Stamped off
    the existing restart_last_mission event so no new event names enter the
    replay/capture streams. ADR 082 adds the physical floor: the aircraft
    respawns airborne, so a sub-floor death is respawn re-detection churn,
    counted separately rather than dropped."""

    def test_death_soon_after_restart_counts(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 106.0)
        sc = t.finalize()["spawn_crashes"]
        assert sc["count"] == 1
        assert sc["died_after_s"] == [6.0]

    def test_death_outside_window_does_not_count(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 140.0)
        assert t.finalize()["spawn_crashes"]["count"] == 0

    def test_one_candidate_per_life(self, tmp_path):
        """The restart stamp is consumed by the first death — a later death
        without a new restart must not count against the old stamp."""
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 140.0)   # consumed, out of window
        t.on_event("respawn_detected", 141.0)   # no stamp — never counts
        assert t.finalize()["spawn_crashes"]["count"] == 0

    # -- ADR 082: the physical floor ---------------------------------------

    def test_sub_floor_death_is_redetect_not_crash(self, tmp_path):
        """0.2 s after restart: the aircraft cannot have reached terrain —
        this is the 2026-08-19 artifact class (22 events, median 0.2 s)."""
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 100.2)
        sc = t.finalize()["spawn_crashes"]
        assert sc["count"] == 0, "sub-floor death counted as a spawn crash"
        assert sc["immediate_redetects"] == 1
        assert sc["redetect_after_s"] == [0.2]

    def test_just_under_floor_is_redetect(self, tmp_path):
        """2.5 s — the slowest observed artifact — stays below the floor."""
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 102.5)
        sc = t.finalize()["spawn_crashes"]
        assert sc["count"] == 0
        assert sc["immediate_redetects"] == 1

    def test_at_floor_counts_as_crash(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 103.0)
        sc = t.finalize()["spawn_crashes"]
        assert sc["count"] == 1
        assert sc["immediate_redetects"] == 0

    def test_redetect_consumes_the_stamp(self, tmp_path):
        """A sub-floor redetect consumes the restart stamp like any other
        candidate — a later death must not also book against it."""
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 100.1)   # redetect, consumes stamp
        t.on_event("respawn_detected", 105.0)   # no stamp — counts as neither
        sc = t.finalize()["spawn_crashes"]
        assert sc["count"] == 0
        assert sc["immediate_redetects"] == 1

    def test_summary_reports_both_counts(self, tmp_path, caplog):
        import logging
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("restart_last_mission", 100.0)
        t.on_event("respawn_detected", 100.2)
        t.finalize()
        with caplog.at_level(logging.INFO):
            t.print_summary()
        assert "Spawn crashes" in caplog.text
        assert "redetect churn" in caplog.text

    def test_death_with_no_restart_is_not_a_spawn_crash(self, tmp_path):
        t = _tracker(tmp_path)
        _enter_battle(t)
        t.on_event("respawn_detected", 100.0)
        assert t.finalize()["spawn_crashes"]["count"] == 0

"""Unit tests for MissionStatsTracker."""

import json
import time

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
        result = t.finalize(run_id="test_run_001")
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

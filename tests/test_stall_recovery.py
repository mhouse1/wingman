"""ADR 084: gate logic for the stall-recovery crops.

These actions leave squads and dismiss a modal sitting beside an "Exit to
Desktop" button, so the gate — not the detection — is the safety-critical part.
Every test here is about when the gate stays SHUT.

Usage: uv run pytest tests/test_stall_recovery.py -q
"""

import copy
import time
from pathlib import Path

import pytest
import yaml

from wingman.analyzer import (GameStateAnalyzer, GameEvent, GameState,
                              STALL_ACTION_STATES, STALL_RECOVERY_CROPS,
                              STALL_UNREADY_CROP)
from constants import CONFIG_PATH


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


@pytest.fixture
def analyzer():
    a = GameStateAnalyzer(copy.deepcopy(load_config()))
    try:
        yield a
    finally:
        a.cleanup()


class TestStallGate:
    """The gate must stay shut during healthy operation."""

    @pytest.mark.parametrize("state", [GameState.GAME_LOBBY, GameState.GAME_WAITING,
                                       GameState.GAME_BATTLE, GameState.GAME_STARTING,
                                       GameState.GAME_END_B])
    def test_classified_states_never_open_the_gate(self, analyzer, state):
        """Even with the dwell clock maxed out, a classified state acts on nothing."""
        analyzer._stall_state_since = time.time() - 9999.0
        assert analyzer._stall_recovery_targets(state) == []

    @pytest.mark.parametrize("state", STALL_ACTION_STATES)
    def test_stalled_state_without_dwell_is_shut(self, analyzer, state):
        """A GAME_UNKNOWN blip mid-transition must not trigger recovery."""
        analyzer._stall_state_since = time.time()   # just entered
        assert analyzer._stall_recovery_targets(state) == []

    @pytest.mark.parametrize("state", STALL_ACTION_STATES)
    def test_stalled_state_past_dwell_opens_the_gate(self, analyzer, state):
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(state)
        assert targets == list(STALL_RECOVERY_CROPS)

    def test_unset_dwell_clock_is_shut(self, analyzer):
        """_stall_state_since == 0 means 'not stalled', not 'stalled since epoch'."""
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN) == []

    def test_scan_order_is_most_specific_first(self, analyzer):
        """The batch breaks on first hit, so order is behaviour, not style."""
        analyzer._stall_state_since = time.time() - 9999.0
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert targets.index("STALL_RETRY") < targets.index("STALL_AIRCRAFT")


class TestUnreadyGate:
    """STALL_MULTI_PLAYER is timed from the UNREADY read, not from the state."""

    def test_unready_below_dwell_is_shut(self, analyzer):
        analyzer._unready_since = time.time()
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_unready_past_dwell_fires_in_classified_lobby(self, analyzer):
        """The 2026-08-19 22:07 stall: UNREADY blocks PLAY in a lobby that classifies."""
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        analyzer._stall_state_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == [STALL_UNREADY_CROP]

    def test_unready_past_dwell_also_fires_while_unclassified(self, analyzer):
        """UNREADY makes _classify_unknown_state fail, so it strands GAME_UNKNOWN too."""
        analyzer._unready_since = time.time() - (analyzer._stall_unready_dwell_s + 1.0)
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert STALL_UNREADY_CROP in targets
        assert targets[-1] == STALL_UNREADY_CROP   # after the state-gated crops

    def test_cleared_unready_shuts_the_gate(self, analyzer):
        analyzer._unready_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []


class TestCropsConfigured:
    """A missing crop must degrade to 'skip it', never to a KeyError mid-stall."""

    def test_absent_crop_is_dropped_from_targets(self, analyzer):
        analyzer._stall_state_since = time.time() - 9999.0
        analyzer.crops.pop("STALL_RETRY", None)
        assert "STALL_RETRY" not in analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)

    def test_all_four_crops_have_text_matchers(self, analyzer):
        """Coords without `text` silently never match — calibration is not enough."""
        for crop in (*STALL_RECOVERY_CROPS, STALL_UNREADY_CROP):
            assert crop in analyzer.crops, f"{crop} missing from config"
            assert analyzer.crops[crop].text, f"{crop} has no text matchers"


class TestEventWiring:
    def test_stall_recovery_action_event_fans_out(self, analyzer):
        seen = []
        analyzer.subscribe(GameEvent.STALL_RECOVERY_ACTION, seen.append, name="t")
        analyzer.emit(GameEvent.STALL_RECOVERY_ACTION, "STALL_RETRY")
        assert seen == ["STALL_RETRY"]

    def test_stall_states_exclude_lobby_and_waiting(self):
        """Regression guard: widening this tuple re-enables actions during healthy play."""
        assert GameState.GAME_LOBBY not in STALL_ACTION_STATES
        assert GameState.GAME_WAITING not in STALL_ACTION_STATES
        assert set(STALL_ACTION_STATES) == {GameState.GAME_UNKNOWN,
                                            GameState.GAME_STARTING_STALLED}

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

    def test_lobby_blackout_past_dwell_opens_only_the_exit_dialog(self, analyzer):
        """ADR 087: a sustained lobby blackout may cancel the Exit dialog.

        The ESC pressed on every LOBBY_STALL beat is what opens "Exit to
        Desktop"; GAME_LOBBY is not in STALL_ACTION_STATES, so its crop was
        never scanned and wingman deadlocked against a modal it created
        (2026-08-21: 8 minutes, 187 blank cycles). Only that one crop opens —
        STALL_RETRY and STALL_AIRCRAFT remain gated on an unclassifiable state.
        """
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_LOBBY)
        assert targets == ["STALL_EXIT_TO_DESKTOP"], \
            f"lobby blackout must open exactly the exit dialog, got {targets}"

    def test_lobby_blackout_within_dwell_is_shut(self, analyzer):
        """A brief blank patch between lobby frames is not a stall."""
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = time.time()
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_unset_lobby_blackout_clock_is_shut(self, analyzer):
        analyzer._stall_state_since = 0.0
        analyzer._lobby_blackout_since = 0.0
        assert analyzer._stall_recovery_targets(GameState.GAME_LOBBY) == []

    def test_lobby_blackout_does_not_duplicate_in_a_real_stall(self, analyzer):
        """Both gates open at once: the crop must appear exactly once."""
        analyzer._stall_state_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        analyzer._lobby_blackout_since = time.time() - (analyzer._stall_action_after_s + 1.0)
        targets = analyzer._stall_recovery_targets(GameState.GAME_UNKNOWN)
        assert targets.count("STALL_EXIT_TO_DESKTOP") == 1, targets

    def test_exit_dialog_flag_suppresses_and_lapses(self, analyzer):
        """ADR 087: every ESC source stands down while the modal is up.

        ESC is what OPENS Exit-to-Desktop, so a press while it is on screen
        re-opens what the Cancel click just closed — the 2026-08-21 deadlock,
        where three ESC sources fought one 20s-cooldown recovery for 25
        minutes. The flag must also lapse on its own so a cleared dialog does
        not suppress ESC forever.
        """
        assert analyzer.exit_dialog_visible() is False, "unset flag must not suppress"
        analyzer._exit_dialog_seen_ts = time.time()
        assert analyzer.exit_dialog_visible() is True
        analyzer._exit_dialog_seen_ts = time.time() - 60.0
        assert analyzer.exit_dialog_visible() is False, "stale flag must lapse"

    def test_blackout_flag_gates_esc_independently_of_the_dialog(self, analyzer):
        """ADR 087 addendum 3: ESC is gated on the blackout, not the dialog.

        A single 'not found' scan clears the dialog flag, and the ESC that then
        fired re-created the dialog 1.4s later — a 23s cancel-then-reopen cycle
        (2026-08-21 10:48). The blackout outlasts those gaps, so it is the
        correct gate.
        """
        analyzer._lobby_blackout_since = 0.0
        assert analyzer.lobby_blackout_active() is False
        analyzer._lobby_blackout_since = time.time()
        analyzer._exit_dialog_seen_ts = 0.0      # dialog momentarily "not found"
        assert analyzer.exit_dialog_visible() is False
        assert analyzer.lobby_blackout_active() is True, \
            "ESC must stay suppressed across a momentary dialog miss"

    def test_click_to_suppression_yields_to_a_lobby_blackout(self, analyzer):
        """ADR 087 addendum 4: a forced GAME_LOBBY must not blind click-to OCR.

        `GAME_END_B timeout — forcing recovery to GAME_LOBBY` can assert a state
        the game is not in. The click-to scan self-suppresses in GAME_LOBBY, so
        the forced state disabled the one detector that clears the screen
        holding it there: on 2026-08-21 the post-match PERFORMANCE panel with
        "Click to Continue..." went unread for 17 minutes.

        The suppression must hold in a healthy lobby (2026-07-30 double
        click-through) and yield during a blackout.
        """
        def suppressed(state):
            # Mirrors the guard in _run_click_to_in_background.
            return not (state == GameState.GAME_LOBBY
                        and analyzer.lobby_blackout_active())

        analyzer._lobby_blackout_since = 0.0
        assert suppressed(GameState.GAME_LOBBY), \
            "healthy lobby must keep the 2026-07-30 suppression"
        analyzer._lobby_blackout_since = time.time()
        assert not suppressed(GameState.GAME_LOBBY), \
            "blackout must re-enable click-to OCR"
        assert suppressed(GameState.GAME_END_B), \
            "GAME_END_B suppression is unconditional"

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

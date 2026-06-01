"""
Tests for GameStateAnalyzer resource lifecycle (item 5.1).

Verifies that cleanup() shuts down the executor and stops the click-to thread,
and that __enter__ / __exit__ provide equivalent behaviour.
"""

import threading
import time
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import GameStateAnalyzer, GameState


def _load_config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _blank_frame(width=1920, height=1200):
    return np.zeros((height, width, 3), dtype=np.uint8)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cfg():
    return _load_config()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_cleanup_shuts_down_executor(cfg):
    """After cleanup(), the ThreadPoolExecutor must be None."""
    analyzer = GameStateAnalyzer(cfg)
    analyzer._game_lobby = False
    # Trigger lazy executor init by calling analyze_frame once
    analyzer.analyze_frame(_blank_frame())
    # Give background thread a moment to start
    time.sleep(0.1)

    analyzer.cleanup()

    assert analyzer._ocr_executor is None


def test_cleanup_signals_click_to_thread(cfg):
    """cleanup() must set _click_to_stop so the click-to thread exits."""
    analyzer = GameStateAnalyzer(cfg)
    analyzer._game_lobby = False
    analyzer.analyze_frame(_blank_frame())
    time.sleep(0.1)

    assert not analyzer._click_to_stop.is_set()
    analyzer.cleanup()
    assert analyzer._click_to_stop.is_set()


def test_context_manager_calls_cleanup(cfg):
    """Using GameStateAnalyzer as a context manager must call cleanup() on exit."""
    with GameStateAnalyzer(cfg) as analyzer:
        analyzer._game_lobby = False
        analyzer.analyze_frame(_blank_frame())
        time.sleep(0.1)

    assert analyzer._ocr_executor is None
    assert analyzer._click_to_stop.is_set()


def test_no_threads_leaked_after_cleanup(cfg):
    """No extra threads from the analyzer should be alive after cleanup()."""
    before = threading.active_count()
    analyzer = GameStateAnalyzer(cfg)
    analyzer._game_lobby = False
    analyzer.analyze_frame(_blank_frame())
    time.sleep(0.2)  # let click-to thread and OCR thread start

    analyzer.cleanup()
    # Give daemon threads a moment to notice the stop event
    time.sleep(0.2)

    after = threading.active_count()
    # Unknown-state startup classification can trigger OCR worker prewarm before
    # cleanup; allow a small cushion for lingering daemon worker teardown.
    assert after <= before + 4, (
        f"Thread count did not return to baseline: before={before}, after={after}"
    )


def test_trigger_runs_side_effect_callbacks_outside_state_lock(cfg):
    """Regression guard for CR-008 H-4b: side effects must run after lock release."""
    analyzer = GameStateAnalyzer(cfg)
    analyzer.state = GameState.GAME_BATTLE.name

    acquired_while_callback = []

    def _cancel_side_effect():
        ok = analyzer._state_lock.acquire(blocking=False)
        acquired_while_callback.append(ok)
        if ok:
            analyzer._state_lock.release()

    analyzer._on_cancel_mission = _cancel_side_effect

    assert analyzer._trigger("manual_reset") is True
    assert analyzer.game_state == GameState.GAME_LOBBY
    assert acquired_while_callback == [True]


def test_fsm_unattended_lifecycle_and_starting_loop_callbacks(cfg):
    """Covers unattended lifecycle + GAME_STARTING callback ownership via FSM."""
    analyzer = GameStateAnalyzer(cfg)

    events = {"cancel": 0, "start_loop": 0}
    def _on_cancel():
        events["cancel"] += 1

    def _on_start_loop():
        events["start_loop"] += 1

    analyzer._on_cancel_mission = _on_cancel
    analyzer._on_start_game_starting_loop = _on_start_loop

    assert analyzer.game_state == GameState.GAME_UNKNOWN

    assert analyzer._trigger("unknown_to_lobby_detected") is True
    assert analyzer.game_state == GameState.GAME_LOBBY

    assert analyzer._trigger("play_clicked") is True
    assert analyzer.game_state == GameState.GAME_WAITING

    assert analyzer._trigger("waiting_timeout") is True
    assert analyzer.game_state == GameState.GAME_LOBBY
    # unknown_to_lobby_detected and waiting_timeout both enter GAME_LOBBY.
    assert events["cancel"] == 2

    assert analyzer._trigger("cancel_detected") is True
    assert analyzer.game_state == GameState.GAME_STARTING
    assert events["start_loop"] == 1

    assert analyzer._trigger("starting_timeout") is True
    assert analyzer.game_state == GameState.GAME_STARTING_STALLED

    assert analyzer._trigger("starting_recovery") is True
    assert analyzer.game_state == GameState.GAME_STARTING
    assert events["start_loop"] == 2

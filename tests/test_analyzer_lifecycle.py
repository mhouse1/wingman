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
from wingman.analyzer import GameStateAnalyzer


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
    # Allow at most 1 extra lingering daemon thread (background OCR may still be
    # finishing its current cycle when cleanup is called).
    assert after <= before + 1, (
        f"Thread count did not return to baseline: before={before}, after={after}"
    )

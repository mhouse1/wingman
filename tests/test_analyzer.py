"""
Pytest unit tests for GameStateAnalyzer.
Usage: uv run pytest tests/test_analyzer.py -q -rs --html=tests/test-output/report.html --self-contained-html
"""

from pathlib import Path
import time

import cv2
import numpy as np
import pytest
import yaml
import wingman.analyzer as analyzer_module

from wingman.analyzer import GameStateAnalyzer, GameState, _respawn_text_matches
from constants import (
    CONFIG_PATH,
    TEST_SCREENSHOT,
    TEST_SCREENSHOT_B,
    TEST_SCREENSHOT_C,
    TEST_SCREENSHOT_D,
)


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


@pytest.fixture
def analyzer() -> GameStateAnalyzer:
    a = GameStateAnalyzer(load_config())
    a.state = GameState.GAME_BATTLE.name  # Tests use static screenshots; force GAME_BATTLE
    try:
        yield a
    finally:
        a.cleanup()


@pytest.fixture
def require_easyocr():
    pytest.importorskip("easyocr", reason="EasyOCR not installed")


@pytest.fixture
def require_analyzer_easyocr(require_easyocr):
    if analyzer_module.easyocr is None:
        pytest.skip("wingman.analyzer EasyOCR backend unavailable in current environment")


def _run_respawn_ocr_detection(analyzer: GameStateAnalyzer, frame, attempts: int = 3):
    """Run bounded OCR attempts and return the first OCR-backed respawn state.

    EasyOCR on the discolored respawn fixture is occasionally nondeterministic on the
    first async pass, so tests poll via a few full cache-reset attempts instead of
    assuming a single background worker completion is sufficient.
    """
    last_state = analyzer._empty_state()
    for _ in range(attempts):
        analyzer.reset_cache()
        analyzer.analyze_frame(frame)

        if analyzer._background_ocr_thread and analyzer._background_ocr_thread.is_alive():
            analyzer._background_ocr_thread.join(timeout=30)

        last_state = analyzer.analyze_frame(frame)
        if last_state["respawn_method"] == "ocr":
            return last_state

    return last_state


def _load_image(image_path: Path):
    frame = cv2.imread(str(image_path))
    assert frame is not None, f"Could not load image: {image_path}"
    return frame



@pytest.mark.parametrize(
    "image_path, description",
    [
        (TEST_SCREENSHOT, "normal quality"),
        (TEST_SCREENSHOT_C, "discolored - tests OCR robustness"),
    ],
)
def test_respawn_detection_positive(analyzer: GameStateAnalyzer, require_analyzer_easyocr, image_path: Path, description: str):
    frame = _load_image(image_path)

    state = _run_respawn_ocr_detection(analyzer, frame)

    assert state["is_respawning"] is True, f"Failed to detect RESPA in {description} image"
    assert state["respawn_method"] == "ocr"
    assert state["respawn_confidence"] > 0.0


@pytest.mark.parametrize(
    "image_path",
    [
        TEST_SCREENSHOT_B,  # No respawn text
        TEST_SCREENSHOT_D,  # Contains "natethegreat" text, should fail Levenshtein matching
    ],
)
def test_respawn_detection_negative(analyzer: GameStateAnalyzer, require_analyzer_easyocr, image_path: Path):
    frame = _load_image(image_path)
    analyzer.reset_cache()
    
    # First call schedules background OCR
    state = analyzer.analyze_frame(frame)
    
    # Wait for background OCR thread to complete
    if analyzer._background_ocr_thread and analyzer._background_ocr_thread.is_alive():
        analyzer._background_ocr_thread.join(timeout=30)
    
    # Re-analyze to get updated cache result
    state = analyzer.analyze_frame(frame)

    assert state["is_respawning"] is False


# ---------------------------------------------------------------------------
# _respawn_text_matches — tests for the function actually used in detection
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text_clean, expected",
    [
        # --- must detect (real OCR outputs from the respawn screen) ---
        ("REPA",   True),   # EasyOCR at 0.7x scale reads this from RESPAWN.png;
                            # raising min-length to 5 would silently break detection
        ("RESPA",  True),   # Exact in-game label
        ("RESP",   True),   # 4-char read, last char dropped
        ("RESPAW", True),   # 6-char read within tolerance
        ("RESPAWN", True),  # Full word
        # --- must NOT detect (unrelated text) ---
        ("GREAT",  False),
        ("NATETHEGREAT", False),
        ("RPA",    False),  # Too short (< 4 chars)
        ("",       False),
    ],
)
def test_respawn_text_matches(text_clean: str, expected: bool):
    """Guards the actual detection logic used by _process_respawn_region.

    _is_respawn_text is dead code; this test covers _respawn_text_matches which
    is the function that runs inside the OCR worker process.
    """
    assert _respawn_text_matches(text_clean) is expected


# ---------------------------------------------------------------------------
# _game_starting state blocks respawn OCR
# ---------------------------------------------------------------------------

def test_game_starting_blocks_respawn_detection(analyzer: GameStateAnalyzer):
    """Respawn must not be reported while in GAME_STARTING state.

    Regression guard: commit 8ba01c9 added GAME_STARTING but there was no
    timeout on the loop.  If Good Luck detection fails, _game_starting stays
    True forever and all respawn OCR is silently skipped.
    """
    analyzer.state = GameState.GAME_STARTING.name
    assert analyzer.game_state == GameState.GAME_STARTING

    # Seed the OCR cache as if a respawn was previously detected
    with analyzer._ocr_cache_lock:
        analyzer._ocr_cache["result"] = (True, 1.0, "ocr")
        analyzer._ocr_cache["timestamp"] = time.time()

    frame = _load_image(TEST_SCREENSHOT)
    state = analyzer.analyze_frame(frame)

    assert state["is_respawning"] is False, (
        "Respawn should be suppressed in GAME_STARTING — "
        "if this fails, _game_starting is no longer blocking OCR"
    )


def test_game_battle_does_not_block_respawn_detection(analyzer: GameStateAnalyzer, require_analyzer_easyocr):
    """Respawn cache result must be surfaced normally in GAME_BATTLE state."""
    assert analyzer.game_state == GameState.GAME_BATTLE

    with analyzer._ocr_cache_lock:
        analyzer._ocr_cache["result"] = (True, 1.0, "ocr")
        analyzer._ocr_cache["timestamp"] = time.time()

    frame = _load_image(TEST_SCREENSHOT)
    state = analyzer.analyze_frame(frame)

    assert state["is_respawning"] is True


def test_game_end_b_blocks_background_ocr_scheduling(analyzer: GameStateAnalyzer):
    """GAME_END_B must skip OCR scheduling, including INCOMING region work."""
    analyzer.state = GameState.GAME_END_B.name
    assert analyzer.game_state == GameState.GAME_END_B

    frame = _load_image(TEST_SCREENSHOT)
    result = analyzer._detect_respawn_ocr(frame)

    assert result == (False, 0.0, None)
    assert analyzer._background_ocr_running is False
    assert analyzer._background_ocr_thread is None


def test_waiting_cancel_baseline_capture_and_diff(analyzer: GameStateAnalyzer):
    if "CANCEL" not in analyzer.crops:
        pytest.skip("CANCEL crop not configured")

    h = 3600
    w = 3200
    frame_a = np.zeros((h, w, 3), dtype=np.uint8)
    frame_b = np.zeros((h, w, 3), dtype=np.uint8)

    x1, y1, x2, y2 = analyzer.crops["CANCEL"][:4]
    x1 = int(x1 * w)
    x2 = int(x2 * w)
    y1 = int(y1 * h)
    y2 = int(y2 * h)
    frame_b[y1:y2, x1:x2] = 255

    assert analyzer.capture_waiting_cancel_baseline(frame_a) is True

    diff_same = analyzer.compute_waiting_cancel_diff(frame_a)
    diff_changed = analyzer.compute_waiting_cancel_diff(frame_b)

    assert diff_same is not None
    assert diff_changed is not None
    assert diff_same <= 0.01
    assert diff_changed > diff_same


def test_waiting_cancel_diff_none_without_baseline(analyzer: GameStateAnalyzer):
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    with analyzer._waiting_cancel_baseline_lock:
        analyzer._waiting_cancel_baseline_gray = None
        analyzer._waiting_cancel_baseline_shape = None

    diff = analyzer.compute_waiting_cancel_diff(frame)
    assert diff is None

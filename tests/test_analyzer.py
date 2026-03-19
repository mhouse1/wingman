"""
Pytest unit tests for GameStateAnalyzer.
Usage: uv run pytest tests/test_analyzer.py -q -rs --html=tests/test-output/report.html --self-contained-html
"""

from pathlib import Path
import time

import cv2
import pytest
import yaml

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
    a._game_lobby = False  # Tests use static screenshots; force GAME_BATTLE
    return a


@pytest.fixture
def require_easyocr():
    pytest.importorskip("easyocr", reason="EasyOCR not installed")


def _load_image(image_path: Path):
    frame = cv2.imread(str(image_path))
    assert frame is not None, f"Could not load image: {image_path}"
    return frame


def test_run_ocr_in_background(analyzer: GameStateAnalyzer, require_easyocr):
    frame = _load_image(TEST_SCREENSHOT)

    start_time = time.time()
    analyzer._background_ocr_frame = frame
    analyzer._run_ocr_in_background()
    elapsed = time.time() - start_time

    with analyzer._ocr_cache_lock:
        is_respawning, confidence, method = analyzer._ocr_cache["result"]

    assert is_respawning is True
    assert confidence >= 1.0
    assert method == "ocr"
    assert elapsed >= 0.0


@pytest.mark.parametrize(
    "image_path, description",
    [
        (TEST_SCREENSHOT, "normal quality"),
        (TEST_SCREENSHOT_C, "discolored - tests OCR robustness"),
    ],
)
def test_respawn_detection_positive(analyzer: GameStateAnalyzer, require_easyocr, image_path: Path, description: str):
    frame = _load_image(image_path)
    
    # First call schedules background OCR
    state = analyzer.analyze_frame(frame)
    
    # Wait for background OCR thread to complete
    if analyzer._background_ocr_thread and analyzer._background_ocr_thread.is_alive():
        analyzer._background_ocr_thread.join(timeout=30)
    
    # Re-analyze to get updated cache result
    state = analyzer.analyze_frame(frame)

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
def test_respawn_detection_negative(analyzer: GameStateAnalyzer, require_easyocr, image_path: Path):
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


@pytest.mark.parametrize(
    "text_clean, expected",
    [
        ("RESPA", True),      # Exact match (actual in-game text)
        ("RESP", True),       # Partial match
        ("REPA", True),       # Common OCR error (missing 'S')
        ("RESPTA", True),     # Levenshtein distance 1
        ("RESLA", True),      # Levenshtein distance 2
        ("NATETHEGREAT", False),
        ("GREAT", False),
        ("", False),
    ],
)
def test_is_respawn_text_matching(text_clean: str, expected: bool):
    assert GameStateAnalyzer._is_respawn_text(text_clean) is expected


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
    analyzer._game_starting = True
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


def test_game_battle_does_not_block_respawn_detection(analyzer: GameStateAnalyzer):
    """Respawn cache result must be surfaced normally in GAME_BATTLE state."""
    assert analyzer.game_state == GameState.GAME_BATTLE

    with analyzer._ocr_cache_lock:
        analyzer._ocr_cache["result"] = (True, 1.0, "ocr")
        analyzer._ocr_cache["timestamp"] = time.time()

    frame = _load_image(TEST_SCREENSHOT)
    state = analyzer.analyze_frame(frame)

    assert state["is_respawning"] is True

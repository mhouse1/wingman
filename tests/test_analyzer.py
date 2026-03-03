"""
Pytest unit tests for GameStateAnalyzer.
Usage: uv run pytest tests/test_analyzer.py -q -rs --html=tests/test-output/report.html --self-contained-html
"""

from pathlib import Path
import time

import cv2
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "wingman" / "config.yaml"
RESPAWN_IMAGE = PROJECT_ROOT / "test_screenshots" / "RESPAWN.png"
RESPAWN_B_IMAGE = PROJECT_ROOT / "test_screenshots" / "RESPAWNB.png"
RESPAWNC_IMAGE = PROJECT_ROOT / "test_screenshots" / "RESPAWNC.png"
RESPAWND_IMAGE = PROJECT_ROOT / "test_screenshots" / "RESPAWND.png"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


@pytest.fixture
def analyzer() -> GameStateAnalyzer:
    return GameStateAnalyzer(load_config())


@pytest.fixture
def require_easyocr():
    pytest.importorskip("easyocr", reason="EasyOCR not installed")


def _load_image(image_path: Path):
    frame = cv2.imread(str(image_path))
    assert frame is not None, f"Could not load image: {image_path}"
    return frame


def test_run_ocr_in_background(analyzer: GameStateAnalyzer, require_easyocr):
    frame = _load_image(RESPAWN_IMAGE)

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
        (RESPAWN_IMAGE, "normal quality"),
        (RESPAWNC_IMAGE, "discolored - tests OCR robustness"),
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
        RESPAWN_B_IMAGE,  # No respawn text
        RESPAWND_IMAGE,   # Contains "natethegreat" text, should fail Levenshtein matching
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

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

from wingman.analyzer import GameStateAnalyzer
import wingman.analyzer as analyzer_module
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
    return GameStateAnalyzer(load_config())


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


def test_incoming_cache_cooldown_starts_after_ocr_completion(monkeypatch):
    """Ensure incoming OCR cooldown uses completion timestamp, not start timestamp."""
    config = load_config()
    analyzer = GameStateAnalyzer(config)

    # Force OCR path to be active for this unit test without depending on easyocr import state.
    monkeypatch.setattr(analyzer_module, "easyocr", object())

    class FakeReader:
        def __init__(self):
            self.calls = 0

        def readtext(self, *args, **kwargs):
            self.calls += 1
            return ["MING"]

    fake_reader = FakeReader()
    analyzer._ocr_reader = fake_reader

    # Cooldown intentionally short for deterministic checks.
    analyzer._incoming_cache["cooldown"] = 1.0

    # Simulated clock:
    # - 100.0: first call start
    # - 102.0: first call completion timestamp
    # - 102.1: immediate second call start (should hit cache)
    times = iter([100.0, 102.0, 102.1])

    def fake_time():
        try:
            return next(times)
        except StopIteration:
            return 102.1

    monkeypatch.setattr(analyzer_module.time, "time", fake_time)

    frame = np.zeros((120, 320, 3), dtype=np.uint8)

    # First call performs OCR and populates cache.
    first = analyzer._detect_label_ocr_cached(
        frame,
        target_text="MING",
        cache=analyzer._incoming_cache,
        cache_lock=analyzer._incoming_cache_lock,
        use_ocr=True,
        debug_prefix="incoming",
    )

    # Immediate second call should reuse cache (no second OCR invocation).
    second = analyzer._detect_label_ocr_cached(
        frame,
        target_text="MING",
        cache=analyzer._incoming_cache,
        cache_lock=analyzer._incoming_cache_lock,
        use_ocr=True,
        debug_prefix="incoming",
    )

    assert first[0] is True
    assert second[0] is True
    assert fake_reader.calls == 1, "Expected cache hit; OCR should run once"

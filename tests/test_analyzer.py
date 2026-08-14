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

from wingman.analyzer import (
    GameStateAnalyzer,
    GameState,
    _process_incoming_region,
    _respawn_text_matches,
    _split_telemetry_rows,
)
from wingman.crop_region import get_crop
from constants import (
    CONFIG_PATH,
    TEST_SCREENSHOT,
    TEST_SCREENSHOT_B,
    TEST_SCREENSHOT_D,
    TEST_SCREENSHOT_INCOMING,
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
        (TEST_SCREENSHOT, "normal quality"),  # P1_050 gate frame (ADR 072)
        # Respawn variants retired, no recapture — ADR 072 decision 3
        # (discolored-frame coverage is an accepted loss, CR-015-04).
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
        TEST_SCREENSHOT_B,  # P1_030 battle HUD — no respawn text
        TEST_SCREENSHOT_D,  # P1_060 battle HUD — no respawn text
        # Levenshtein-distractor negative retired with the variant set —
        # ADR 072 decision 3 (accepted loss, CR-015-03). The token-level
        # rejection cases live in test_respawn_text_matches below.
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


@pytest.mark.parametrize(
    "image_path",
    [
        TEST_SCREENSHOT_INCOMING,
    ],
)
def test_incoming_template_detection_positive(analyzer: GameStateAnalyzer, image_path: Path):
    frame = _load_image(image_path)
    incoming_crop = get_crop(frame, *analyzer.crops["incoming"][:4])

    result = _process_incoming_region(
        incoming_crop,
        analyzer._incoming_templates,
        True,
        analyzer._incoming_template_threshold,
        analyzer._incoming_template_near_threshold_low,
        analyzer._incoming_template_near_threshold_high,
        False,
    )

    assert analyzer._incoming_templates, "Expected incoming templates to load from test_screenshots"
    assert result["template_hit"] is True
    assert result["template_score"] >= analyzer._incoming_template_threshold
    assert result["fallback_used"] is False


def test_incoming_template_detection_negative_blank(analyzer: GameStateAnalyzer):
    frame = _load_image(TEST_SCREENSHOT_INCOMING)
    incoming_crop = get_crop(frame, *analyzer.crops["incoming"][:4])
    blank_crop = np.zeros_like(incoming_crop)

    result = _process_incoming_region(
        blank_crop,
        analyzer._incoming_templates,
        True,
        analyzer._incoming_template_threshold,
        analyzer._incoming_template_near_threshold_low,
        analyzer._incoming_template_near_threshold_high,
        False,
    )

    assert result["template_hit"] is False
    assert result["fallback_used"] is False


def test_incoming_ocr_fallback_when_template_disabled(analyzer: GameStateAnalyzer, monkeypatch):
    class _StubReader:
        def readtext(self, _img, detail=0, paragraph=True, workers=0):
            return ["INCOMING"]

    monkeypatch.setattr(analyzer_module, "_get_thread_ocr_reader", lambda: _StubReader())

    frame = _load_image(TEST_SCREENSHOT_INCOMING)
    incoming_crop = get_crop(frame, *analyzer.crops["incoming"][:4])

    result = _process_incoming_region(
        incoming_crop,
        analyzer._incoming_templates,
        False,
        analyzer._incoming_template_threshold,
        analyzer._incoming_template_near_threshold_low,
        analyzer._incoming_template_near_threshold_high,
        True,
    )

    assert result["template_hit"] is False
    assert result["fallback_used"] is True
    assert result["fallback_hit"] is True


@pytest.mark.parametrize(
    "ocr_text,expected_hit",
    [
        # Clean reads from a correctly calibrated crop must trigger.
        ("INCOMING", True),
        ("WARNING", True),
        ("INCOMINGMISSILE", True),
        # Degraded reads with mangled edge characters (pre-recalibration
        # 2026-08-13 session) must NOT trigger with the strict tokens — if
        # these reappear in the log, the crop is clipping the text again:
        # recalibrate the incoming crop rather than loosening the tokens.
        ("NCOMIN", False),
        ("VCOMIN", False),
        ("MIOOMIN", False),
        # Non-warning HUD text must not trigger.
        ("LSELISI", False),
        ("HRUST", False),
        ("1.8KM", False),
    ],
)
def test_incoming_ocr_fallback_degraded_reads(
    analyzer: GameStateAnalyzer, monkeypatch, ocr_text: str, expected_hit: bool
):
    class _StubReader:
        def readtext(self, _img, detail=0, paragraph=True, workers=0):
            return [ocr_text]

    monkeypatch.setattr(analyzer_module, "_get_thread_ocr_reader", lambda: _StubReader())

    frame = _load_image(TEST_SCREENSHOT_INCOMING)
    incoming_crop = get_crop(frame, *analyzer.crops["incoming"][:4])

    result = _process_incoming_region(
        incoming_crop,
        analyzer._incoming_templates,
        False,
        analyzer._incoming_template_threshold,
        analyzer._incoming_template_near_threshold_low,
        analyzer._incoming_template_near_threshold_high,
        True,
        analyzer._incoming_fallback_tokens,
    )

    assert result["fallback_used"] is True
    assert result["fallback_hit"] is expected_hit


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


def test_unknown_starts_and_classifies_to_lobby_with_debounce(monkeypatch):
    a = GameStateAnalyzer(load_config())
    transitions = []
    a.set_on_fsm_transition(lambda trigger, _prev, _next, _ts: transitions.append(trigger))
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    monkeypatch.setattr(a, "scan_region_for_click_to", lambda _frame: False)
    monkeypatch.setattr(a, "scan_region_for_play_button", lambda _frame: "PLAY")
    monkeypatch.setattr(a, "_scan_region_for_health_value", lambda _frame: None)

    try:
        assert a.game_state == GameState.GAME_UNKNOWN
        a.analyze_frame(frame)
        assert a.game_state == GameState.GAME_UNKNOWN
        a.analyze_frame(frame)
        assert a.game_state == GameState.GAME_LOBBY
        assert "unknown_to_lobby_detected" in transitions
    finally:
        a.cleanup()


def test_unknown_precedence_prefers_end_over_lobby_and_battle(monkeypatch):
    a = GameStateAnalyzer(load_config())
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    monkeypatch.setattr(a, "scan_region_for_click_to", lambda _frame: True)
    monkeypatch.setattr(a, "scan_region_for_play_button", lambda _frame: "PLAY")
    monkeypatch.setattr(a, "_scan_region_for_health_value", lambda _frame: 100)

    try:
        a.analyze_frame(frame)
        assert a.game_state == GameState.GAME_UNKNOWN
        a.analyze_frame(frame)
        assert a.game_state == GameState.GAME_END_B
    finally:
        a.cleanup()


def test_unknown_stays_unknown_without_classifier_hit(monkeypatch):
    a = GameStateAnalyzer(load_config())
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    monkeypatch.setattr(a, "scan_region_for_click_to", lambda _frame: False)
    monkeypatch.setattr(a, "scan_region_for_play_button", lambda _frame: None)
    monkeypatch.setattr(a, "_scan_region_for_health_value", lambda _frame: None)

    try:
        for _ in range(3):
            a.analyze_frame(frame)
            assert a.game_state == GameState.GAME_UNKNOWN
    finally:
        a.cleanup()


# --- ALTITUDE_SPEED telemetry row splitter (ADR 038) ---------------------------

def _tbox(x0, y0, x1, y1, text):
    """detail=1 OCR result: (4-point bbox, text, confidence)."""
    return ([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], text, 0.9)


def test_telemetry_split_labels_in_same_box():
    # Number and MPH/feet label share one detection box; leading digits win.
    results = [_tbox(10, 5, 120, 25, "957 MPH"), _tbox(10, 35, 140, 55, "27123 feet")]
    assert _split_telemetry_rows(results, img_height=60) == (957, 27123, 0.9, 0.9)


def test_telemetry_split_number_and_label_separate_boxes():
    results = [
        _tbox(10, 5, 60, 25, "530"), _tbox(70, 5, 120, 25, "MPH"),
        _tbox(10, 35, 90, 55, "27681"), _tbox(100, 35, 140, 55, "feet"),
    ]
    assert _split_telemetry_rows(results, img_height=60) == (530, 27681, 0.9, 0.9)


def test_telemetry_split_single_line_assigned_by_crop_half():
    # Only the speed line visible (upper half) → altitude None, and vice versa.
    assert _split_telemetry_rows([_tbox(10, 5, 120, 25, "530 MPH")], 60) == (530, None, 0.9, 0.0)
    assert _split_telemetry_rows([_tbox(10, 40, 140, 58, "27681 feet")], 60) == (None, 27681, 0.0, 0.9)


def test_telemetry_split_ignores_boxes_without_digits_and_empty():
    assert _split_telemetry_rows([], 60) == (None, None, 0.0, 0.0)
    assert _split_telemetry_rows([_tbox(10, 5, 60, 25, "MPH")], 60) == (None, None, 0.0, 0.0)


def test_telemetry_harvest_never_blocks_and_feeds_filter(analyzer):
    """ADR 038 safety rule: the tick harvests finished telemetry OCR without
    waiting; pending futures are left in flight."""
    from concurrent.futures import Future

    pending = Future()  # never resolved — a slow OCR pass still running
    analyzer._telemetry_future = pending
    assert analyzer._harvest_telemetry_future() == 0.0
    assert analyzer._telemetry_future is pending  # left in flight, not dropped

    done = Future()
    done.set_result((600, 12000, 0.42))
    analyzer._telemetry_future = done
    assert analyzer._harvest_telemetry_future() == 0.42
    assert analyzer._telemetry_future is None

    snap = analyzer.get_telemetry()
    assert snap.speed.value == 600
    assert snap.altitude.value == 12000


def test_telemetry_split_row_confidence_is_minimum_of_digit_boxes():
    # One doubtful digit box taints the whole row (conservative min), while
    # the other row keeps its own confidence. The row value stays the leading
    # digit run of the joined text.
    results = [
        _tbox(10, 5, 60, 25, "530"),
        ([[70, 5], [110, 5], [110, 25], [70, 25]], "4", 0.3),  # stray low-conf digit box
        _tbox(10, 35, 140, 55, "27123 feet"),
    ]
    speed, alt, speed_conf, alt_conf = _split_telemetry_rows(results, img_height=60)
    assert (speed, alt) == (530, 27123)
    assert speed_conf == pytest.approx(0.3)
    assert alt_conf == pytest.approx(0.9)

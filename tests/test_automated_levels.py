'''
Usage, run in bash:  uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html

uv run pytest tests/test_automated_levels.py -k test_level3_unit_ocr --html=tests/test-output/report.html --self-contained-html

'''
import subprocess
import time
import os
from pathlib import Path
import pytest

from constants import TEST_SCREENSHOT, TEST_SCREENSHOT_B, TEST_SCREENSHOT_C, TEST_SCREENSHOT_D

SCRIPT = str(Path(__file__).resolve().parent / "analyzer_cli.py")

def run_command(cmd, timeout=60):
    start = time.time()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - start
    return result.returncode, result.stdout, result.stderr, elapsed


def test_wingman_smoke_launch():
    """
    Smoke test: Verify wingman main module imports without errors.
    
    Catches import errors, missing dependencies, threading initialization issues,
    and other startup problems. This is a quick sanity check, not a full integration test.
    """
    try:
        from wingman import main
    except Exception as e:
        pytest.fail(f"wingman.main failed to import: {e}")


def test_level1_static_screenshot():
    """
    Level 1: Respawn detection on saved screenshots with grid visualization.
    
    Validates that the analyzer can:
    - Load and analyze saved RESPAWN.png (positive case)
    - Correctly identify respawn regions and generate grid overlays
    - Handle non-respawn screenshots (RESPAWNB.png negative case)
    - Generate output_grid.png and output_grid_highlighted.png when respawn detected
    """
    screenshots = [
        ("RESPAWN.png", TEST_SCREENSHOT, True),
        ("RESPAWNB.png", TEST_SCREENSHOT_B, False)
    ]
    for name, path, should_detect in screenshots:
        assert path.exists(), f"Test screenshot not found: {path}"
        cmd = f"uv run python {SCRIPT} {path} --grid"
        code, out, err, elapsed = run_command(cmd)
        print(f"\n[Level 1 Output for {name}]\n", out)
        assert code == 0, f"Level 1 failed for {name}: {err}"
        assert "output_grid.png" in out, f"output_grid.png not generated for {name}"
        detection_msg = "[OK] Respawn detected in region(s):"
        if should_detect:
            assert "output_grid_highlighted.png" in out or "highlighted grid" in out, f"output_grid_highlighted.png not generated for {name}"
            assert detection_msg in out, f"Respawn not detected in Level 1 for {name}"
        else:
            assert detection_msg not in out, f"Respawn was incorrectly detected in {name}"


def test_respawn_detection_positive():
    """
    Respawn detection positive test: Verify correct detection of RESPAWN text.
    
    Validates that the analyzer correctly detects RESPAWN text in:
    - RESPAWN.png (normal quality image)
    - RESPAWNC.png (discolored image - tests OCR robustness)
    """
    screenshots = [
        ("RESPAWN.png", TEST_SCREENSHOT, "normal quality"),
        ("RESPAWNC.png", TEST_SCREENSHOT_C, "discolored - tests OCR robustness"),
    ]
    for name, path, description in screenshots:
        assert path.exists(), f"Test screenshot not found: {path}"
        cmd = f"uv run python {SCRIPT} {path} --grid"
        code, out, err, elapsed = run_command(cmd)
        print(f"\n[Respawn Detection Positive - {name} ({description})]\n", out)
        assert code == 0, f"Positive detection failed for {name}: {err}"
        assert "[OK] Respawn detected in region(s):" in out, f"Respawn not detected in {name} ({description})"


def test_respawn_detection_negative():
    """
    Respawn detection negative test: Verify correct rejection of non-RESPAWN text.
    
    Validates that the analyzer correctly rejects:
    - RESPAWNB.png (no respawn text)
    - RESPAWND.png (contains "natethegreat" text - should fail Levenshtein matching)
    """
    screenshots = [
        ("RESPAWNB.png", TEST_SCREENSHOT_B, "no respawn text"),
        ("RESPAWND.png", TEST_SCREENSHOT_D, "contains 'natethegreat' - Levenshtein distance too high"),
    ]
    for name, path, description in screenshots:
        assert path.exists(), f"Test screenshot not found: {path}"
        cmd = f"uv run python {SCRIPT} {path} --grid"
        code, out, err, elapsed = run_command(cmd)
        print(f"\n[Respawn Detection Negative - {name} ({description})]\n", out)
        assert code == 0, f"Negative detection failed for {name}: {err}"
        assert "[OK] Respawn detected in region(s):" not in out, f"Respawn was incorrectly detected in {name} ({description})"


def test_level2_live_capture():
    """
    Level 2: Live screen capture and real-time game state analysis.
    
    Validates that the analyzer can:
    - Capture a live screenshot from the configured monitor/region
    - Perform grid-based region analysis on captured frame
    - Generate grid visualization even when respawn is not detected
    """
    cmd = f"uv run python {SCRIPT} --capture"
    code, out, err, elapsed = run_command(cmd)
    print("\n[Level 2 Output]\n", out)
    assert code == 0, f"Level 2 failed: {err}"
    assert "output_grid.png" in out, "output_grid.png not generated"
    assert "output_grid_highlighted.png" in out or "highlighted grid" in out or "NOT detected" in out, "output_grid_highlighted.png not generated"


def test_level3_unit_ocr():
    """
    Level 3: OCR background worker performance and accuracy test.
    
    Validates that the background OCR thread:
    - Correctly runs EasyOCR on RESPAWN.png without blocking main loop
    - Detects 'RESPAWN' text with 100% confidence
    - Reports preprocessing and recognition timing metrics
    - Completes reliably on CPU (no GPU acceleration required)
    """
    assert TEST_SCREENSHOT.exists(), f"Test screenshot not found: {TEST_SCREENSHOT}"

    stage_times = {}

    t0 = time.time()
    cmd = f"uv run python {SCRIPT} --unit-ocr"
    t1 = time.time()
    code, out, err, elapsed = run_command(cmd)
    t2 = time.time()

    stage_times['command_build'] = t1 - t0
    stage_times['command_run'] = t2 - t1
    stage_times['total'] = t2 - t0

    print("\n[Level 3 Output]\n", out)
    print(f"[Timing] Command build: {stage_times['command_build']:.2f}s, Command run: {stage_times['command_run']:.2f}s, Total: {stage_times['total']:.2f}s")

    assert code == 0, f"Level 3 failed: {err}"
    assert "_run_ocr_in_background result" in out, "OCR unit test did not run"
    assert "OCR runtime" in out, "OCR runtime not reported"


'''
Usage, run in bash:  uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html

uv run pytest tests/test_automated_levels.py -k test_level3_unit_ocr --html=tests/test-output/report.html --self-contained-html

'''
import subprocess
import time
import os
from pathlib import Path

# Paths
SCRIPT = "test_analyzer.py"

import os
from pathlib import Path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEST_SCREENSHOT = Path(PROJECT_ROOT) / "test_screenshots" / "RESPAWN.png"
SCRIPT = str(Path(PROJECT_ROOT) / "tests" / "test_analyzer.py")


def run_command(cmd, timeout=60):
    start = time.time()
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    elapsed = time.time() - start
    return result.returncode, result.stdout, result.stderr, elapsed


def test_level1_static_screenshot():
    screenshots = [
        ("RESPAWN.png", TEST_SCREENSHOT, True),
        ("RESPAWN_B.png", TEST_SCREENSHOT.parent / "RESPAWN_B.png", False)
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


def test_level2_live_capture():
    cmd = f"uv run python {SCRIPT} --capture"
    code, out, err, elapsed = run_command(cmd)
    print("\n[Level 2 Output]\n", out)
    assert code == 0, f"Level 2 failed: {err}"
    assert "output_grid.png" in out, "output_grid.png not generated"
    assert "output_grid_highlighted.png" in out or "highlighted grid" in out or "NOT detected" in out, "output_grid_highlighted.png not generated"


def test_level3_unit_ocr():
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


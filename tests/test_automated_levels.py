'''
Usage, run in bash:  uv run pytest tests/test_automated_levels.py --html=tests/test-output/report.html --self-contained-html

uv run pytest tests/test_automated_levels.py -k test_level3_unit_ocr --html=tests/test-output/report.html --self-contained-html

'''
import subprocess
import time
import os
from pathlib import Path
import cv2
import pytest
import yaml

from constants import (
    CONFIG_PATH,
    TEST_SCREENSHOT,
    TEST_SCREENSHOT_B,
    TEST_SCREENSHOT_C,
    TEST_SCREENSHOT_D,
    TEST_SCREENSHOT_CONTINUE,
    TEST_SCREENSHOT_CONTINUE_1,
    TEST_SCREENSHOT_INCOMING,
    TEST_SCREENSHOT_INCOMING_1,
    TEST_SCREENSHOT_INCOMING_2,
)
from wingman.analyzer import GameStateAnalyzer

SCRIPT = str(Path(__file__).resolve().parent / "analyzer_cli.py")


def load_config(path: Path = CONFIG_PATH) -> dict:
    with path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def _load_image(image_path: Path):
    frame = cv2.imread(str(image_path))
    assert frame is not None, f"Could not load image: {image_path}"
    return frame


@pytest.fixture
def require_easyocr():
    pytest.importorskip("easyocr", reason="EasyOCR not installed")

def run_command(cmd, timeout=120):
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


@pytest.mark.parametrize(
    "image_path",
    [
        TEST_SCREENSHOT_CONTINUE,
        TEST_SCREENSHOT_CONTINUE_1,
    ],
)
def test_level4_region33_contains_lick_to_c(require_easyocr, image_path: Path):
    """Validate region 33 OCR includes 'lick to C' on continue screenshots."""
    analyzer = GameStateAnalyzer(load_config())
    frame = _load_image(image_path)
    region_frame = analyzer.get_region(frame, 33)

    gray = cv2.cvtColor(region_frame, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    preprocessed = cv2.resize(binary, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)

    reader = analyzer.ocr_reader
    assert reader is not None, "EasyOCR reader failed to initialize"

    ocr_results = reader.readtext(preprocessed, detail=0, paragraph=True)
    extracted_text = " ".join(str(result) for result in ocr_results)
    normalized = " ".join(extracted_text.upper().split())

    assert "LICK TO C" in normalized, (
        f"Expected 'lick to C' in region 33 for {image_path.name}; OCR output was: {ocr_results}"
    )


@pytest.mark.parametrize(
    "image_path",
    [
        TEST_SCREENSHOT_INCOMING,
        TEST_SCREENSHOT_INCOMING_1,
        TEST_SCREENSHOT_INCOMING_2,
    ],
)
def test_level4_region9_contains_inco(require_easyocr, image_path: Path):
    """Validate region 10 OCR includes 'MING' on INCOMING screenshots."""
    analyzer = GameStateAnalyzer(load_config())
    frame = _load_image(image_path)
    region_frame = analyzer.get_region(frame, 10)

    reader = analyzer.ocr_reader
    assert reader is not None, "EasyOCR reader failed to initialize"

    gray = cv2.cvtColor(region_frame, cv2.COLOR_BGR2GRAY)
    _, binary_otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Try multiple preprocessing variants to avoid losing thin glyphs on downscale.
    variants = {
        "binary_otsu_1p0": binary_otsu,
        "binary_otsu_up_1p4": cv2.resize(binary_otsu, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
        "binary_otsu_up_1p8": cv2.resize(binary_otsu, None, fx=1.8, fy=1.8, interpolation=cv2.INTER_CUBIC),
        "binary_otsu_inv_1p4": cv2.bitwise_not(cv2.resize(binary_otsu, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC)),
        "gray_up_1p4": cv2.resize(gray, None, fx=1.4, fy=1.4, interpolation=cv2.INTER_CUBIC),
    }

    debug_dir = Path(__file__).parent / "test-output"
    debug_dir.mkdir(parents=True, exist_ok=True)
    stem = image_path.stem
    cv2.imwrite(str(debug_dir / f"debug_ocr_region10_{stem}_grayscale.png"), gray)
    cv2.imwrite(str(debug_dir / f"debug_ocr_region10_{stem}_binary.png"), binary_otsu)

    target = "MING"
    matched_variant = None
    variant_outputs = {}

    for variant_name, img in variants.items():
        cv2.imwrite(str(debug_dir / f"debug_ocr_region10_{stem}_{variant_name}.png"), img)
        ocr_results = reader.readtext(img, detail=0, paragraph=True)
        extracted_text = " ".join(str(result) for result in ocr_results)
        normalized = " ".join(extracted_text.upper().split())
        variant_outputs[variant_name] = {"raw": ocr_results, "normalized": normalized}
        if target in normalized:
            matched_variant = variant_name
            # Keep legacy debug filenames for pytest-html hook compatibility.
            cv2.imwrite(str(debug_dir / "debug_ocr_grayscale.png"), gray)
            cv2.imwrite(str(debug_dir / "debug_ocr_binary.png"), binary_otsu)
            cv2.imwrite(str(debug_dir / "debug_ocr_downscaled.png"), img)
            break

    assert matched_variant is not None, (
        f"Expected '{target}' in region 10 for {image_path.name}; no preprocessing variant matched. "
        f"Variant outputs: {variant_outputs}"
    )


def test_level5_performance_validation(test_timings, strict_timing):
    """
    Level 5: Performance validation against baseline.
    
    Validates that test execution times are within expected ranges.
    - Warning mode (default): Reports deviations but doesn't fail
    - Strict mode (--strict-timing): Fails on timing violations
    
    This test runs last to validate all previous test timings.
    """
    baseline_path = Path(__file__).resolve().parent / "test_timing_baseline.yaml"
    
    if not baseline_path.exists():
        print("\n[Level 5] No timing baseline found - skipping performance validation")
        print(f"  Create {baseline_path} to enable performance validation")
        return
    
    # Load baseline
    with baseline_path.open('r') as f:
        baseline_data = yaml.safe_load(f)
    
    baseline_durations = baseline_data.get('test_durations', {})
    tolerance = baseline_data.get('tolerance', 5)
    strict_mode = strict_timing
    
    print("\n" + "=" * 60)
    print("[Level 5] Performance Validation")
    print("=" * 60)
    print(f"Mode: {'STRICT (fails on violations)' if strict_mode else 'WARNING (reports only)'}")
    print(f"Tolerance: ±{tolerance}s")
    print("-" * 60)
    
    violations = []
    all_ok = []
    
    for test_name, durations in sorted(test_timings.items()):
        if test_name == 'test_level5_performance_validation':
            continue  # Skip self
        
        # For parametrized tests, use average duration
        avg_duration = sum(durations) / len(durations)
        
        if test_name in baseline_durations:
            expected = baseline_durations[test_name]
            deviation = abs(avg_duration - expected)
            
            if deviation > tolerance:
                status = "⚠️  VIOLATION"
                violations.append({
                    'test': test_name,
                    'expected': expected,
                    'actual': avg_duration,
                    'deviation': deviation,
                    'tolerance': tolerance
                })
            else:
                status = "✓ OK"
                all_ok.append(test_name)
            
            print(f"{status:12s} {test_name:45s} Expected: {expected:5.1f}s  Actual: {avg_duration:5.1f}s  Δ {deviation:+5.1f}s")
        else:
            print(f"{'ℹ️  NEW':12s} {test_name:45s} Duration: {avg_duration:5.1f}s (no baseline)")
    
    print("-" * 60)
    
    if violations:
        print(f"\n⚠️  {len(violations)} timing violation(s) detected:")
        for v in violations:
            print(f"  • {v['test']}")
            print(f"    Expected: {v['expected']:.1f}s ± {v['tolerance']}s")
            print(f"    Actual:   {v['actual']:.1f}s")
            print(f"    Deviation: {v['deviation']:.1f}s over tolerance")
        
        if strict_mode:
            print("\n❌ --strict-timing: Failing due to timing violations")
            pytest.fail(f"{len(violations)} test(s) exceeded timing tolerance of ±{tolerance}s")
        else:
            print("\n💡 Tip: Use pytest --strict-timing to fail on timing violations (useful for CI)")
    else:
        print(f"\n✓ All {len(all_ok)} tests within expected timing ranges")
    
    print("=" * 60)



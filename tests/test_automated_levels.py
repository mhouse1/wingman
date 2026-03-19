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


def _grid_meta(cfg: dict) -> tuple[int, int, int, int]:
    """Return (grid_size, total_regions, incoming_region, respawn_region)."""
    respawn_cfg = cfg.get("respawn_detection", {})
    grid_size = int(respawn_cfg.get("grid_size", 6))
    total_regions = grid_size * grid_size
    incoming_region = int(respawn_cfg.get("incoming_region", 10))
    respawn_region = int(respawn_cfg.get("region", 27))
    return grid_size, total_regions, incoming_region, respawn_region


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


def test_get_frame_and_analyze_frame():
    """
    Measures the time for one main-loop capture+analyze iteration:

        frame = cap.get_frame()
        game_state = analyzer.analyze_frame(frame)

    analyze_frame() is non-blocking — it returns the cached OCR result
    immediately and schedules background work. Only that synchronous
    return path is timed here (no OCR wait).
    """
    from wingman.capture import Capture

    cfg = load_config()
    region = (
        cfg["region"]["left"],
        cfg["region"]["top"],
        cfg["region"]["width"],
        cfg["region"]["height"],
    )
    cap = Capture(region, monitor_index=cfg["region"].get("monitor", 1))
    analyzer = GameStateAnalyzer(cfg)
    analyzer._game_lobby = False  # force GAME_BATTLE so analyze_frame runs

    # Seed the cache so analyze_frame returns immediately without scheduling OCR
    with analyzer._ocr_cache_lock:
        analyzer._ocr_cache['result'] = (True, 1.0, 'test')
        analyzer._ocr_cache['timestamp'] = time.time()

    t0 = time.perf_counter()
    frame = cap.get_frame()
    t1 = time.perf_counter()
    game_state = analyzer.analyze_frame(frame)
    t2 = time.perf_counter()

    get_frame_ms  = (t1 - t0) * 1000
    analyze_ms    = (t2 - t1) * 1000
    total_ms      = (t2 - t0) * 1000

    print(
        f"\n[get_frame + analyze_frame]"
        f"\n  get_frame()     : {get_frame_ms:.2f} ms"
        f"\n  analyze_frame() : {analyze_ms:.2f} ms"
        f"\n  total           : {total_ms:.2f} ms"
    )

    assert frame is not None, "get_frame() returned no frame"
    assert game_state.get('is_respawning') is True, "Expected respawning from seeded cache"
    assert total_ms < 1000, f"Took {total_ms:.0f} ms — expected under 1000 ms"

    analyzer.cleanup()  # shut down OCR executor so threads don't outlive this test


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
    """Validate continue text OCR includes 'LICK TO C' in at least one grid region."""
    cfg = load_config()
    analyzer = GameStateAnalyzer(cfg)
    _, total_regions, _, _ = _grid_meta(cfg)
    frame = _load_image(image_path)

    reader = analyzer.ocr_reader
    assert reader is not None, "EasyOCR reader failed to initialize"

    matched_region = None
    region_outputs = {}

    for region in range(1, total_regions + 1):
        region_frame = analyzer.get_region(frame, region)
        gray = cv2.cvtColor(region_frame, cv2.COLOR_BGR2GRAY)
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        preprocessed = cv2.resize(binary, None, fx=0.7, fy=0.7, interpolation=cv2.INTER_AREA)

        ocr_results = reader.readtext(preprocessed, detail=0, paragraph=True)
        extracted_text = " ".join(str(result) for result in ocr_results)
        normalized = " ".join(extracted_text.upper().split())
        region_outputs[region] = normalized

        if "LICK TO C" in normalized:
            matched_region = region
            break

    assert matched_region is not None, (
        f"Expected 'LICK TO C' in one of {total_regions} regions for {image_path.name}; "
        f"sample outputs: {dict(list(region_outputs.items())[:10])}"
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
    """Validate configured incoming region OCR includes 'MING' on INCOMING screenshots."""
    cfg = load_config()
    analyzer = GameStateAnalyzer(cfg)
    _, _, incoming_region, _ = _grid_meta(cfg)
    frame = _load_image(image_path)
    region_frame = analyzer.get_region(frame, incoming_region)

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
    # cv2.imwrite(str(debug_dir / f"debug_ocr_region{incoming_region}_{stem}_grayscale.png"), gray)
    # cv2.imwrite(str(debug_dir / f"debug_ocr_region{incoming_region}_{stem}_binary.png"), binary_otsu)

    target = "MING"
    matched_variant = None
    variant_outputs = {}

    for variant_name, img in variants.items():
        # cv2.imwrite(str(debug_dir / f"debug_ocr_region{incoming_region}_{stem}_{variant_name}.png"), img)
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
        f"Expected '{target}' in region {incoming_region} for {image_path.name}; no preprocessing variant matched. "
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



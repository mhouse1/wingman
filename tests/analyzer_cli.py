"""Manual CLI utility for GameStateAnalyzer diagnostics.

Example:
    uv run python tests/analyzer_cli.py test_screenshots/integration_test/P1_050_RESPAWN_VISIBLE_NO_HEALTH.png --grid
"""

import argparse
import logging
import os
import sys
import time
from pathlib import Path

import cv2
import yaml

# Enable debug logging to see what OCR detects
logging.basicConfig(level=logging.DEBUG, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from wingman.analyzer import GameState, GameStateAnalyzer
from wingman.capture import Capture
from wingman.crop_region import draw_crops, get_crop


def load_config(path: Path | None = None) -> dict:
    config_path = path or (PROJECT_ROOT / "wingman" / "config.yaml")
    with config_path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def test_run_ocr_in_background(image_path: str = "RESPAWN.png"):
    if image_path == "RESPAWN.png":
        # Default respawn fixture is the gate-corpus frame (ADR 072).
        image_path = str(PROJECT_ROOT / "test_screenshots" / "integration_test"
                         / "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png")

    analyzer = GameStateAnalyzer(load_config())
    analyzer.state = GameState.GAME_BATTLE.name  # CLI tests static screenshots; force GAME_BATTLE
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    start_time = time.time()
    analyzer._background_ocr_frame = frame
    analyzer._run_ocr_in_background()
    elapsed = time.time() - start_time

    with analyzer._ocr_cache_lock:
        result = analyzer._ocr_cache["result"]
        print(
            f"[UnitTest] _run_ocr_in_background result: "
            f"is_respawning={result[0]}, confidence={result[1]:.2%}, method={result[2]}"
        )
        print(f"[UnitTest] OCR runtime: {elapsed:.2f} seconds")
        if result[1] < 1.0:
            print(f"[UnitTest] FAIL: OCR confidence is not 100% (confidence={result[1]:.2%})")
            sys.stdout.flush()
            sys.stderr.flush()
            os._exit(1)

    # Force exit to avoid hanging on EasyOCR threads
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def capture_and_test_with_visualization():
    cfg = load_config()
    region = (
        cfg["region"]["left"],
        cfg["region"]["top"],
        cfg["region"]["width"],
        cfg["region"]["height"],
    )
    monitor_index = cfg.get("monitor", 1)
    cap = Capture(region, monitor_index=monitor_index)

    print(f"Capturing screenshot from monitor {monitor_index} region {region}...")
    frame = cap.get_frame()
    if frame is None:
        print("SKIP: get_frame() returned None — game window not found (MetalStorm not running)")
        sys.stdout.flush()
        os._exit(0)
    output_dir = Path("tests") / "test-output"
    output_dir.mkdir(parents=True, exist_ok=True)
    screenshot_path = str(output_dir / "live_capture.png")
    cv2.imwrite(screenshot_path, frame)
    print(f"[OK] Screenshot saved to {screenshot_path}")

    test_with_visualization(screenshot_path)

    # Force exit to avoid hanging on EasyOCR threads (called by test_with_visualization)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def test_respawn_detection(image_path: str = "RESPAWN.png"):
    print(f"Loading config and image: {image_path}")
    analyzer = GameStateAnalyzer(load_config())
    analyzer.state = GameState.GAME_BATTLE.name  # CLI tests static screenshots; force GAME_BATTLE

    if image_path == "RESPAWN.png":
        # Default respawn fixture is the gate-corpus frame (ADR 072).
        image_path = str(PROJECT_ROOT / "test_screenshots" / "integration_test"
                         / "P1_050_RESPAWN_VISIBLE_NO_HEALTH.png")
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        print("Make sure the gate-corpus screenshots exist (refresh with: make p1)")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    print(f"Image loaded: {frame.shape[1]}x{frame.shape[0]} pixels")
    print("-" * 60)

    print("Analyzing frame...")
    state = analyzer.analyze_frame(frame)

    # Wait for background OCR thread to complete
    if analyzer._background_ocr_thread and analyzer._background_ocr_thread.is_alive():
        print("Waiting for OCR to complete...")
        analyzer._background_ocr_thread.join(timeout=120)

    # Re-analyze to get updated cache result
    state = analyzer.analyze_frame(frame)

    print("\nGame State Analysis:")
    print(f"  Is Respawning: {state['is_respawning']}")
    print(f"  Confidence: {state['respawn_confidence']:.2%}")
    print(f"  Detection Method: {state['respawn_method']}")

    print("\n" + "=" * 60)
    if state["is_respawning"]:
        print("[OK] RESPAWN SCREEN DETECTED!")
    else:
        print("[FAIL] Respawn screen NOT detected")
        print("\nTry running with --grid flag to visualize detection regions:")
    print("=" * 60)
    # Force exit to avoid hanging on EasyOCR threads
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)

def test_multiple_images():
    test_dir = PROJECT_ROOT / "test_screenshots"

    if not test_dir.exists():
        print(f"ERROR: {test_dir} directory not found")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    test_images = sorted(test_dir.glob("*.png"))
    if not test_images:
        print(f"No PNG files found in {test_dir}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    analyzer = GameStateAnalyzer(load_config())
    analyzer.state = GameState.GAME_BATTLE.name  # CLI tests static screenshots; force GAME_BATTLE

    print(f"Testing {len(test_images)} images from {test_dir}:")
    print("=" * 60)

    for img_path in test_images:
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"⊘ {img_path.name}: Failed to load")
            continue

        analyzer.reset_cache()
        state = analyzer.analyze_frame(frame)
        status = "RESPAWN" if state["is_respawning"] else "GAMEPLAY"
        confidence = state["respawn_confidence"]
        print(f"[OK] {img_path.name:40s} | {status:8s} | Conf: {confidence:.2%}")

    print("=" * 60)

    # Force exit to avoid hanging on EasyOCR threads
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def test_with_visualization(image_path: str = "RESPAWN.png"):
    start_time = time.time()
    print(f"Loading {image_path}...")

    analyzer = GameStateAnalyzer(load_config())
    analyzer.state = GameState.GAME_BATTLE.name  # CLI tests static screenshots; force GAME_BATTLE
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load {image_path}")
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)

    print(f"Image size: {frame.shape[1]}x{frame.shape[0]}")

    # First call schedules background OCR
    state = analyzer.analyze_frame(frame)

    # Wait for background OCR thread to complete
    if analyzer._background_ocr_thread and analyzer._background_ocr_thread.is_alive():
        print("\nWaiting for OCR to complete...")
        analyzer._background_ocr_thread.join(timeout=120)

    # Re-analyze to get updated cache result
    state = analyzer.analyze_frame(frame)

    print("\nFull frame analysis:")
    print(f"  Respawning: {state['is_respawning']}")
    print(f"  Confidence: {state['respawn_confidence']:.2%}")
    print(f"  Method: {state['respawn_method']}")

    output_dir = Path("tests") / "test-output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Draw named crop overlays on the frame
    frame_with_crops = draw_crops(frame, analyzer.crops)
    output_grid = str(output_dir / "output_grid.png")
    cv2.imwrite(output_grid, frame_with_crops)
    print(f"\n[OK] Saved grid visualization to {output_grid}")

    if state["is_respawning"]:
        output_highlighted = str(output_dir / "output_grid_highlighted.png")
        cv2.imwrite(output_highlighted, frame_with_crops)
        print(f"[OK] Saved highlighted grid to {output_highlighted}")
        print("\n[OK] Respawn detected in region(s): ['respawn']")
        print(f"  Confidence: {state['respawn_confidence']:.2%}")
    else:
        print("\n[FAIL] Respawn NOT detected in any region")

    elapsed = time.time() - start_time
    print(f"\nTotal runtime: {elapsed:.2f} seconds")

    # Force exit to avoid hanging on EasyOCR threads
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def draw_crops_overlay(image_path: str):
    """Draw named crop region overlays on a screenshot.

    Outputs:
      tests/test-output/crops_overlay.png  — full frame with all named crop rectangles labelled
      tests/test-output/crop_<name>.png    — individual crop extract for each named region
    """
    config = load_config()
    analyzer = GameStateAnalyzer(config)

    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load {image_path}")
        sys.exit(1)

    output_dir = Path("tests") / "test-output"
    output_dir.mkdir(parents=True, exist_ok=True)

    # Full-frame overlay with all crop rectangles
    frame_with_crops = draw_crops(frame, analyzer.crops)
    overlay_out = str(output_dir / "crops_overlay.png")
    cv2.imwrite(overlay_out, frame_with_crops)
    print(f"[OK] Crops overlay -> {overlay_out}")

    # Individual crop extracts
    for name, coords in analyzer.crops.items():
        crop_img = get_crop(frame, coords.x1, coords.y1, coords.x2, coords.y2)
        if crop_img.size == 0:
            print(f"[WARN] Crop '{name}' is empty — skipping")
            continue
        crop_out = str(output_dir / f"crop_{name}.png")
        cv2.imwrite(crop_out, crop_img)
        print(f"[OK] Crop '{name}' ({crop_img.shape[1]}x{crop_img.shape[0]}) -> {crop_out}")


def main():
    parser = argparse.ArgumentParser(description="Test GameStateAnalyzer")
    parser.add_argument("image", nargs="?", default="RESPAWN.png", help="Path to screenshot")
    parser.add_argument("--multiple", action="store_true", help="Test multiple screenshots")
    parser.add_argument("--grid", action="store_true", help="Run grid visualization")
    parser.add_argument("--capture", action="store_true", help="Capture screenshot then analyze")
    parser.add_argument("--unit-ocr", action="store_true", help="Directly test _run_ocr_in_background")
    parser.add_argument("--crops", action="store_true", help="Draw named crop region overlays on a screenshot")
    args = parser.parse_args()

    if args.unit_ocr:
        test_run_ocr_in_background(args.image)
    elif args.capture:
        capture_and_test_with_visualization()
    elif args.multiple:
        test_multiple_images()
    elif args.grid:
        test_with_visualization(args.image)
    elif args.crops:
        draw_crops_overlay(args.image)
    else:
        test_respawn_detection(args.image)


if __name__ == "__main__":
    main()

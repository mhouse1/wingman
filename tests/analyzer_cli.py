"""Manual CLI utility for GameStateAnalyzer diagnostics.

Example:
    uv run python tests/analyzer_cli.py test_screenshots/RESPAWN.png --grid
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

from wingman.analyzer import GameStateAnalyzer
from wingman.capture import Capture


def load_config(path: Path | None = None) -> dict:
    config_path = path or (PROJECT_ROOT / "wingman" / "config.yaml")
    with config_path.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def test_run_ocr_in_background(image_path: str = "RESPAWN.png"):
    if image_path == "RESPAWN.png":
        image_path = str(PROJECT_ROOT / "test_screenshots" / "RESPAWN.png")

    analyzer = GameStateAnalyzer(load_config())
    analyzer._game_lobby = False  # CLI tests static screenshots; force GAME_BATTLE
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
    monitor_index = cfg["region"].get("monitor", 1)
    cap = Capture(region, monitor_index=monitor_index)

    print(f"Capturing screenshot from monitor {monitor_index} region {region}...")
    frame = cap.get_frame()
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
    analyzer._game_lobby = False  # CLI tests static screenshots; force GAME_BATTLE

    if image_path == "RESPAWN.png":
        image_path = str(PROJECT_ROOT / "test_screenshots" / "RESPAWN.png")
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        print("Make sure RESPAWN.png is in the test_screenshots directory at the project root")
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
    analyzer._game_lobby = False  # CLI tests static screenshots; force GAME_BATTLE

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
    analyzer._game_lobby = False  # CLI tests static screenshots; force GAME_BATTLE
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
    analyzer.draw_grid(frame, output_path=str(output_dir / "output_grid.png"))
    print(f"\n[OK] Saved grid visualization to {output_dir / 'output_grid.png'}")

    total_regions = analyzer.grid_rows * analyzer.grid_cols
    print(f"\nTesting individual regions (1-{total_regions}):")
    print("-" * 60)
    respawn_regions = []

    for region in range(1, total_regions + 1):
        region_state = analyzer.analyze_frame(frame, region=region)
        status = "[OK] RESPAWN" if region_state["is_respawning"] else "  gameplay"
        confidence = region_state["respawn_confidence"]
        print(f"  Region {region:2d}: {status} | Conf: {confidence:.2%}")

        if region_state["is_respawning"]:
            respawn_regions.append((region, confidence))

    print("-" * 60)
    if respawn_regions:
        print(f"\n[OK] Respawn detected in region(s): {[r[0] for r in respawn_regions]}")
        best_region = max(respawn_regions, key=lambda value: value[1])
        print(f"  Best match: Region {best_region[0]} (confidence: {best_region[1]:.2%})")
        print(f"\nRecommendation: Use region={best_region[0]} for faster analysis")

        output_dir = Path("tests") / "test-output"
        analyzer.draw_grid(frame, highlight_region=best_region[0], output_path=str(output_dir / "output_grid_highlighted.png"))
        print(f"[OK] Saved highlighted grid to {output_dir / 'output_grid_highlighted.png'}")
    else:
        print("\n[FAIL] Respawn NOT detected in any region")

    elapsed = time.time() - start_time
    print(f"\nTotal runtime: {elapsed:.2f} seconds")
    
    # Force exit to avoid hanging on EasyOCR threads
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


def draw_subgrid_overlay(image_path: str):
    """Draw the respawn and incoming sub-grid overlays on their extracted regions.

    Outputs:
      tests/test-output/subgrid_respawn.png   — region 44 with rows×cols sub-grid
      tests/test-output/subgrid_incoming.png  — region 21 with NxN sub-grid
    """
    config = load_config()
    analyzer = GameStateAnalyzer(config)

    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load {image_path}")
        sys.exit(1)

    output_dir = Path("tests") / "test-output"
    output_dir.mkdir(parents=True, exist_ok=True)

    def _draw_subgrid(cell_frame, grid_rows, grid_cols, highlight_cell):
        """Return a copy of cell_frame with a rows×cols numbered grid drawn on it."""
        out = cell_frame.copy()
        h, w = out.shape[:2]
        cell_h = h // grid_rows
        cell_w = w // grid_cols

        # Draw grid lines
        for c in range(1, grid_cols):
            x = w * c // grid_cols
            cv2.line(out, (x, 0), (x, h), (0, 255, 255), 2)
        for r in range(1, grid_rows):
            y = h * r // grid_rows
            cv2.line(out, (0, y), (w, y), (0, 255, 255), 2)

        # Number each cell
        for idx in range(1, grid_rows * grid_cols + 1):
            row = (idx - 1) // grid_cols
            col = (idx - 1) % grid_cols
            cx = col * cell_w + cell_w // 2 - 10
            cy = row * cell_h + cell_h // 2 + 10
            cv2.putText(out, str(idx), (cx, cy), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 2)

        # Highlight configured sub-region
        if 1 <= highlight_cell <= grid_rows * grid_cols:
            row = (highlight_cell - 1) // grid_cols
            col = (highlight_cell - 1) % grid_cols
            x1 = col * cell_w
            y1 = row * cell_h
            cv2.rectangle(out, (x1, y1), (x1 + cell_w, y1 + cell_h), (0, 255, 0), 4)

        return out

    # --- Respawn region sub-grid ---
    respawn_cell = analyzer.get_region(frame, analyzer.respawn_region)
    respawn_overlay = _draw_subgrid(
        respawn_cell,
        analyzer.respawn_subgrid_rows,
        analyzer.respawn_subgrid_cols,
        analyzer.respawn_subregion,
    )
    respawn_out = str(output_dir / "subgrid_respawn.png")
    cv2.imwrite(respawn_out, respawn_overlay)
    print(f"[OK] Respawn sub-grid ({analyzer.respawn_subgrid_rows}x{analyzer.respawn_subgrid_cols}, "
          f"highlight={analyzer.respawn_subregion}) -> {respawn_out}")

    # --- Incoming region sub-grid ---
    incoming_cell = analyzer.get_region(frame, analyzer.incoming_region)
    n = analyzer.incoming_subgrid_size
    incoming_overlay = _draw_subgrid(incoming_cell, n, n, analyzer.incoming_subregion)
    incoming_out = str(output_dir / "subgrid_incoming.png")
    cv2.imwrite(incoming_out, incoming_overlay)
    print(f"[OK] Incoming sub-grid ({n}x{n}, highlight={analyzer.incoming_subregion}) -> {incoming_out}")


def main():
    parser = argparse.ArgumentParser(description="Test GameStateAnalyzer")
    parser.add_argument("image", nargs="?", default="RESPAWN.png", help="Path to screenshot")
    parser.add_argument("--multiple", action="store_true", help="Test multiple screenshots")
    parser.add_argument("--grid", action="store_true", help="Run grid visualization")
    parser.add_argument("--capture", action="store_true", help="Capture screenshot then analyze")
    parser.add_argument("--unit-ocr", action="store_true", help="Directly test _run_ocr_in_background")
    parser.add_argument("--subgrid", action="store_true", help="Draw sub-grid overlays for respawn and incoming regions")
    args = parser.parse_args()

    if args.unit_ocr:
        test_run_ocr_in_background(args.image)
    elif args.capture:
        capture_and_test_with_visualization()
    elif args.multiple:
        test_multiple_images()
    elif args.grid:
        test_with_visualization(args.image)
    elif args.subgrid:
        draw_subgrid_overlay(args.image)
    else:
        test_respawn_detection(args.image)


if __name__ == "__main__":
    main()

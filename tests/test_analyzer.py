"""
Test script for GameStateAnalyzer with saved screenshots.
Example usage: uv run python test_analyzer.py test_screenshots/RESPAWN.png --grid
"""

import cv2
import yaml
import sys
import time
import os
from pathlib import Path



# Add project root to sys.path so 'wingman' can be imported regardless of working directory
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Project root for absolute paths
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

from wingman.analyzer import GameStateAnalyzer

def test_run_ocr_in_background(image_path="RESPAWN.png"):
    """
    Directly test the GameStateAnalyzer._run_ocr_in_background method for OCR detection.
    Loads an image, sets it as the background frame, runs OCR in background, and prints the result from cache.
    uv run python test_analyzer.py --unit-ocr
    """
    # Default to test_screenshots/RESPAWN.png if no path or default is given
    if image_path == "RESPAWN.png":
        image_path = os.path.join(PROJECT_ROOT, "test_screenshots", "RESPAWN.png")
    cfg = load_config()
    analyzer = GameStateAnalyzer(cfg)
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        return
    start_time = time.time()
    analyzer._background_ocr_frame = frame
    analyzer._run_ocr_in_background()
    elapsed = time.time() - start_time
    # Read result from cache
    with analyzer._ocr_cache_lock:
        result = analyzer._ocr_cache['result']
        print(f"[UnitTest] _run_ocr_in_background result: is_respawning={result[0]}, confidence={result[1]:.2%}, method={result[2]}")
        print(f"[UnitTest] OCR runtime: {elapsed:.2f} seconds")
        # Fail the test if confidence is not 100%
        if result[1] < 1.0:
            raise AssertionError(f"[UnitTest] FAIL: OCR confidence is not 100% (confidence={result[1]:.2%})")


def capture_and_test_with_visualization():
    """
    Capture a new screenshot using the configured region and monitor, then run grid visualization analysis on it.
    The screenshot is saved as 'live_capture.png' and used for analysis instead of RESPAWN.png.
    """
    from wingman.capture import Capture
    import time

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
    # Save the screenshot
    screenshot_path = "live_capture.png"
    cv2.imwrite(screenshot_path, frame)
    print(f"[OK] Screenshot saved to {screenshot_path}")
    # Run grid visualization on the captured screenshot
    test_with_visualization(screenshot_path)

def load_config(path=None):
    """Load configuration file."""
    if path is None:
        path = os.path.join(PROJECT_ROOT, "wingman", "config.yaml")
    with open(path, "r") as f:
        return yaml.safe_load(f)


def test_respawn_detection(image_path="RESPAWN.png", calibrate=False):
    """
    Test respawn detection on a saved screenshot.
    
    Args:
        image_path: Path to screenshot file
        calibrate: If True, show detailed calibration info
    """
    print(f"Loading config and image: {image_path}")
    
    # Load config
    cfg = load_config()
    
    # Create analyzer
    analyzer = GameStateAnalyzer(cfg)
    
    # Load screenshot
    if image_path == "RESPAWN.png":
        image_path = os.path.join(PROJECT_ROOT, "test_screenshots", "RESPAWN.png")
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        print("Make sure RESPAWN.png is in the test_screenshots directory at the project root")
        return
    
    print(f"Image loaded: {frame.shape[1]}x{frame.shape[0]} pixels")
    print("-" * 60)
    
    if calibrate:
        # Detailed calibration mode
        print("Running calibration mode...")
        print("This will show HSV masks and detection statistics")
        print("Press any key to close windows\n")
        
        stats = analyzer.calibrate_respawn_detection(frame)
        
        print("\nCalibration Results:")
        print(f"  Text Detection:")
        print(f"    Pixels detected: {stats['text_pixels']}")
        print(f"    Ratio: {stats['text_ratio']:.6f} (threshold: {stats['text_threshold']:.6f})")
        print(f"    Status: {'[OK] DETECTED' if stats['text_detected'] else '[FAIL] NOT DETECTED'}")
        print(f"\n  Progress Bar Detection:")
        print(f"    Pixels detected: {stats['bar_pixels']}")
        print(f"    Ratio: {stats['bar_ratio']:.6f} (threshold: {stats['bar_threshold']:.6f})")
        print(f"    Status: {'[OK] DETECTED' if stats['bar_detected'] else '[FAIL] NOT DETECTED'}")
        
    else:
        # Normal test mode
        print("Analyzing frame...")
        state = analyzer.analyze_frame(frame)
        
        print("\nGame State Analysis:")
        print(f"  Is Respawning: {state['is_respawning']}")
        print(f"  Confidence: {state['respawn_confidence']:.2%}")
        print(f"  Detection Method: {state['respawn_method']}")
        
        print("\n" + "=" * 60)
        if state['is_respawning']:
            print("[OK] RESPAWN SCREEN DETECTED!")
        else:
            print("[FAIL] Respawn screen NOT detected")
            print("\nTry running with --calibrate flag to debug:")
            print("  python test_analyzer.py RESPAWN.png --calibrate")
        print("=" * 60)


def test_multiple_images():
    """Test analyzer on multiple screenshots if available."""
    test_dir = Path(os.path.join(PROJECT_ROOT, "test_screenshots"))
    
    if not test_dir.exists():
        print(f"ERROR: {test_dir} directory not found")
        return
    
    # Find all PNG files
    test_images = sorted(test_dir.glob("*.png"))
    
    if not test_images:
        print(f"No PNG files found in {test_dir}")
        return
    
    cfg = load_config()
    analyzer = GameStateAnalyzer(cfg)
    
    print(f"Testing {len(test_images)} images from {test_dir}:")
    print("=" * 60)
    
    for img_path in test_images:
        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"⊘ {img_path.name}: Failed to load")
            continue
        
        # Reset cache between images to avoid cached results from previous image
        analyzer.reset_cache()
        
        state = analyzer.analyze_frame(frame)
        status = "RESPAWN" if state['is_respawning'] else "GAMEPLAY"
        confidence = state['respawn_confidence']
        
        print(f"[OK] {img_path.name:40s} | {status:8s} | Conf: {confidence:.2%}")
    
    print("=" * 60)




def test_with_visualization(image_path="RESPAWN.png"):
    """Test with visual grid overlay to identify regions."""
    start_time = time.time()
    print(f"Loading {image_path}...")

    cfg = load_config()
    analyzer = GameStateAnalyzer(cfg)

    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load {image_path}")
        return

    print(f"Image size: {frame.shape[1]}x{frame.shape[0]}")

    # Analyze full frame
    state = analyzer.analyze_frame(frame)
    print(f"\nFull frame analysis:")
    print(f"  Respawning: {state['is_respawning']}")
    print(f"  Confidence: {state['respawn_confidence']:.2%}")
    print(f"  Method: {state['respawn_method']}")

    # Draw grid and save
    grid_img = analyzer.draw_grid(frame, output_path="output_grid.png")
    print(f"\n[OK] Saved grid visualization to output_grid.png")

    # Test each region
    print(f"\nTesting individual regions (1-36):")
    print("-" * 60)
    respawn_regions = []


    for region in range(1, 37):
        region_state = analyzer.analyze_frame(frame, region=region)
        status = "[OK] RESPAWN" if region_state['is_respawning'] else "  gameplay"
        confidence = region_state['respawn_confidence']
        print(f"  Region {region:2d}: {status} | Conf: {confidence:.2%}")

        if region_state['is_respawning']:
            respawn_regions.append((region, confidence))

    print("-" * 60)
    if respawn_regions:
        print(f"\n[OK] Respawn detected in region(s): {[r[0] for r in respawn_regions]}")
        best_region = max(respawn_regions, key=lambda x: x[1])
        print(f"  Best match: Region {best_region[0]} (confidence: {best_region[1]:.2%})")
        print(f"\nRecommendation: Use region={best_region[0]} for faster analysis")

        # Save grid with best region highlighted
        highlighted_grid = analyzer.draw_grid(frame, highlight_region=best_region[0], 
                             output_path="output_grid_highlighted.png")
        print(f"[OK] Saved highlighted grid to output_grid_highlighted.png")
    else:
        print("\n[FAIL] Respawn NOT detected in any region")

    elapsed = time.time() - start_time
    print(f"\nTotal runtime: {elapsed:.2f} seconds")



def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Test GameStateAnalyzer")
    parser.add_argument("image", nargs="?", default="RESPAWN.png", 
                       help="Path to screenshot (default: RESPAWN.png)")
    parser.add_argument("--calibrate", action="store_true",
                       help="Run calibration mode with detailed HSV visualization")
    parser.add_argument("--multiple", action="store_true",
                       help="Test multiple images from test_screenshots/")
    parser.add_argument("--grid", action="store_true",
                       help="Test with grid visualization and per-region analysis")
    parser.add_argument("--capture", action="store_true",
                       help="Capture a new screenshot and run grid visualization on it")
    parser.add_argument("--unit-ocr", action="store_true",
                        help="Directly test _run_ocr_in_background on the given image")
    
    args = parser.parse_args()
    
    if args.unit_ocr:
        test_run_ocr_in_background(args.image)
    elif args.capture:
        capture_and_test_with_visualization()
    elif args.multiple:
        test_multiple_images()
    elif args.grid:
        test_with_visualization(args.image)
    else:
        test_respawn_detection(args.image, args.calibrate)


if __name__ == "__main__":
    main()

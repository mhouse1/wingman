"""Test script for GameStateAnalyzer with saved screenshots."""

import cv2
import yaml
import sys
from pathlib import Path

# Add wingman to path
sys.path.insert(0, str(Path(__file__).parent))

from wingman.analyzer import GameStateAnalyzer


def load_config(path="wingman/config.yaml"):
    """Load configuration file."""
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
    frame = cv2.imread(image_path)
    if frame is None:
        print(f"ERROR: Could not load image: {image_path}")
        print("Make sure RESPAWN.png is in the current directory")
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
        print(f"    Status: {'✓ DETECTED' if stats['text_detected'] else '✗ NOT DETECTED'}")
        print(f"\n  Progress Bar Detection:")
        print(f"    Pixels detected: {stats['bar_pixels']}")
        print(f"    Ratio: {stats['bar_ratio']:.6f} (threshold: {stats['bar_threshold']:.6f})")
        print(f"    Status: {'✓ DETECTED' if stats['bar_detected'] else '✗ NOT DETECTED'}")
        
    else:
        # Normal test mode
        print("Analyzing frame...")
        state = analyzer.analyze_frame(frame)
        
        print("\nGame State Analysis:")
        print(f"  Is Respawning: {state['is_respawning']}")
        print(f"  Confidence: {state['respawn_confidence']:.2%}")
        print(f"  Detection Method: {state['respawn_method']}")
        print(f"  Enemy Count: {state['enemy_count']}")
        
        if state['enemies']:
            print(f"  Enemies detected at:")
            for i, (x, y, area) in enumerate(state['enemies'][:5], 1):
                print(f"    {i}. Position: ({x}, {y}), Area: {area}")
            if len(state['enemies']) > 5:
                print(f"    ... and {len(state['enemies']) - 5} more")
        
        print("\n" + "=" * 60)
        if state['is_respawning']:
            print("✓ RESPAWN SCREEN DETECTED!")
        else:
            print("✗ Respawn screen NOT detected")
            print("\nTry running with --calibrate flag to debug:")
            print("  python test_analyzer.py RESPAWN.png --calibrate")
        print("=" * 60)


def test_multiple_images():
    """Test analyzer on multiple screenshots if available."""
    test_dir = Path("test_screenshots")
    
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
        
        state = analyzer.analyze_frame(frame)
        status = "RESPAWN" if state['is_respawning'] else "GAMEPLAY"
        confidence = state['respawn_confidence']
        enemies = state['enemy_count']
        
        print(f"✓ {img_path.name:40s} | {status:8s} | Conf: {confidence:.2%} | Enemies: {enemies}")
    
    print("=" * 60)


def test_with_visualization(image_path="RESPAWN.png"):
    """Test with visual grid overlay to identify regions."""
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
    print(f"\n✓ Saved grid visualization to output_grid.png")
    
    # Test each region
    print(f"\nTesting individual regions (1-36):")
    print("-" * 60)
    respawn_regions = []
    
    for region in range(1, 37):
        region_state = analyzer.analyze_frame(frame, region=region)
        status = "✓ RESPAWN" if region_state['is_respawning'] else "  gameplay"
        confidence = region_state['respawn_confidence']
        print(f"  Region {region:2d}: {status} | Conf: {confidence:.2%}")
        
        if region_state['is_respawning']:
            respawn_regions.append((region, confidence))
    
    print("-" * 60)
    if respawn_regions:
        print(f"\n✓ Respawn detected in region(s): {[r[0] for r in respawn_regions]}")
        best_region = max(respawn_regions, key=lambda x: x[1])
        print(f"  Best match: Region {best_region[0]} (confidence: {best_region[1]:.2%})")
        print(f"\nRecommendation: Use region={best_region[0]} for faster analysis")
        
        # Save grid with best region highlighted
        highlighted_grid = analyzer.draw_grid(frame, highlight_region=best_region[0], 
                                             output_path="output_grid_highlighted.png")
        print(f"✓ Saved highlighted grid to output_grid_highlighted.png")
    else:
        print("\n✗ Respawn NOT detected in any region")



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
    
    args = parser.parse_args()
    
    if args.multiple:
        test_multiple_images()
    elif args.grid:
        test_with_visualization(args.image)
    else:
        test_respawn_detection(args.image, args.calibrate)


if __name__ == "__main__":
    main()

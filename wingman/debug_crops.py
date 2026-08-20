"""debug_crops.py — capture one frame and save each configured crop as a PNG.

Run while MetalStorm is visible on screen:
    uv run --active python wingman/debug_crops.py

Outputs:
    /tmp/wingman_full_annotated.png   — full monitor frame with crop rectangles drawn
    /tmp/wingman_crop_<NAME>.png      — each crop region extracted as its own image
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cv2
import yaml
import time

from wingman.capture import Capture


def load_config():
    cfg_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "wingman", "config.yaml")
    with open(cfg_path) as f:
        return yaml.safe_load(f)


def get_crop_px(coords, gw, gh):
    x1 = int(coords[0][0] * gw)
    y1 = int(coords[0][1] * gh)
    x2 = int(coords[1][0] * gw)
    y2 = int(coords[1][1] * gh)
    return x1, y1, x2, y2


def main():
    cfg = load_config()
    reg = cfg["region"]
    region = (reg["left"], reg["top"], reg["width"], reg["height"])
    gw, gh = region[2], region[3]

    gwo = cfg.get("game_window_offset") or {}
    gwo_x = gwo.get("x")
    gwo_y = gwo.get("y")
    offset = (int(gwo_x), int(gwo_y)) if (gwo_x is not None and gwo_y is not None) else None

    print(f"Capturing frame (game region {gw}×{gh}, configured offset={offset})…")
    cap = Capture(region, cfg.get("monitor", 1), game_window_offset=offset)

    # Give PipeWire a moment to start streaming
    time.sleep(1.5)

    # Pull the raw full-monitor frame directly (bypasses game-detection logic).
    raw = None
    for _ in range(10):
        raw = cap._backend._pull_raw_frame()
        if raw is not None:
            break
        print("  waiting for raw frame…")
        time.sleep(0.5)

    if raw is None:
        print("ERROR: could not capture raw frame — PipeWire portal may not be set up.")
        print("Run 'make setup-capture' first.")
        sys.exit(1)

    fh_raw, fw_raw = raw.shape[:2]
    print(f"Raw monitor frame: {fw_raw}×{fh_raw}")

    # Determine game offset: config override → X11 lookup → manual default.
    if offset is not None:
        ox, oy = offset
        print(f"Using configured offset: ({ox}, {oy})")
    else:
        ox, oy = cap._backend._detect_via_x11() or (0, 0)
        if (ox, oy) == (0, 0):
            print("WARNING: X11 window not found. Showing full raw frame with crops at (0,0).")
            print("Make sure MetalStorm is running, then re-run 'make debug-crops'.")
        else:
            print(f"Game window found via xwininfo at: ({ox}, {oy})")

    # Crop the game-sized region from the raw frame.
    if ox + gw <= fw_raw and oy + gh <= fh_raw:
        frame = raw[oy:oy + gh, ox:ox + gw]
    else:
        print(f"WARNING: offset ({ox},{oy})+{gw}×{gh} out of bounds for {fw_raw}×{fh_raw} — using top-left")
        frame = raw[:gh, :gw]
        ox, oy = 0, 0

    fh, fw = frame.shape[:2]
    print(f"Game crop: {fw}×{fh} at ({ox},{oy})")

    crops_cfg = cfg.get("crops", {})
    annotated = frame.copy()

    colours = [
        (0, 255, 0), (0, 0, 255), (255, 0, 0), (255, 255, 0),
        (0, 255, 255), (255, 0, 255), (128, 255, 0), (0, 128, 255),
    ]

    for i, (name, crop_data) in enumerate(crops_cfg.items()):
        if not isinstance(crop_data, dict):
            continue
        coords = crop_data.get("coords")
        if not coords or len(coords) < 2:
            continue

        x1, y1, x2, y2 = get_crop_px(coords, gw, gh)
        colour = colours[i % len(colours)]

        # Draw on annotated frame
        cv2.rectangle(annotated, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(annotated, name, (x1, max(y1 - 4, 12)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, colour, 1, cv2.LINE_AA)

        # Save individual crop
        if y2 > y1 and x2 > x1 and x2 <= fw and y2 <= fh:
            crop_img = frame[y1:y2, x1:x2]
            crop_path = f"/tmp/wingman_crop_{name}.png"
            cv2.imwrite(crop_path, crop_img)
            print(f"  {name}: ({x1},{y1})→({x2},{y2})  saved → {crop_path}")
        else:
            print(f"  {name}: ({x1},{y1})→({x2},{y2})  OUT OF BOUNDS (frame {fw}×{fh})")

    # Save half-res annotated full frame
    half = cv2.resize(annotated, (fw // 2, fh // 2))
    out_path = "/tmp/wingman_full_annotated.png"
    cv2.imwrite(out_path, half)
    print(f"\nFull annotated frame (half-res) → {out_path}")


if __name__ == "__main__":
    main()

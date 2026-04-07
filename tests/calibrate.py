"""Offline crop calibration tool for Wingman.

Iterates through reference screenshots defined in tests/calibration_map.yaml,
lets the user click two corners per crop, and writes updated percentage
coordinates directly into wingman/config.yaml. No game required.

Usage:
    python tests/calibrate.py                    # calibrate all crops
    python tests/calibrate.py --crop respawn     # recalibrate one crop only

Controls (per crop):
    Click top-left corner, then bottom-right corner  — define/update crop
    S  — skip (keep existing value; disabled when crop has no value yet)
    Q  — quit (saves progress made so far, then exits)

Startup validation:
    1. Every screenshot's pixel dimensions must equal config.yaml region.width × region.height.
    2. Every crop in config.yaml crops: must appear in at least one calibration_map.yaml entry.

Both checks print a clear error or warning; the dimension check aborts the run.
"""

import sys
import os
from pathlib import Path

import cv2
import numpy as np
import yaml

# ---------------------------------------------------------------------------
# Path resolution — works whether run from repo root or tests/
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CONFIG_PATH = _ROOT / "wingman" / "config.yaml"
_MAP_PATH = _HERE / "calibration_map.yaml"
_SCREENSHOTS_DIR = _ROOT / "test_screenshots"

sys.path.insert(0, str(_ROOT))
from wingman.crop_region import CropCoords, load_crops, draw_crops, get_crop


# ---------------------------------------------------------------------------
# Config I/O helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_config(path: Path, cfg: dict) -> None:
    """Write config.yaml preserving key order. Overwrites in place."""
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, default_flow_style=None, sort_keys=False, allow_unicode=True)


def _update_crop_in_config(cfg: dict, name: str, x1: float, y1: float,
                            x2: float, y2: float) -> None:
    """Set crops.<name> in the config dict (in-place)."""
    if "crops" not in cfg or cfg["crops"] is None:
        cfg["crops"] = {}
    cfg["crops"][name] = [[round(x1, 4), round(y1, 4)],
                           [round(x2, 4), round(y2, 4)]]


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def _validate(cfg: dict, calibration: list[dict]) -> bool:
    """Validate dimension match and coverage. Returns False if fatal error."""
    region_w = cfg.get("region", {}).get("width", 0)
    region_h = cfg.get("region", {}).get("height", 0)

    ok = True
    for entry in calibration:
        screenshot = entry.get("screenshot", "")
        img_path = _SCREENSHOTS_DIR / screenshot
        if not img_path.exists():
            print(f"[WARN] screenshot not found: {img_path}")
            continue
        img = cv2.imread(str(img_path))
        if img is None:
            print(f"[WARN] cannot read: {img_path}")
            continue
        h, w = img.shape[:2]
        if w != region_w or h != region_h:
            print(
                f"[ERROR] dimension mismatch: {screenshot} is {w}×{h} "
                f"but config.yaml region is {region_w}×{region_h}.\n"
                f"  Screenshot was taken at a different capture region — percentages would be wrong.\n"
                f"  Either retake the screenshot with the current region, or update region: in config.yaml."
            )
            ok = False

    # Coverage check
    config_crops = set((cfg.get("crops") or {}).keys())
    mapped_crops: set[str] = set()
    for entry in calibration:
        for c in entry.get("crops", []):
            mapped_crops.add(c)
    unmapped = config_crops - mapped_crops
    if unmapped:
        print(f"[WARN] crops in config.yaml with no calibration_map.yaml entry: {sorted(unmapped)}")

    return ok


# ---------------------------------------------------------------------------
# Interactive calibration
# ---------------------------------------------------------------------------

# BGR colours
_GREY_FADED  = (100, 100, 100)
_GREEN       = (0, 200, 0)
_WHITE       = (255, 255, 255)
_YELLOW      = (0, 220, 220)


def _draw_existing(frame: np.ndarray, crops: dict[str, CropCoords],
                    active_name: str) -> np.ndarray:
    """Draw already-defined crops as faded grey boxes; active crop is not drawn here."""
    out = frame.copy()
    h, w = out.shape[:2]
    for name, coords in crops.items():
        if name == active_name:
            continue
        px1, py1 = int(w * coords.x1), int(h * coords.y1)
        px2, py2 = int(w * coords.x2), int(h * coords.y2)
        cv2.rectangle(out, (px1, py1), (px2, py2), _GREY_FADED, 1)
        cv2.putText(out, name, (px1 + 2, py1 + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, _GREY_FADED, 1, cv2.LINE_AA)
    return out


def _calibrate_crop(frame: np.ndarray, crop_name: str,
                     existing_coords: "CropCoords | None",
                     all_crops: dict[str, CropCoords]) -> "tuple[float, float, float, float] | str":
    """Interactive two-click crop selection.

    Returns:
        (x1, y1, x2, y2) on successful selection.
        'skip'            when user presses S (only if existing_coords is set).
        'quit'            when user presses Q.
    """
    WIN = "calibrate"
    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, min(frame.shape[1], 1600), min(frame.shape[0], 1000))

    h, w = frame.shape[:2]
    clicks: list[tuple[int, int]] = []
    result_box: "list[tuple[float, float, float, float] | None]" = [None]
    done = [False]
    action = ["none"]  # 'none' | 'skip' | 'quit' | 'done'

    has_existing = existing_coords is not None

    def _mouse(event, x, y, flags, param):
        if action[0] in ("skip", "quit", "done"):
            return
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((x, y))
            if len(clicks) == 2:
                # Order so (x1,y1) is top-left
                lx = min(clicks[0][0], clicks[1][0])
                ly = min(clicks[0][1], clicks[1][1])
                rx = max(clicks[0][0], clicks[1][0])
                ry = max(clicks[0][1], clicks[1][1])
                result_box[0] = (lx / w, ly / h, rx / w, ry / h)
                action[0] = "done"

    cv2.setMouseCallback(WIN, _mouse)

    skip_hint = "  S=skip" if has_existing else "  S=DISABLED"
    undefined_hint = "" if has_existing else "  [UNDEFINED]"
    base_title = f"Click corners for: {crop_name}{undefined_hint} — Q=quit{skip_hint}"

    while action[0] == "none":
        base = _draw_existing(frame, all_crops, crop_name)

        # Draw existing value for this crop if available
        if has_existing:
            px1 = int(w * existing_coords.x1)
            py1 = int(h * existing_coords.y1)
            px2 = int(w * existing_coords.x2)
            py2 = int(h * existing_coords.y2)
            cv2.rectangle(base, (px1, py1), (px2, py2), _YELLOW, 1)

        # Draw first click point if one click received
        if len(clicks) == 1:
            cv2.circle(base, clicks[0], 5, _GREEN, -1)
            cv2.putText(base, "Now click bottom-right", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, _GREEN, 2, cv2.LINE_AA)
            cv2.setWindowTitle(WIN, f"Click bottom-right for: {crop_name}")
        else:
            cv2.setWindowTitle(WIN, base_title)

        cv2.imshow(WIN, base)
        key = cv2.waitKey(30) & 0xFF

        if key == ord("q") or key == ord("Q"):
            action[0] = "quit"
        elif key == ord("s") or key == ord("S"):
            if has_existing:
                action[0] = "skip"

    if action[0] == "done":
        # Show confirmation overlay
        confirm = _draw_existing(frame, all_crops, crop_name)
        x1, y1, x2, y2 = result_box[0]
        cv2.rectangle(confirm,
                      (int(w * x1), int(h * y1)),
                      (int(w * x2), int(h * y2)),
                      _GREEN, 2)
        cv2.putText(confirm, f"Saved: {crop_name}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.9, _GREEN, 2, cv2.LINE_AA)
        cv2.imshow(WIN, confirm)
        cv2.waitKey(600)

    cv2.destroyAllWindows()

    if action[0] == "done":
        return result_box[0]
    return action[0]  # 'skip' or 'quit'


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Offline crop calibration for Wingman")
    parser.add_argument("--crop", metavar="NAME",
                        help="Calibrate only this crop name (default: all)")
    args = parser.parse_args()

    if not _CONFIG_PATH.exists():
        print(f"[ERROR] config not found: {_CONFIG_PATH}")
        sys.exit(1)
    if not _MAP_PATH.exists():
        print(f"[ERROR] calibration map not found: {_MAP_PATH}")
        sys.exit(1)

    cfg = _load_yaml(_CONFIG_PATH)
    cal_doc = _load_yaml(_MAP_PATH)
    calibration: list[dict] = cal_doc.get("calibration", [])

    if not _validate(cfg, calibration):
        sys.exit(1)

    # Build current crops dict (may be empty on first run)
    current_crops = load_crops(cfg.get("crops") or {}) if cfg.get("crops") else {}

    filter_crop = args.crop  # None = calibrate all

    for entry in calibration:
        screenshot = entry.get("screenshot", "")
        crop_names: list[str] = entry.get("crops", [])

        # Filter to only the requested crop if --crop was given
        if filter_crop:
            crop_names = [c for c in crop_names if c == filter_crop]
            if not crop_names:
                continue

        img_path = _SCREENSHOTS_DIR / screenshot
        if not img_path.exists():
            print(f"[SKIP] screenshot not found: {img_path}")
            continue

        frame = cv2.imread(str(img_path))
        if frame is None:
            print(f"[SKIP] cannot read: {img_path}")
            continue

        print(f"\n--- {screenshot} ---")

        for crop_name in crop_names:
            existing = current_crops.get(crop_name)
            print(f"  Calibrating '{crop_name}' "
                  f"({'existing: ' + str(existing) if existing else 'UNDEFINED'})...")

            result = _calibrate_crop(frame, crop_name, existing, current_crops)

            if result == "quit":
                print("[INFO] Quit — progress saved.")
                sys.exit(0)
            elif result == "skip":
                print(f"  Skipped '{crop_name}' — keeping existing value.")
            else:
                x1, y1, x2, y2 = result
                _update_crop_in_config(cfg, crop_name, x1, y1, x2, y2)
                _save_config(_CONFIG_PATH, cfg)
                # Reload crops so the new value appears in the overlay for subsequent crops
                current_crops = load_crops(cfg.get("crops") or {})
                print(f"  Saved '{crop_name}': [[{x1:.4f}, {y1:.4f}], [{x2:.4f}, {y2:.4f}]]")

    print("\n[DONE] Calibration complete.")


if __name__ == "__main__":
    main()

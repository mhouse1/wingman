"""Offline crop calibration tool for Wingman.

Iterates through reference screenshots defined in tests/calibration_map.yaml,
lets the user click two corners per crop, and writes updated percentage
coordinates directly into wingman/config.yaml. No game required.

Usage:
    python tests/calibrate.py                    # calibrate all crops
    python tests/calibrate.py --crop respawn     # recalibrate one crop only
    python tests/calibrate.py --add-new-crops    # add/update crops from test_screenshots/to_be_added and move calibrated images to test_screenshots/

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
import tkinter as tk
from pathlib import Path

import cv2
import numpy as np
import yaml
from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------------------
# Path resolution — works whether run from repo root or tests/
# ---------------------------------------------------------------------------
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
_CONFIG_PATH = _ROOT / "wingman" / "config.yaml"
_MAP_PATH = _HERE / "calibration_map.yaml"
_SCREENSHOTS_DIR = _ROOT / "test_screenshots"
_TO_BE_ADDED_DIR = _SCREENSHOTS_DIR / "to_be_added"

sys.path.insert(0, str(_ROOT))
from wingman.crop_region import CropCoords, load_crops


# ---------------------------------------------------------------------------
# Config I/O helpers
# ---------------------------------------------------------------------------

def _load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _save_config(path: Path, cfg: dict) -> None:
    with path.open("w", encoding="utf-8") as fh:
        yaml.dump(cfg, fh, default_flow_style=None, sort_keys=False, allow_unicode=True)


def _update_crop_in_config(cfg: dict, name: str, x1: float, y1: float,
                            x2: float, y2: float) -> None:
    if "crops" not in cfg or cfg["crops"] is None:
        cfg["crops"] = {}
    cfg["crops"][name] = [[round(x1, 4), round(y1, 4)],
                           [round(x2, 4), round(y2, 4)]]


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------

def _validate(cfg: dict, calibration: list[dict]) -> bool:
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

    config_crops = set((cfg.get("crops") or {}).keys())
    mapped_crops: set[str] = set()
    for entry in calibration:
        for c in entry.get("crops", []):
            mapped_crops.add(c)
    unmapped = config_crops - mapped_crops
    if unmapped:
        print(f"[WARN] crops in config.yaml with no calibration_map.yaml entry: {sorted(unmapped)}")

    return ok


def _validate_dimensions(cfg: dict, image_paths: list[Path]) -> bool:
    region_w = cfg.get("region", {}).get("width", 0)
    region_h = cfg.get("region", {}).get("height", 0)

    ok = True
    for img_path in image_paths:
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
                f"[ERROR] dimension mismatch: {img_path.name} is {w}x{h} "
                f"but config.yaml region is {region_w}x{region_h}.\n"
                f"  Screenshot was taken at a different capture region - percentages would be wrong.\n"
                f"  Either retake the screenshot with the current region, or update region: in config.yaml."
            )
            ok = False

    return ok


def _move_to_screenshots_root(image_path: Path) -> Path:
    """Move image to test_screenshots/, suffixing name if needed to avoid collision."""
    target = _SCREENSHOTS_DIR / image_path.name
    if not target.exists():
        return image_path.replace(target)

    stem = image_path.stem
    suffix = image_path.suffix
    i = 1
    while True:
        candidate = _SCREENSHOTS_DIR / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return image_path.replace(candidate)
        i += 1


# ---------------------------------------------------------------------------
# Drawing helpers (PIL-based, no cv2 GUI)
# ---------------------------------------------------------------------------

def _bgr_to_pil(frame: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))


def _draw_overlays(pil_img: Image.Image,
                   crops: dict[str, CropCoords],
                   active_name: str,
                   existing_active: "CropCoords | None",
                   first_click: "tuple[int, int] | None",
                   drag_pos: "tuple[int, int] | None") -> Image.Image:
    """Return a copy of pil_img with crop overlays drawn."""
    out = pil_img.copy()
    draw = ImageDraw.Draw(out, "RGBA")
    w, h = out.size

    # Other crops — faded grey
    for name, coords in crops.items():
        if name == active_name:
            continue
        px1, py1 = int(w * coords.x1), int(h * coords.y1)
        px2, py2 = int(w * coords.x2), int(h * coords.y2)
        draw.rectangle([px1, py1, px2, py2], outline=(130, 130, 130), width=1)
        draw.text((px1 + 3, py1 + 2), name, fill=(130, 130, 130))

    # Existing value for active crop — yellow outline
    if existing_active is not None:
        px1 = int(w * existing_active.x1)
        py1 = int(h * existing_active.y1)
        px2 = int(w * existing_active.x2)
        py2 = int(h * existing_active.y2)
        draw.rectangle([px1, py1, px2, py2], outline=(220, 220, 0), width=2)

    # Live drag preview — green semi-transparent fill
    if first_click and drag_pos:
        lx = min(first_click[0], drag_pos[0])
        ly = min(first_click[1], drag_pos[1])
        rx = max(first_click[0], drag_pos[0])
        ry = max(first_click[1], drag_pos[1])
        draw.rectangle([lx, ly, rx, ry], outline=(0, 220, 0), fill=(0, 220, 0, 40), width=2)

    # First click dot
    if first_click:
        cx, cy = first_click
        r = 5
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 220, 0))

    return out


# ---------------------------------------------------------------------------
# Interactive calibration window (tkinter)
# ---------------------------------------------------------------------------

def _calibrate_crop(frame: np.ndarray, crop_name: str,
                     existing_coords: "CropCoords | None",
                     all_crops: dict[str, CropCoords]) -> "tuple[float, float, float, float] | str":
    """Interactive two-click crop selection using tkinter.

    Returns:
        (x1, y1, x2, y2) on successful selection.
        'skip'            when user presses S (only if existing_coords is set).
        'quit'            when user presses Q.
    """
    img_h, img_w = frame.shape[:2]

    # Scale to fit on screen (max 1600×900 display area)
    max_w, max_h = 1600, 900
    scale = min(max_w / img_w, max_h / img_h, 1.0)
    disp_w = int(img_w * scale)
    disp_h = int(img_h * scale)

    pil_full = _bgr_to_pil(frame)
    pil_disp = pil_full.resize((disp_w, disp_h), Image.LANCZOS)

    result: dict = {"action": "none", "box": None}
    state: dict = {"clicks": [], "drag": None}
    has_existing = existing_coords is not None

    # Scale existing coords to display space for overlay drawing
    existing_disp: "CropCoords | None" = None
    if has_existing:
        existing_disp = CropCoords(
            existing_coords.x1, existing_coords.y1,
            existing_coords.x2, existing_coords.y2,
        )

    # Scale all_crops for display (they're already percentage-based, no change needed)
    root = tk.Tk()
    skip_hint = "  S=skip" if has_existing else "  S=disabled"
    undefined_tag = " [UNDEFINED]" if not has_existing else ""
    root.title(f"Crop: {crop_name}{undefined_tag}   |   click top-left then bottom-right   |   Q=quit{skip_hint}")
    root.resizable(False, False)

    canvas = tk.Canvas(root, width=disp_w, height=disp_h, cursor="crosshair")
    canvas.pack()

    status_var = tk.StringVar(value=f"Click TOP-LEFT corner of  '{crop_name}'")
    status_bar = tk.Label(root, textvariable=status_var, anchor="w",
                          bg="#222", fg="#eee", font=("Consolas", 10), pady=4)
    status_bar.pack(fill=tk.X)

    tk_img_ref: list = [None]  # hold reference to avoid GC

    def _redraw(drag_pos=None):
        overlay = _draw_overlays(pil_disp, all_crops, crop_name,
                                  existing_disp,
                                  state["clicks"][0] if state["clicks"] else None,
                                  drag_pos)
        tk_img = ImageTk.PhotoImage(overlay)
        tk_img_ref[0] = tk_img
        canvas.create_image(0, 0, anchor=tk.NW, image=tk_img)

    _redraw()

    def _on_click(event):
        if result["action"] != "none":
            return
        x, y = event.x, event.y
        if len(state["clicks"]) == 0:
            state["clicks"].append((x, y))
            status_var.set(f"Click BOTTOM-RIGHT corner of  '{crop_name}'")
            _redraw()
        else:
            # Second click — compute box in image space then convert to percentages
            x0, y0 = state["clicks"][0]
            lx = min(x0, x) / scale
            ly = min(y0, y) / scale
            rx = max(x0, x) / scale
            ry = max(y0, y) / scale
            result["action"] = "done"
            result["box"] = (lx / img_w, ly / img_h, rx / img_w, ry / img_h)
            root.destroy()

    def _on_motion(event):
        if result["action"] != "none" or not state["clicks"]:
            return
        _redraw(drag_pos=(event.x, event.y))

    def _on_key(event):
        ch = event.char.lower() if event.char else ""
        if ch == "q":
            result["action"] = "quit"
            root.destroy()
        elif ch == "s" and has_existing:
            result["action"] = "skip"
            root.destroy()

    canvas.bind("<Button-1>", _on_click)
    canvas.bind("<Motion>", _on_motion)
    root.bind("<Key>", _on_key)
    root.protocol("WM_DELETE_WINDOW", lambda: (_on_key(type("E", (), {"char": "q"})()),))

    root.mainloop()

    if result["action"] == "done":
        return result["box"]
    return result["action"]  # 'skip' or 'quit'


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Offline crop calibration for Wingman")
    parser.add_argument("--crop", metavar="NAME",
                        help="Calibrate only this crop name (default: all)")
    parser.add_argument("--add-new-crops", action="store_true",
                        help="Calibrate one crop per image from test_screenshots/to_be_added using filename as crop name")
    args = parser.parse_args()

    if not _CONFIG_PATH.exists():
        print(f"[ERROR] config not found: {_CONFIG_PATH}")
        sys.exit(1)
    if not _MAP_PATH.exists():
        print(f"[ERROR] calibration map not found: {_MAP_PATH}")
        sys.exit(1)

    cfg = _load_yaml(_CONFIG_PATH)
    current_crops = load_crops(cfg.get("crops") or {}) if cfg.get("crops") else {}

    if args.add_new_crops:
        if args.crop:
            print("[ERROR] --crop cannot be used together with --add-new-crops")
            sys.exit(1)

        allowed_ext = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
        to_add_images = sorted(
            p for p in _TO_BE_ADDED_DIR.iterdir()
            if p.is_file() and p.suffix.lower() in allowed_ext
        ) if _TO_BE_ADDED_DIR.exists() else []

        if not to_add_images:
            print(f"[ERROR] no images found in: {_TO_BE_ADDED_DIR}")
            sys.exit(1)

        if not _validate_dimensions(cfg, to_add_images):
            sys.exit(1)

        for img_path in to_add_images:
            crop_name = img_path.stem
            frame = cv2.imread(str(img_path))
            if frame is None:
                print(f"[SKIP] cannot read: {img_path}")
                continue

            existing = current_crops.get(crop_name)
            print(f"\n--- {img_path.name} ---")
            print(f"  Calibrating '{crop_name}' "
                  f"({'existing: ' + str(existing) if existing else 'UNDEFINED'})...")

            result = _calibrate_crop(frame, crop_name, existing, current_crops)

            if result == "quit":
                print("[INFO] Quit - progress saved.")
                sys.exit(0)
            elif result == "skip":
                print(f"  Skipped '{crop_name}' - keeping existing value.")
            else:
                x1, y1, x2, y2 = result
                _update_crop_in_config(cfg, crop_name, x1, y1, x2, y2)
                _save_config(_CONFIG_PATH, cfg)
                current_crops = load_crops(cfg.get("crops") or {})
                print(f"  Saved '{crop_name}': [[{x1:.4f}, {y1:.4f}], [{x2:.4f}, {y2:.4f}]]")
                moved_to = _move_to_screenshots_root(img_path)
                print(f"  Moved screenshot to: {moved_to}")
    else:
        cal_doc = _load_yaml(_MAP_PATH)
        calibration: list[dict] = cal_doc.get("calibration", [])

        if not _validate(cfg, calibration):
            sys.exit(1)

        filter_crop = args.crop

        for entry in calibration:
            screenshot = entry.get("screenshot", "")
            crop_names: list[str] = entry.get("crops", [])

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
                    print("[INFO] Quit - progress saved.")
                    sys.exit(0)
                elif result == "skip":
                    print(f"  Skipped '{crop_name}' - keeping existing value.")
                else:
                    x1, y1, x2, y2 = result
                    _update_crop_in_config(cfg, crop_name, x1, y1, x2, y2)
                    _save_config(_CONFIG_PATH, cfg)
                    current_crops = load_crops(cfg.get("crops") or {})
                    print(f"  Saved '{crop_name}': [[{x1:.4f}, {y1:.4f}], [{x2:.4f}, {y2:.4f}]]")

    print("\n[DONE] Calibration complete.")


if __name__ == "__main__":
    main()

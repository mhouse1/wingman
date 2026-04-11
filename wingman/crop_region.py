"""Percentage-coordinate crop regions for screen scanning.

All crop coordinates are fractions of the capture frame dimensions (0.0–1.0).
Coordinate order is screen convention: x (horizontal) before y (vertical).
In NumPy indexing this maps to frame[y_start:y_end, x_start:x_end].

This module has no imports from the rest of Wingman — only numpy, cv2, and stdlib.
It can be used by any screen-scanning or OCR system independently.
"""

from typing import NamedTuple

import cv2
import numpy as np


class CropCoords(NamedTuple):
    """Percentage-coordinate bounding box for a named screen region.

    All values are fractions of the capture frame (0.0–1.0).
    x is horizontal (left→right), y is vertical (top→bottom).

    text: OCR substrings to match (any hit → detected). None or empty means
          click-only — no OCR scanning is performed for this crop.
    """
    x1: float
    y1: float
    x2: float
    y2: float
    text: "list[str] | None" = None


def get_crop(frame: np.ndarray, x1: float, y1: float,
             x2: float, y2: float) -> np.ndarray:
    """Extract a percentage-coordinate crop from a full frame.

    All coordinates are fractions of the frame dimensions (0.0–1.0).
    Coordinate order follows screen convention: x (horizontal) before y (vertical).
    In NumPy indexing this maps to frame[y_start:y_end, x_start:x_end].

    Args:
        frame: Full capture frame as a numpy array.
        x1: Left edge as a fraction of frame width.
        y1: Top edge as a fraction of frame height.
        x2: Right edge as a fraction of frame width.
        y2: Bottom edge as a fraction of frame height.

    Returns:
        Cropped region as a numpy array.
    """
    h, w = frame.shape[:2]
    return frame[int(h * y1):int(h * y2), int(w * x1):int(w * x2)]


def load_crops(crops_cfg: dict) -> "dict[str, CropCoords]":
    """Load and validate crop coordinates from a config dict.

    Supports two entry formats:

      List format (legacy):
        name:
        - [x1, y1]
        - [x2, y2]

      Dict format (with optional OCR text):
        name:
          coords:
          - [x1, y1]
          - [x2, y2]
          text: [TOKEN1, TOKEN2]   # optional; omit for click-only crops

    All coordinate values must be in [0.0, 1.0] with x1 < x2 and y1 < y2.

    Args:
        crops_cfg: Raw dict from config.yaml crops: section.

    Returns:
        Dict mapping crop names to validated CropCoords named tuples.

    Raises:
        ValueError: If any crop definition is invalid.
    """
    result: "dict[str, CropCoords]" = {}
    for name, entry in crops_cfg.items():
        if isinstance(entry, dict):
            raw_coords = entry.get("coords")
            raw_text = entry.get("text")
            text = [str(t) for t in raw_text] if raw_text else None
        else:
            raw_coords = entry
            text = None
        try:
            (x1, y1), (x2, y2) = raw_coords
            x1, y1, x2, y2 = float(x1), float(y1), float(x2), float(y2)
        except (TypeError, ValueError) as e:
            raise ValueError(
                f"crops.{name}: expected [[x1,y1],[x2,y2]], got {raw_coords!r}"
            ) from e
        errors = []
        for label, val in [("x1", x1), ("y1", y1), ("x2", x2), ("y2", y2)]:
            if not (0.0 <= val <= 1.0):
                errors.append(f"{label}={val!r} must be in [0.0, 1.0]")
        if x1 >= x2:
            errors.append(f"x1={x1!r} must be less than x2={x2!r}")
        if y1 >= y2:
            errors.append(f"y1={y1!r} must be less than y2={y2!r}")
        if errors:
            raise ValueError(f"crops.{name}: " + "; ".join(errors))
        result[name] = CropCoords(x1, y1, x2, y2, text)
    return result


# Distinct BGR colours for draw_crops — cycles if there are more crops than colours
_CROP_COLOURS = [
    (0, 255, 0),    # green
    (0, 128, 255),  # orange
    (255, 0, 255),  # magenta
    (0, 255, 255),  # yellow
    (255, 128, 0),  # blue
    (128, 0, 255),  # purple
    (0, 200, 200),  # teal
    (255, 200, 0),  # cyan
]


def draw_crops(frame: np.ndarray,
               crops: "dict[str, CropCoords]") -> np.ndarray:
    """Draw labelled bounding boxes for all named crops on a copy of the frame.

    Each crop is drawn as a coloured rectangle with its name in the top-left corner.
    Colours cycle through a fixed palette so each crop gets a distinct colour.

    Args:
        frame: Full capture frame as a numpy array (BGR).
        crops: Dict of crop name → CropCoords as returned by load_crops().

    Returns:
        Copy of frame with crop overlays drawn.
    """
    out = frame.copy()
    h, w = out.shape[:2]
    for i, (name, coords) in enumerate(crops.items()):
        colour = _CROP_COLOURS[i % len(_CROP_COLOURS)]
        px1 = int(w * coords.x1)
        py1 = int(h * coords.y1)
        px2 = int(w * coords.x2)
        py2 = int(h * coords.y2)
        cv2.rectangle(out, (px1, py1), (px2, py2), colour, 2)
        # Label background for legibility on any background colour
        label = name
        (lw, lh), baseline = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.55, 1)
        cv2.rectangle(out, (px1, py1 - lh - baseline - 4), (px1 + lw + 2, py1), colour, -1)
        cv2.putText(out, label, (px1 + 1, py1 - baseline - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 1, cv2.LINE_AA)
    return out


def crop_centre(coords: CropCoords,
                frame_w: int, frame_h: int,
                abs_left: int, abs_top: int) -> "tuple[int, int]":
    """Compute the absolute screen click target at the centre of a crop.

    Args:
        coords: CropCoords percentage-coordinate bounding box.
        frame_w: Capture frame width in pixels.
        frame_h: Capture frame height in pixels.
        abs_left: Absolute screen X of the capture frame left edge.
        abs_top: Absolute screen Y of the capture frame top edge.

    Returns:
        (abs_x, abs_y) absolute screen coordinates of the crop centre.
    """
    abs_x = int(abs_left + ((coords.x1 + coords.x2) / 2) * frame_w)
    abs_y = int(abs_top + ((coords.y1 + coords.y2) / 2) * frame_h)
    return abs_x, abs_y

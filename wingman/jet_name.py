"""Which jet a round is flown in, read off the lobby (operator, 2026-10-31, ADR 167).

The lobby labels the selected jet above it: a class icon, the name, then a gold
trophy icon and the jet's trophies. The label is centred, so its name starts
anywhere from x 793 to 881 of 1920 px and ends where the name does, and OCR can
run the name into what follows ("Su-35 SUPER FLANKER '/683"). So the crop is a
wide band, and it is cut at the trophy icon before it is read. No icon means no
label, and no reading: a round without a jet beats a round under the wrong one.

The game keeps the selected jet in none of the PlayerPrefs wingman reads
(wingman/game_stats.py), so it is read off the screen. `read_jet` runs on the
analyzer's OCR pool with that thread's reader (`GameStateAnalyzer.run_ocr`).
"""

from __future__ import annotations

import re
import string
from typing import NamedTuple

import cv2
import numpy as np

CROP = "JET_NAME"

# Where `jet_name.capture` saves the band each name was read from.
CAPTURE_DIR = "test_screenshots/jet_name"

_ALLOWLIST = string.ascii_letters + string.digits + "-/. "
# Words of letters and digits, with marks inside them: J-10 FIREBIRD, F/A-18F, M.III.
_WORD = r"[A-Z0-9]+(?:[-/.][A-Z0-9]+)*"
_NAME_RE = re.compile(rf"{_WORD}(?: {_WORD})*")
_MIN_NAME_LEN = 3
# OCR reads the slash of F/A-18 as a letter, on all 12 scalings and thresholds tried
# on live frames (2026-10-31: FIA-18F SUPER HORNET), so the text is corrected.
_MISREAD_SLASH_RE = re.compile(r"^F[IL1]A-")

# The trophy icon's gold, in OpenCV's HSV: hue 22 to 23 on every lobby frame of
# 2026-09 to 2026-10. The J-10's orange class icon falls outside it.
_GOLD_LOW = (20, 110, 130)
_GOLD_HIGH = (27, 255, 255)
# A column is gold with this many gold pixels, and the icon is 16 to 22 columns
# wide at 1920 px: anything narrower is a stray pixel of the scenery behind.
_GOLD_ROWS = 3
_ICON_MIN_COLUMNS = 8


class JetRead(NamedTuple):
    """One frame's read of the label: the jet's name or None, and OCR's text."""

    name: "str | None"
    text: str
    band: object = None


def parse_name(text: str) -> "str | None":
    """The name in the game's capitals, so that OCR's case cannot split a jet in
    two on the dashboard; None unless it reads as a name."""
    name = _MISREAD_SLASH_RE.sub("F/A-", " ".join(text.upper().split()))
    if len(name) < _MIN_NAME_LEN or not any(c.isalpha() for c in name):
        return None
    return name if _NAME_RE.fullmatch(name) else None


def file_stem(name: str) -> str:
    """The name, safe in a file name: the slash of F/A-18F is a directory there."""
    return re.sub(r"[^A-Z0-9-]+", "-", name)


def name_band(frame, crop):
    """The crop up to the trophy icon, or None when it holds no icon."""
    h, w = frame.shape[:2]
    region = frame[int(crop.y1 * h):int(crop.y2 * h), int(crop.x1 * w):int(crop.x2 * w)]
    if region.size == 0:
        return None
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, _GOLD_LOW, _GOLD_HIGH) > 0
    columns = np.flatnonzero(gold.sum(axis=0) >= _GOLD_ROWS)
    scale = w / 1920.0
    for run in np.split(columns, np.flatnonzero(np.diff(columns) > 2) + 1):
        if run.size and run[-1] - run[0] + 1 >= _ICON_MIN_COLUMNS * scale:
            return region[:, :int(run[0])]
    return None


def read_jet(reader, frame, crop) -> JetRead:
    """The jet named on a lobby frame, read at double size."""
    band = name_band(frame, crop)
    if band is None or band.size == 0:
        return JetRead(None, "")
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    boxes = reader.readtext(gray, detail=1, paragraph=False, workers=0, allowlist=_ALLOWLIST)
    text = " ".join(str(word) for _x, word in
                    sorted((min(p[0] for p in box), word) for box, word, _conf in boxes))
    return JetRead(parse_name(text), text, band)

"""ADR 084: real-OCR verification of the stall-recovery crops.

Guards calibration drift. The gate logic in tests/test_stall_recovery.py decides
WHEN to act; this decides whether the crop can see the screen at all. A crop
that stops matching fails silently in production — the stall simply never
recovers — so it needs a test that runs OCR for real.

Marked @pytest.mark.slow so it is excluded from the default `make test` run.

Usage: uv run pytest tests/test_stall_crops_ocr.py -m slow -v
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import _process_crop_region
from constants import CONFIG_PATH

pytestmark = pytest.mark.slow

SCREENSHOT_DIR = Path("test_screenshots")

# (crop name, screenshot, the exact string OCR is expected to yield)
STALL_CASES = [
    ("STALL_AIRCRAFT",        "STALL_AIRCRAFT.png",        "AIRCRAFT"),
    ("STALL_EXIT_TO_DESKTOP", "STALL_EXIT_TO_DESKTOP.png", "CANCEL"),
    ("STALL_MULTI_PLAYER",    "STALL_MULTI_PLAYER.png",    "X"),
    ("STALL_RETRY",           "STALL_RETRY.png",           "RETRY"),
    # ADR 093: the PROFILE overlay that livelocked a session for 110 minutes.
    ("STALL_PROFILE",         "STALL_PROFILE.png",         "PROFILE"),
]


def _config():
    with CONFIG_PATH.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load(name):
    path = SCREENSHOT_DIR / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    img = cv2.imread(str(path))
    if img is None:
        pytest.skip(f"{path} unreadable")
    if not np.any(img):
        pytest.skip(f"{path} is an all-black placeholder")
    return img


def _coords(cfg, crop):
    c = cfg["crops"][crop]["coords"]
    return (c[0][0], c[0][1], c[1][0], c[1][1])


@pytest.mark.parametrize("crop,screenshot,expected", STALL_CASES)
def test_stall_crop_detects_its_marker(crop, screenshot, expected):
    cfg = _config()
    tokens = cfg["crops"][crop].get("text") or []
    assert tokens, f"{crop} has no text matchers — coords alone never match"
    detected, _, text = _process_crop_region(_load(screenshot), _coords(cfg, crop), tokens)
    assert detected, f"{crop} failed to detect on {screenshot} (OCR read {text!r})"
    assert expected in (text or ""), f"{crop} read {text!r}, expected to contain {expected!r}"


@pytest.mark.parametrize("crop,screenshot,_expected", STALL_CASES)
def test_stall_crop_does_not_fire_on_a_clean_lobby(crop, screenshot, _expected):
    """A crop that matches the ordinary lobby would act during healthy play.

    The gate should prevent that anyway, but a crop this loose is worth pinning
    independently — STALL_MULTI_PLAYER matches the single character 'X'. Verified
    2026-08-20: the red squad-exit X is absent from ordinary lobby frames, so
    this crop does discriminate rather than relying on the gate alone.
    """
    cfg = _config()
    lobby = SCREENSHOT_DIR / "NEW_FLIGHT_PASS.png"   # a normal lobby frame
    if not lobby.exists():
        pytest.skip("no lobby reference screenshot")
    img = cv2.imread(str(lobby))
    if img is None or not np.any(img):
        pytest.skip("lobby reference unusable")
    detected, _, text = _process_crop_region(
        img, _coords(cfg, crop), cfg["crops"][crop].get("text") or [])
    assert not detected, f"{crop} fired on an ordinary lobby frame (read {text!r})"

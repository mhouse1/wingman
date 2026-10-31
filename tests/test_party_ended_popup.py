"""The "That party has ended." popup (2026-10-19 09:14).

It opened over the lobby at a start with one OK button. No lobby popup matched
it and the generic close recovery (ADR 146) looks for a white cross, so the
start stayed in GAME_UNKNOWN, logging a classification timeout every tick,
until OK was clicked by hand. PARTY_ENDED reads the message; PARTY_ENDED_OK,
the button under it, is what the popup handler clicks.

The screenshot is the frame of that start (test_screenshots/PARTY_ENDED.png,
local like the rest of that corpus: the tests skip without it).
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import GameState, _STATE_CROPS, _process_crop_region
from wingman.main import _POPUP_CLICK_TARGETS
from constants import CONFIG_PATH

SHOT = Path("test_screenshots/PARTY_ENDED.png")
LOBBY = Path("test_screenshots/NEW_FLIGHT_PASS.png")   # a normal lobby frame


def _crops():
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["crops"]


def _box(crop):
    (x1, y1), (x2, y2) = crop["coords"]
    return x1, y1, x2, y2


def _load(path):
    if not path.exists():
        pytest.skip(f"{path} not present")
    img = cv2.imread(str(path))
    if img is None or not np.any(img):
        pytest.skip(f"{path} unreadable or blank")
    return img


def test_party_ended_is_a_lobby_popup_that_clicks_its_ok_button():
    crops = _crops()
    assert "PARTY_ENDED" in _STATE_CROPS[GameState.GAME_LOBBY]
    assert crops["PARTY_ENDED"].get("text"), "the message crop needs text to match"
    assert _POPUP_CLICK_TARGETS["PARTY_ENDED"] == "PARTY_ENDED_OK"
    assert "PARTY_ENDED_OK" in crops


def test_the_ok_crop_sits_on_the_button():
    img = _load(SHOT)
    h, w = img.shape[:2]
    x1, y1, x2, y2 = _box(_crops()["PARTY_ENDED_OK"])
    button = img[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]
    white = (button > 230).all(axis=2)
    assert white.mean() > 0.9, f"only {white.mean():.0%} of the OK crop is the white button"
    # The click point itself is on the dark "OK" lettering; the white button
    # surrounds it on all four sides.
    cy, cx = white.shape[0] // 2, white.shape[1] // 2
    for dy, dx in ((0, -100), (0, 100), (-20, 0), (20, 0)):
        assert white[cy + dy, cx + dx], f"no button {dx:+d},{dy:+d} px from the click point"


@pytest.mark.slow
def test_the_message_crop_reads_the_popup():
    crop = _crops()["PARTY_ENDED"]
    detected, _, text = _process_crop_region(_load(SHOT), _box(crop), crop["text"])
    assert detected, f"PARTY_ENDED did not match (OCR read {text!r})"


@pytest.mark.slow
def test_the_message_crop_does_not_fire_on_a_clean_lobby():
    crop = _crops()["PARTY_ENDED"]
    detected, _, text = _process_crop_region(_load(LOBBY), _box(crop), crop["text"])
    assert not detected, f"PARTY_ENDED matched an ordinary lobby (OCR read {text!r})"

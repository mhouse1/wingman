"""Code Review 019 cycle 24: the party's red X (STALL_MULTI_PLAYER).

2026-10-18, account 1: invited into a two-player squad that never
started a battle, wingman decided to leave it ("this squad has produced no battle
for 32 min ... leaving it via the red X") and then logged "Stall recovery:
'STALL_MULTI_PLAYER' not found" every 5 s until the liveness guard ended the
session. The red X was on screen at about x 853 px; the crop looked at 735 to
783 px.

2026-11-02: the game moved the red X to the PARTY panel at the right, under
INVITE TO PARTY. The fixture is that panel from screenshot_20261102_231819
(x 1500 to 1920, y 60 to 420 px), a two-player party.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import _process_crop_region

FIXTURE = Path("tests/fixtures/lobby_party_panel_red_x.png")
FIXTURE_ORIGIN = (1500, 60)
SOLO_LOBBY = Path("test_screenshots/integration_test/P1_000_LOBBY_PLAY.png")
W, H = 1920, 1200


def _box():
    c = yaml.safe_load(Path(CONFIG_PATH).read_text())["crops"]["STALL_MULTI_PLAYER"]["coords"]
    return (c[0][0], c[0][1], c[1][0], c[1][1])


def _party_lobby():
    panel = cv2.imread(str(FIXTURE))
    if panel is None:
        pytest.skip(f"{FIXTURE} missing")
    x, y = FIXTURE_ORIGIN
    frame = np.zeros((H, W, 3), np.uint8)
    frame[y:y + panel.shape[0], x:x + panel.shape[1]] = panel
    return frame


@pytest.mark.slow
def test_the_red_x_is_read_on_the_party_lobby():
    detected, _, text = _process_crop_region(_party_lobby(), _box(), ["X"])
    assert detected and text == "X"


@pytest.mark.slow
def test_a_solo_lobby_has_no_red_x():
    frame = cv2.imread(str(SOLO_LOBBY))
    if frame is None:
        pytest.skip(f"{SOLO_LOBBY} missing")
    detected, _, _ = _process_crop_region(frame, _box(), ["X"])
    assert not detected

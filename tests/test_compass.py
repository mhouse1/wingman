"""Compass heading from the minimap rim (Design 017 phase 4, shadow)."""

import logging
import math
import re
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer
from wingman.compass import CompassReader, fmt_heading
from wingman.config_schema import schema_default
from wingman.crop_region import get_crop, load_crops
from wingman.state import GameState
from wingman.tick_handlers import BehaviorTreeHandler

CONFIG_PATH = "wingman/config.yaml"
_SIZE = 320                     # the MINIMAP crop at 1920 by 1200
_N_BGR = (0, 165, 255)          # the letter's orange: hue 19
_ROCK_BGR = (40, 110, 230)      # canyon rock seen through the rim: hue 11
_ENEMY_BGR = (0, 220, 255)      # an enemy marker's yellow: hue 26


def _minimap(*blobs, backdrop=(90, 70, 50)):
    """A minimap crop with coloured blobs on its rim: (bearing_deg, bgr, radius_px)."""
    img = np.full((_SIZE, _SIZE, 3), backdrop, np.uint8)
    centre = (_SIZE - 1) / 2.0
    for bearing, bgr, size in blobs:
        r = 0.945 * _SIZE / 2.0
        x = centre + r * math.sin(math.radians(bearing))
        y = centre - r * math.cos(math.radians(bearing))
        cv2.circle(img, (int(round(x)), int(round(y))), size, bgr, -1)
    return img


@pytest.mark.parametrize("bearing, heading", [(0, 0), (90, 270), (150, 210), (-120, 120), (180, 180)])
def test_heading_is_read_from_where_the_orange_n_sits(bearing, heading):
    got = CompassReader().heading(_minimap((bearing, _N_BGR, 5)))
    assert got is not None
    assert abs((got - heading + 180) % 360 - 180) < 1.5


def test_two_candidates_read_as_nothing():
    """A wrong heading is worse than none, so the reader does not pick one."""
    assert CompassReader().heading(_minimap((30, _N_BGR, 5), (200, _N_BGR, 5))) is None


def test_rock_and_enemy_markers_on_the_rim_are_not_the_letter():
    """2026-10-03: over orange rock the loose rule took redder, wider blobs for N."""
    crop = _minimap((100, _N_BGR, 5), (20, _ROCK_BGR, 5), (250, _ENEMY_BGR, 5))
    got = CompassReader().heading(crop)
    assert got is not None and abs(got - 260) < 1.5


def test_pieces_of_markers_on_the_rim_are_too_small_to_be_the_letter():
    """2026-10-04 18:15: markers sitting on the rim left orange pieces of 14 to
    37 px in the letter's ring beside an N of 68 to 90 px. Each counted as a
    second candidate, so the heading read as nothing, for 56 s in level flight,
    and the survey flew unsteered to the arena's rim."""
    crop = _minimap((100, _N_BGR, 5), (20, _N_BGR, 3), (27, _N_BGR, 2), (250, _N_BGR, 3))
    got = CompassReader().heading(crop)
    assert got is not None and abs(got - 260) < 1.5


def test_an_orange_backdrop_flooding_the_rim_reads_as_nothing():
    assert CompassReader().heading(_minimap(backdrop=_N_BGR)) is None


def test_no_letter_and_no_picture_read_as_nothing():
    reader = CompassReader()
    assert reader.heading(_minimap()) is None
    assert reader.heading(np.zeros((0, 0, 3), np.uint8)) is None
    assert reader.heading(None) is None


def test_a_disabled_reader_is_reported_and_defaults_come_from_the_schema():
    assert CompassReader({"enabled": False}).enabled is False
    assert CompassReader().enabled is schema_default("minimap.compass.enabled")


def test_the_log_field():
    assert fmt_heading(213.6) == "214"
    assert fmt_heading(359.93) == "0"       # never "360"
    assert fmt_heading(None) == "n/a"
    assert fmt_heading(None, "padlock") == "n/a(padlock)"


# Archived full-size frames, each checked by eye against the letters round its rim.
@pytest.mark.parametrize("path, heading", [
    ("test_screenshots/integration_test/P1_030_BATTLE_HUD_MISSILES_4.png", 0),     # N at the top
    ("test_screenshots/GAME_BATTLE_ENEMY_AT_NOSE_DOWN.png", 148),                  # top between SE and S
])
def test_real_frames(path, heading):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    frame = cv2.imread(path)
    assert frame is not None, path
    crops = load_crops(cfg["crops"])
    got = CompassReader(cfg["minimap"]["compass"]).heading(get_crop(frame, *crops["MINIMAP"][:4]))
    assert got is not None
    assert abs((got - heading + 180) % 360 - 180) < 4


def test_the_old_minimap_with_white_letters_reads_as_nothing():
    """Before the 2026-09-02 minimap, N was white like the other letters."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    frame = cv2.imread("test_screenshots/GAME_BATTLE_loiter.png")
    crops = load_crops(cfg["crops"])
    assert CompassReader().heading(get_crop(frame, *crops["MINIMAP"][:4])) is None


# --- the BT log line ---------------------------------------------------------

_BATTLE_FRAME = "test_screenshots/integration_test/P1_030_BATTLE_HUD_MISSILES_4.png"     # N at the top


@pytest.fixture
def analyzer():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        a = GameStateAnalyzer(yaml.safe_load(f))
    a.state = GameState.GAME_BATTLE.name
    a._game_battle_alive = True         # health is being read: the battle HUD is up
    try:
        yield a
    finally:
        a.cleanup()


def _hdg(analyzer, caplog, state, padlock):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = padlock
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    with caplog.at_level(logging.DEBUG, logger="wingman.tick_handlers"):
        handler.tick(cv2.imread(_BATTLE_FRAME), state, {"health": 100})
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("BT[") and "selected=" in r.getMessage()]
    assert len(lines) == 1
    return re.search(r" hdg=(\S+)", lines[0]).group(1)


def test_the_bt_line_carries_the_heading_in_battle(analyzer, caplog):
    assert _hdg(analyzer, caplog, GameState.GAME_BATTLE, False) in ("0", "1", "359")


@pytest.mark.parametrize("padlock", [True, None])
def test_with_the_padlock_on_or_unconfirmed_the_compass_is_not_logged_as_a_heading(analyzer, caplog, padlock):
    """The minimap follows the camera, so the compass is the camera's bearing."""
    assert _hdg(analyzer, caplog, GameState.GAME_BATTLE, padlock) == "n/a(padlock)"


def test_no_heading_outside_battle_or_while_health_is_not_being_read(analyzer, caplog):
    assert _hdg(analyzer, caplog, GameState.GAME_LOBBY, False) == "n/a"
    caplog.clear()
    analyzer._game_battle_alive = False
    assert _hdg(analyzer, caplog, GameState.GAME_BATTLE, False) == "n/a"

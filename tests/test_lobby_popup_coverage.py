"""Every dismissible GAME_LOBBY crop must actually be scanned.

A calibrated crop that no scanner looks at is invisible capability. On
2026-08-22 a PILOT LEVEL UP screen ("Tap Here to Continue") stranded the lobby
for 40 minutes: `TAP_HERE_TO_CONTINUE` was declared in the GAME_LOBBY crop set
and calibrated in config.yaml, but was absent from the lobby quick-scan's
popup list, so it was never once scanned in the session.

Same shape as ADR 087 (Research 008, Lesson 1): the capability existed, its
trigger excluded the situation it was for.

Usage: uv run pytest tests/test_lobby_popup_coverage.py -q
"""

import re
from pathlib import Path

import yaml

from wingman.analyzer import GameState, _STATE_CROPS
from constants import CONFIG_PATH

SRC = Path("wingman/analyzer.py").read_text(encoding="utf-8")

# Crops that classify the lobby rather than being dismissed on sight.
_CLASSIFIERS = {"PLAY", "READY", "UNREADY", "CANCEL"}


def _scanned_popups():
    m = re.search(r"popup_crop_names = \[(.*?)\]", SRC, re.S)
    assert m, "popup_crop_names list not found"
    return set(re.findall(r'"([A-Za-z_]+)"', m.group(1)))


def test_every_dismissible_lobby_crop_is_scanned():
    declared = set(_STATE_CROPS[GameState.GAME_LOBBY]) - _CLASSIFIERS
    missing = declared - _scanned_popups()
    assert not missing, (
        f"declared as GAME_LOBBY crops but never scanned: {sorted(missing)} — "
        "a calibrated crop nothing looks at cannot dismiss anything")


def test_tap_here_to_continue_is_scanned_and_calibrated():
    """The specific regression: the level-up screen's dismiss prompt."""
    assert "TAP_HERE_TO_CONTINUE" in _scanned_popups()
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    crop = cfg["crops"].get("TAP_HERE_TO_CONTINUE")
    assert crop, "TAP_HERE_TO_CONTINUE missing from config crops"
    assert crop.get("text"), "crop has no text tokens to match on"


def test_scanned_popups_are_all_calibrated():
    """A scanned name with no crop is silently skipped at runtime."""
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    uncalibrated = _scanned_popups() - set(cfg["crops"])
    assert not uncalibrated, f"scanned but not calibrated: {sorted(uncalibrated)}"


# The one-line and two-line versions of the same OK dialog. Strips are
# x 780-1140 of 1920x1200 frames: 2026-11-19 19:17 "Matchmaking in progress.
# Unable to join." and the CREATION_FAILED calibration frame.
_DIALOGS = {
    "UNABLE_TO_JOIN": "unable_to_join_dialog_strip.png",
    "CREATION_FAILED": "creation_failed_dialog_strip.png",
}


def _ok_button_rows(strip_name):
    """The rows where the white OK button spans the strip's middle."""
    import cv2
    import numpy as np
    strip = cv2.imread(str(Path(__file__).parent / "fixtures" / strip_name))
    middle = strip[:, 160:200].mean(axis=(1, 2))   # x 940-980: inside the button
    rows = np.flatnonzero(middle > 200)
    rows = rows[(rows > 400) & (rows < 800)]
    return rows.min(), rows.max()


def test_each_ok_crop_sits_on_its_own_dialogs_button():
    """2026-11-19 19:16: the one-line dialog's OK sat above CREATION_FAILED's
    crop, so nothing read it and the lobby stayed blacked out."""
    crops = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["crops"]
    for name, strip in _DIALOGS.items():
        top, bottom = _ok_button_rows(strip)
        (_, y1), (_, y2) = crops[name]["coords"]
        centre = (y1 + y2) / 2 * 1200
        assert top + 15 <= centre <= bottom - 15, (name, top, bottom, centre)
        assert crops[name]["text"] == ["OK"]


def test_the_creation_failed_crop_misses_the_one_line_dialog():
    """Why UNABLE_TO_JOIN exists: the older crop's centre is below the button."""
    crops = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["crops"]
    top, bottom = _ok_button_rows(_DIALOGS["UNABLE_TO_JOIN"])
    (_, y1), (_, y2) = crops["CREATION_FAILED"]["coords"]
    assert (y1 + y2) / 2 * 1200 > bottom - 15

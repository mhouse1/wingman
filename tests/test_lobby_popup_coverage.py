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

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


def _collect_frame(shift_px=0):
    """A 1920x1200 frame holding the 2026-11-20 02:44 button row (y 820-880,
    x 400-1520), optionally with the COLLECT button moved right by shift_px."""
    import cv2
    import numpy as np
    row = cv2.imread(str(Path(__file__).parent / "fixtures" / "event_collect_row.png"))
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    frame[820:880, 400:1520] = row
    if shift_px:
        button = frame[836:865, 1203:1333].copy()
        frame[836:865, 1203:1333] = 0
        frame[836:865, 1203 + shift_px:1333 + shift_px] = button
    return frame


def _collect_band():
    from wingman.crop_region import load_crops
    return load_crops(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["crops"])["EVENT_COLLECT"]


def test_the_collect_click_lands_on_the_button_under_any_day():
    """ADR 196 D9: Day 6's COLLECT at x 1203-1332; Day 7's is one tile right."""
    from wingman.analyzer import locate_collect_button
    for shift in (0, 154):
        found = locate_collect_button(_collect_frame(shift), _collect_band())
        assert found is not None
        cx = (found.x1 + found.x2) / 2 * 1920
        cy = (found.y1 + found.y2) / 2 * 1200
        assert abs(cx - (1267.5 + shift)) <= 4 and 836 <= cy <= 864, (shift, cx, cy)


def test_no_button_no_click():
    import numpy as np
    from wingman.analyzer import locate_collect_button
    assert locate_collect_button(np.zeros((1200, 1920, 3), np.uint8), _collect_band()) is None


def test_main_clicks_the_located_collect_button():
    src = Path("wingman/main.py").read_text(encoding="utf-8")
    branch = src[src.index('if popup == "EVENT_COLLECT":'):]
    branch = branch[:branch.index("click_target = _POPUP_CLICK_TARGETS")]
    assert "analyzer.collect_crop()" in branch
    assert "analyzer.crops[" not in branch


def test_the_second_reveal_click_runs_wherever_the_first_could():
    """ADR 196 D10: 2026-11-20 03:20:31, 'REVEAL_ALL second click suppressed —
    state is GAME_UNKNOWN', and the crate's reward summary stranded a session
    that had just started."""
    src = Path("wingman/main.py").read_text(encoding="utf-8")
    body = src[src.index("def _reveal_all_second_click"):]
    body = body[:body.index("threading.Thread(target=_reveal_all_second_click")]
    assert "not in POPUP_DISMISS_STATES" in body
    assert "!= GameState.GAME_LOBBY" not in body


def test_a_collect_miss_saves_its_frame_once(tmp_path, monkeypatch):
    """ADR 196 D9: 2026-11-20 06:33:33, COLLECT read with no button found and
    no frame to say why. The miss frame is now kept, once per 10 minutes."""
    import copy
    import numpy as np
    import wingman.analyzer as analyzer_module
    from wingman import capture_budget
    a = analyzer_module.GameStateAnalyzer(copy.deepcopy(yaml.safe_load(
        CONFIG_PATH.read_text(encoding="utf-8"))))
    try:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(capture_budget, "admit", lambda *a, **k: True)
        frame = np.zeros((1200, 1920, 3), np.uint8)
        a._save_collect_miss(frame)
        a._save_collect_miss(frame)
        saved = list((tmp_path / "test_screenshots" / "unknown_anomalies").glob("collect_miss_*.png"))
        assert len(saved) == 1
    finally:
        a.cleanup()

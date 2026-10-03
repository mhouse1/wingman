"""HLDD 001 Phase 1 overlay: live_hud.png shows what the terrain detector saw.

Built on the real GameStateAnalyzer, HudRenderer and BehaviorTreeHandler with
the shipped config, so the tint is checked against the detector's own mask and
not against a copy of the sky test.
"""

from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman import hud as hud_module
from wingman.analyzer import GameStateAnalyzer
from wingman.hud import HudRenderer
from wingman.state import GameState
from wingman.tick_handlers import BehaviorTreeHandler

_H, _W = 600, 960
_SKY_HSV = (105, 50, 220)     # inside the shipped terrain_avoidance.sky_hsv
_ROCK_HSV = (15, 150, 90)     # well outside it


@pytest.fixture
def analyzer():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        a = GameStateAnalyzer(yaml.safe_load(f))
    a.state = GameState.GAME_BATTLE.name
    try:
        yield a
    finally:
        a.cleanup()


def _frame(split: float = 0.3):
    """Sky above `split` of the height, rock below it."""
    hsv = np.full((_H, _W, 3), _ROCK_HSV, dtype=np.uint8)
    hsv[: int(_H * split)] = _SKY_HSV
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)


def _wired(analyzer, tmp_path, **terrain):
    cfg = {"enabled": True, "shadow": True, "sky_min_frac": 0.55}
    cfg.update(terrain)
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    handler = BehaviorTreeHandler(
        analyzer, MagicMock(), {"climb": {"terrain_avoidance": cfg}})
    handler.set_hud_renderer(renderer)
    return renderer


def _crop_px(analyzer):
    x1, y1, x2, y2 = analyzer.crops["TERRAIN_FORWARD"][:4]
    return int(_W * x1), int(_H * y1), int(_W * x2), int(_H * y2)


def _drawn(renderer, frame, ts=100.0):
    canvas = frame.copy()
    renderer._draw_terrain(canvas, frame, ts)
    return canvas


def test_detector_fraction_comes_from_the_shared_mask(analyzer):
    frame = _frame()
    mask = analyzer.terrain_sky_mask(frame)
    assert mask is not None
    assert analyzer.detect_terrain_ahead(frame) == pytest.approx(
        np.count_nonzero(mask) / mask.size)


def test_tint_covers_exactly_the_pixels_the_detector_counted(analyzer, tmp_path):
    renderer = _wired(analyzer, tmp_path)
    frame = _frame()
    canvas = _drawn(renderer, frame)
    x1, y1, x2, y2 = _crop_px(analyzer)
    mask = analyzer.terrain_sky_mask(frame)
    changed = np.any(canvas[y1:y2, x1:x2] != frame[y1:y2, x1:x2], axis=2)
    # Compare away from the box outline and its label, which are drawn on top.
    inner = (slice(8, -24), slice(8, -8))
    assert np.array_equal(changed[inner], (mask > 0)[inner])
    assert (mask > 0)[inner].any() and not (mask > 0)[inner].all()


@pytest.mark.parametrize("reading, colour", [
    ((0.90, False, 100.0), hud_module._GREEN),    # clear
    ((0.30, False, 100.0), hud_module._YELLOW),   # low, not yet confirmed
    ((0.30, True, 100.0), hud_module._RED),       # terrain ahead
    ((None, False, 100.0), hud_module._GREY),     # tick took no reading
    ((0.30, True, 100.0 - 60.0), hud_module._GREY),  # stale
    (None, hud_module._GREY),                     # nothing reported yet
])
def test_box_colour_follows_the_reading(analyzer, tmp_path, reading, colour):
    renderer = _wired(analyzer, tmp_path)
    if reading is not None:
        renderer.set_terrain_reading(*reading)
    canvas = _drawn(renderer, _frame(), ts=100.0)
    x1, y1, x2, _ = _crop_px(analyzer)
    assert tuple(int(c) for c in canvas[y1, (x1 + x2) // 2]) == colour


def test_a_stale_alarm_is_not_shown_as_live(analyzer, tmp_path):
    """The pursuit loops render many times between readings. An old TERRAIN
    AHEAD must go grey, not stay red."""
    renderer = _wired(analyzer, tmp_path)
    renderer.set_terrain_reading(0.0, True, 100.0)
    x1, y1, x2, _ = _crop_px(analyzer)
    mid = (y1, (x1 + x2) // 2)
    fresh = _drawn(renderer, _frame(), ts=100.0 + hud_module._TERRAIN_STALE_S - 0.1)
    stale = _drawn(renderer, _frame(), ts=100.0 + hud_module._TERRAIN_STALE_S + 0.1)
    assert tuple(int(c) for c in fresh[mid]) == hud_module._RED
    assert tuple(int(c) for c in stale[mid]) == hud_module._GREY


def test_overlay_is_off_when_the_trigger_is_disabled(analyzer, tmp_path):
    renderer = _wired(analyzer, tmp_path, enabled=False)
    renderer.set_terrain_reading(0.0, True, 100.0)
    frame = _frame()
    assert np.array_equal(_drawn(renderer, frame), frame)


def test_overlay_reaches_live_hud_png(analyzer, tmp_path):
    frame = _frame()
    plain = HudRenderer(str(tmp_path / "plain.png"), interval_sec=0.0)
    plain.maybe_render(frame, None, "GAME_BATTLE", 100, 4, 2).join(timeout=5)
    renderer = _wired(analyzer, tmp_path)
    renderer.set_terrain_reading(0.30, True, 1e12)   # fresh against time.time()
    renderer.maybe_render(frame, None, "GAME_BATTLE", 100, 4, 2).join(timeout=5)
    out = cv2.imread(str(tmp_path / "live_hud.png"))
    base = cv2.imread(str(tmp_path / "plain.png"))
    assert out is not None and out.shape == base.shape == (_H // 2, _W // 2, 3)
    assert not np.array_equal(out, base)


def test_a_failing_overlay_does_not_cost_the_hud(tmp_path):
    def _boom(_frame):
        raise RuntimeError("mask failed")
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer.set_terrain_source(_boom, (0.3, 0.1, 0.7, 0.55), 0.55, True)
    renderer.maybe_render(_frame(), None, "GAME_BATTLE", 100, 4, 2).join(timeout=5)
    assert (tmp_path / "live_hud.png").exists()


# ---------------------------------------------------------------------------
# The same reading on the per-tick BT log line, so a rate can be counted.
# ---------------------------------------------------------------------------

def _tick_line(analyzer, caplog, frame, state, padlock):
    import logging
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = padlock
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    with caplog.at_level(logging.DEBUG, logger="wingman.tick_handlers"):
        handler.tick(frame, state, {"health": 100})
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("BT[") and "selected=" in r.getMessage()]
    assert len(lines) == 1
    return lines[0]


def test_bt_line_logs_the_sky_fraction_every_battle_tick(analyzer, caplog):
    frame = _frame()
    line = _tick_line(analyzer, caplog, frame, GameState.GAME_BATTLE, padlock=False)
    assert f" sky={analyzer.detect_terrain_ahead(frame):.2f} " in line


def test_bt_line_says_when_no_reading_was_taken(analyzer, caplog):
    """Padlock not confirmed off (ADR 142): the tick takes no reading, and the
    line must not repeat an old number."""
    line = _tick_line(analyzer, caplog, _frame(), GameState.GAME_BATTLE, padlock=None)
    assert " sky=n/a " in line

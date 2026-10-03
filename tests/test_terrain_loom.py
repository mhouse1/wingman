"""HLDD 001 Phase 2 (shadow): terrain looming from frame-to-frame motion.

The measurement is checked on pictures whose motion is known exactly (a zoom
about a chosen point, a slide, nothing at all), and the wiring on the real
BehaviorTreeHandler and HudRenderer.
"""

import inspect
import logging
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman import hud as hud_module
from wingman import main as main_module
from wingman.analyzer import GameStateAnalyzer
from wingman.hud import HudRenderer
from wingman.state import GameState
from wingman.terrain_loom import LoomReading, TerrainLoom, fmt_tau
from wingman.tick_handlers import BehaviorTreeHandler

_H, _W = 600, 960
_DT = 0.12


def _texture(seed: int) -> np.ndarray:
    """Ground-like clutter with plenty of corners to track.

    Shapes at random places, so no edge sits at the same pixel from one
    picture to the next (a regular grid would be learned as HUD).
    """
    rng = np.random.default_rng(seed)
    img = np.full((_H, _W), 110, np.uint8)
    for _ in range(500):
        x, y = int(rng.integers(0, _W)), int(rng.integers(0, _H))
        w, h = int(rng.integers(8, 40)), int(rng.integers(8, 40))
        cv2.rectangle(img, (x, y), (x + w, y + h), int(rng.integers(30, 230)), -1)
    img = cv2.GaussianBlur(img, (3, 3), 0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def _zoomed(frame, scale, about):
    cx, cy = about
    m = np.float32([[scale, 0, (1 - scale) * cx], [0, scale, (1 - scale) * cy]])
    return cv2.warpAffine(frame, m, (_W, _H), borderMode=cv2.BORDER_REFLECT)


def _shifted(frame, dx, dy):
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(frame, m, (_W, _H), borderMode=cv2.BORDER_REFLECT)


def _with_hud(frame):
    """Fixed HUD strokes: the same pixels on every frame."""
    out = frame.copy()
    cv2.circle(out, (_W // 2, _H // 2 - 60), 70, (120, 255, 120), 2)
    cv2.line(out, (300, 150), (300, 330), (120, 255, 120), 2)
    cv2.line(out, (660, 150), (660, 330), (120, 255, 120), 2)
    cv2.putText(out, "NO LOCK 1078 KPH", (380, 300), cv2.FONT_HERSHEY_SIMPLEX,
                0.8, (60, 60, 255), 2)
    return out


def _warm(hud=False) -> TerrainLoom:
    """A loom past its warm-up, having seen ten unrelated pictures."""
    loom = TerrainLoom({"enabled": True})
    for seed in range(100, 112):
        a = _texture(seed)
        b = _shifted(a, 2, 0)
        if hud:
            a, b = _with_hud(a), _with_hud(b)
        loom.measure(a, b, _DT)
    return loom


def test_no_reading_until_the_hud_mask_has_warmed_up():
    loom = TerrainLoom({"enabled": True})
    a = _texture(1)
    reading = loom.measure(a, _zoomed(a, 1.03, (_W / 2, _H / 2)), _DT)
    assert reading.status == "warming" and not reading.readable


def test_zoom_about_the_centre_reads_time_to_contact_on_course():
    loom = _warm()
    a = _texture(1)
    reading = loom.measure(a, _zoomed(a, 1.03, (_W / 2, 0.4 * _H)), _DT)
    assert reading.readable
    assert reading.tau == pytest.approx(_DT / 0.03, rel=0.15)     # 4.0 s
    assert reading.fixed == pytest.approx((_W / 2, 0.4 * _H), abs=25)
    assert reading.on_course
    assert len(reading.points) == reading.inliers >= 25


def test_zoom_about_a_point_off_to_the_side_is_passing_not_on_course():
    """The operator's rule: an outline that slides sideways is off the path."""
    loom = _warm()
    a = _texture(2)
    reading = loom.measure(a, _zoomed(a, 1.03, (0.05 * _W, 0.4 * _H)), _DT)
    assert reading.readable and reading.tau is not None
    assert not reading.on_course


def test_a_slide_without_growth_is_not_closing():
    loom = _warm()
    a = _texture(3)
    reading = loom.measure(a, _shifted(a, 6, -2), _DT)
    assert reading.readable and reading.tau is None
    assert fmt_tau(reading) == "inf"


def test_featureless_view_gives_no_reading_never_terrain_ahead():
    """Open sky, water, a dark night: nothing to track is not a verdict."""
    loom = _warm()
    sky = np.full((_H, _W, 3), (200, 150, 90), np.uint8)
    night = np.full((_H, _W, 3), (40, 25, 15), np.uint8)
    for a, b in ((sky, sky.copy() + 1), (night, night.copy() + 1)):
        reading = loom.measure(a, b, _DT)
        assert reading.status == "few-points"
        assert loom.update(reading) is False


def test_the_same_picture_twice_is_not_read_as_not_closing():
    loom = _warm()
    a = _texture(4)
    reading = loom.measure(a, a.copy(), _DT)
    assert reading.status == "same-frame"
    assert fmt_tau(reading) == "n/a(same-frame)"


def test_fixed_hud_strokes_do_not_vote():
    """HUD strokes do not move. Unmasked they would agree on 'no zoom'."""
    loom = _warm(hud=True)
    a = _texture(5)
    reading = loom.measure(_with_hud(a), _with_hud(_zoomed(a, 1.03, (_W / 2, 0.4 * _H))), _DT)
    assert reading.readable
    assert reading.tau == pytest.approx(_DT / 0.03, rel=0.2)
    sky = np.full((_H, _W, 3), (200, 150, 90), np.uint8)
    only_hud = loom.measure(_with_hud(sky), _with_hud(sky.copy() + 1), _DT)
    assert only_hud.status == "few-points"


def _closing(tau=4.0, on_course=True):
    return LoomReading(status="ok", dt=_DT, scale=1.03, tau=tau, on_course=on_course,
                       inliers=100, tracked=120)


def test_warning_needs_consecutive_closing_pairs(caplog):
    loom = TerrainLoom({"enabled": True, "confirm_pairs": 3, "tau_warn_s": 8.0})
    with caplog.at_level(logging.WARNING, logger="wingman.terrain_loom"):
        assert [loom.update(_closing()) for _ in range(3)] == [False, False, True]
        assert loom.update(_closing()) is True
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1 and "SHADOW - not actuating" in lines[0]


@pytest.mark.parametrize("breaker", [
    None,                                   # tick took no reading
    LoomReading(status="few-points"),       # nothing to track
    _closing(tau=20.0),                     # far off
    _closing(on_course=False),              # passing to the side
])
def test_a_gap_or_a_safe_reading_resets_the_streak(breaker):
    loom = TerrainLoom({"enabled": True, "confirm_pairs": 3, "tau_warn_s": 8.0})
    loom.update(_closing())
    loom.update(_closing())
    assert loom.update(breaker) is False
    assert loom.update(_closing()) is False


def test_fmt_tau():
    assert fmt_tau(None) == "n/a"
    assert fmt_tau(_closing(4.04)) == "4.0s"
    assert fmt_tau(_closing(4.04, on_course=False)) == "4.0s/off"


# ---------------------------------------------------------------------------
# Wiring: the real handler tick, and the HUD.
# ---------------------------------------------------------------------------

@pytest.fixture
def analyzer():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        a = GameStateAnalyzer(yaml.safe_load(f))
    a.state = GameState.GAME_BATTLE.name
    try:
        yield a
    finally:
        a.cleanup()


def _handler(analyzer, padlock, grabs):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = padlock
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    frames = iter(grabs)
    grab = MagicMock(side_effect=lambda: next(frames))
    handler.set_terrain_loom(_warm(), grab)
    return handler, grab


def _bt_line(handler, caplog, state):
    with caplog.at_level(logging.DEBUG, logger="wingman.tick_handlers"):
        handler.tick(_texture(9), state, {"health": 100})
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("BT[") and "selected=" in r.getMessage()]
    assert len(lines) == 1
    return lines[0]


def test_tick_grabs_its_own_pair_and_logs_tau_on_the_bt_line(analyzer, caplog):
    a = _texture(6)
    handler, grab = _handler(analyzer, False, [a, _zoomed(a, 1.03, (_W / 2, 0.4 * _H))])
    line = _bt_line(handler, caplog, GameState.GAME_BATTLE)
    assert grab.call_count == 2
    tau = line.rsplit("tau=", 1)[1]
    assert tau.endswith("s") and 0.5 < float(tau[:-1]) < 20.0


@pytest.mark.parametrize("state, padlock", [
    (GameState.GAME_BATTLE, None),      # padlock not confirmed off (ADR 142)
    (GameState.GAME_BATTLE, True),
    (GameState.GAME_LOBBY, False),      # menus have no forward view
])
def test_tick_takes_no_reading_outside_a_forward_battle_view(analyzer, caplog, state, padlock):
    handler, grab = _handler(analyzer, padlock, [])
    line = _bt_line(handler, caplog, state)
    assert grab.call_count == 0
    assert line.endswith("tau=n/a")


def test_a_handler_without_the_loom_grabs_nothing(analyzer, caplog):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = False
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    assert _bt_line(handler, caplog, GameState.GAME_BATTLE).endswith("tau=n/a")


def test_main_wires_the_loom_for_live_runs_only():
    """A replay capture hands out its next scripted screenshot on every grab,
    so an extra pair per tick would shift the whole replay."""
    src = inspect.getsource(main_module.main)
    guard = src.index("if not replay_mode and not capture_mode:\n        behavior_tree.set_terrain_loom(")
    assert guard > 0


def _hud(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    loom = TerrainLoom({"enabled": True})
    renderer.set_loom_source(loom.path_box_pct, loom.tau_warn_s)
    return renderer


def _reading_with_points():
    pts = np.float32([[300, 200], [500, 250], [640, 180]])
    return LoomReading(status="ok", dt=_DT, scale=1.03, tau=4.0, fixed=(480.0, 240.0),
                       on_course=True, inliers=3, tracked=3, points=pts)


def test_hud_draws_the_points_the_reading_tracked(tmp_path):
    renderer = _hud(tmp_path)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0)
    for x, y in ((300, 200), (500, 250), (640, 180)):
        assert tuple(int(c) for c in canvas[y, x]) == hud_module._WHITE
    x1, y1 = int(_W * 0.38), int(_H * 0.30)
    assert tuple(int(c) for c in canvas[y1, x1 + 40]) == hud_module._RED


def test_hud_drops_the_points_of_a_stale_reading(tmp_path):
    renderer = _hud(tmp_path)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0 + hud_module._TERRAIN_STALE_S + 0.1)
    assert tuple(int(c) for c in canvas[200, 300]) == (0, 0, 0)
    x1, y1 = int(_W * 0.38), int(_H * 0.30)
    assert tuple(int(c) for c in canvas[y1, x1 + 40]) == hud_module._GREY


def test_hud_without_a_loom_source_draws_nothing(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0)
    assert not canvas.any()


# ---------------------------------------------------------------------------
# HUD colour mask: strokes that move, which the learned mask cannot catch.
# Colours are the ones measured on live frames (config.yaml hud_mask).
# ---------------------------------------------------------------------------

_HUD_RED = (40, 60, 235)      # BGR; HSV about (3, 211, 235)
_HUD_GREEN = (150, 235, 150)  # BGR; HSV about (60, 92, 235)
_ROCK = (60, 110, 190)        # BGR canyon rock; HSV about (12, 174, 190)


def _with_moving_hud(frame, dx):
    """Nameplates and a lock circle at a position that shifts with `dx`."""
    out = frame.copy()
    for k in range(6):
        x, y = 240 + 90 * k + dx, 110 + 35 * k
        cv2.putText(out, "[A51] BANDIT", (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.6, _HUD_RED, 2)
        cv2.line(out, (x, y + 6), (x + 110, y + 6), _HUD_RED, 2)
    cv2.circle(out, (480 + dx, 230), 70, _HUD_GREEN, 2)
    for k in range(8):
        cv2.line(out, (300 + dx, 120 + 25 * k), (320 + dx, 120 + 25 * k), _HUD_GREEN, 2)
    return out


def _points_on_hud_colour(loom, reading, frame):
    hud = loom.hud_colour_mask(frame, (int(_H * 0.60) - int(_H * 0.12),
                                       int(_W * 0.80) - int(_W * 0.20)))
    ox, oy = reading.hud_origin
    p = np.rint(reading.points).astype(int)
    return int((hud[p[:, 1] - oy, p[:, 0] - ox] > 0).sum())


def test_moving_hud_colours_are_left_out_of_the_tracking():
    loom = _warm()
    a = _texture(7)
    first = _with_moving_hud(a, 0)
    second = _with_moving_hud(_zoomed(a, 1.03, (_W / 2, 0.4 * _H)), 14)
    reading = loom.measure(first, second, _DT)
    assert reading.readable
    assert reading.tau == pytest.approx(_DT / 0.03, rel=0.15)
    assert reading.hud_frac > 0.02
    assert _points_on_hud_colour(loom, reading, second) == 0


def test_without_the_colour_mask_the_moving_hud_is_tracked():
    """The control: with the mask off, the same pair tracks the HUD strokes too."""
    unmasked = TerrainLoom({"enabled": True, "hud_mask": {"enabled": False}})
    for seed in range(100, 112):
        t = _texture(seed)
        unmasked.measure(t, _shifted(t, 2, 0), _DT)
    masked = _warm()
    a = _texture(7)
    first = _with_moving_hud(a, 0)
    second = _with_moving_hud(_shifted(a, 1, 0), 14)   # the HUD moves on its own
    off = unmasked.measure(first, second, _DT)
    on = masked.measure(first, second, _DT)
    assert off.hud_frac == 0.0 and on.hud_frac > 0.02
    assert off.tracked > on.tracked + 30


def test_canyon_rock_is_not_taken_for_hud_red():
    loom = TerrainLoom({"enabled": True})
    rock = np.full((_H, _W, 3), _ROCK, np.uint8)
    assert not loom.hud_colour_mask(rock, (288, 576)).any()
    red = np.full((_H, _W, 3), _HUD_RED, np.uint8)
    green = np.full((_H, _W, 3), _HUD_GREEN, np.uint8)
    assert loom.hud_colour_mask(red, (288, 576)).all()
    assert loom.hud_colour_mask(green, (288, 576)).all()


def test_colour_mask_is_tested_at_full_resolution():
    """A 2 px stroke on a 1920 px frame is 1 px after shrinking, blended with
    what is behind it. Testing after the shrink would miss it."""
    loom = TerrainLoom({"enabled": True})
    frame = np.full((1200, 1920, 3), _ROCK, np.uint8)
    cv2.line(frame, (700, 400), (1200, 400), _HUD_RED, 2)
    mask = loom.hud_colour_mask(frame, (288, 576))
    assert mask[int((400 - 144) / 2), 200:400].all()


def test_hud_darkens_what_the_colour_mask_excluded(tmp_path):
    renderer = _hud(tmp_path)
    mask = np.zeros((288, 576), np.uint8)
    mask[100:120, 200:260] = 255
    reading = LoomReading(status="ok", dt=_DT, scale=1.0, hud_mask=mask,
                          hud_origin=(192, 72), hud_scale=1.0, hud_frac=0.01)
    renderer.set_loom_reading(reading, False, 100.0)
    canvas = np.full((_H, _W, 3), 200, np.uint8)
    renderer._draw_loom(canvas, 100.0)
    assert tuple(int(c) for c in canvas[72 + 110, 192 + 230]) == (60, 60, 60)
    assert tuple(int(c) for c in canvas[72 + 200, 192 + 400]) == (200, 200, 200)


# ---------------------------------------------------------------------------
# A picture the game holds for longer than the pair interval.
# ---------------------------------------------------------------------------

def _pair_from(analyzer, grabs):
    handler, grab = _handler(analyzer, False, grabs)
    pairs, waited = handler._grab_loom_pairs(1)
    (first, second, dt), = pairs
    return handler, grab, first, second, dt, waited


def test_a_moving_picture_takes_two_grabs_and_no_wait(analyzer):
    a = _texture(20)
    b = _shifted(a, 3, 0)
    _, grab, first, second, dt, waited = _pair_from(analyzer, [a, b])
    assert grab.call_count == 2 and waited == 0.0
    assert first is a and second is b
    assert 0.10 < dt < 0.25


def test_a_frozen_picture_is_waited_out_and_the_pair_restarts_from_the_change(analyzer):
    """The pair must not be (frozen, changed): how long the frozen picture had
    been on screen is unknown, so that interval would be a guess."""
    a = _texture(21)
    b = _zoomed(a, 1.02, (_W / 2, 0.4 * _H))
    c = _zoomed(a, 1.05, (_W / 2, 0.4 * _H))
    handler, grab, first, second, dt, waited = _pair_from(
        analyzer, [a, a.copy(), a.copy(), b, c])
    assert grab.call_count == 5
    assert first is b and second is c
    assert waited > 0.0
    assert 0.10 < dt < 0.25                 # the pair interval, not the wait
    reading = handler._loom.measure(first, second, dt)
    assert reading.readable and reading.tau is not None


def test_a_screen_that_never_changes_stays_same_frame_and_the_wait_is_bounded(analyzer):
    a = _texture(22)
    handler, grab = _handler(analyzer, False, [])
    grab.side_effect = lambda: a.copy()
    import time as _time
    t0 = _time.monotonic()
    ((first, second, dt),), waited = handler._grab_loom_pairs(1)
    assert _time.monotonic() - t0 < handler._loom.pair_interval_s + handler._loom.same_frame_wait_s + 0.3
    assert waited >= handler._loom.same_frame_wait_s * 0.9
    assert handler._loom.measure(first, second, dt).status == "same-frame"


def test_no_waiting_when_configured_off(analyzer):
    a = _texture(23)
    handler, grab = _handler(analyzer, False, [a, a.copy()])
    handler._loom.same_frame_wait_s = 0.0
    handler._grab_loom_pairs(1)
    assert grab.call_count == 2


def test_same_view_ignores_changes_outside_the_forward_view():
    loom = TerrainLoom({"enabled": True})
    a = _texture(24)
    b = a.copy()
    b[int(_H * 0.8):, :] = 0                # below the view: HUD, own aircraft
    assert loom.same_view(a, b)
    b[int(_H * 0.3), int(_W * 0.5)] = (1, 2, 3)
    assert not loom.same_view(a, b)


# ---------------------------------------------------------------------------
# Several readings a tick, so the confirm can complete inside one tick.
# ---------------------------------------------------------------------------

def _closing_frames(seed, n):
    base = _texture(seed)
    return [_zoomed(base, 1.0 + 0.03 * k, (_W / 2, 0.4 * _H)) for k in range(n)]


def test_three_pairs_come_from_four_consecutive_frames(analyzer):
    frames = _closing_frames(30, 4)
    handler, grab = _handler(analyzer, False, frames)
    pairs, waited = handler._grab_loom_pairs(3)
    assert grab.call_count == 4 and waited == 0.0
    assert [(a is frames[k], b is frames[k + 1]) for k, (a, b, _) in enumerate(pairs)] == [(True, True)] * 3


def test_a_closing_view_confirms_inside_one_tick(analyzer, caplog):
    """At one reading a tick, three in a row took three ticks (3 to 4.5 s)."""
    handler, grab = _handler(analyzer, False, _closing_frames(31, 4))
    handler._loom.pairs_per_tick = 3
    with caplog.at_level(logging.DEBUG):
        handler.tick(_texture(9), GameState.GAME_BATTLE, {"health": 100})
    assert grab.call_count == 4
    assert handler._loom.warn is True
    warned = [r.getMessage() for r in caplog.records if "LOOM[shadow]" in r.getMessage()]
    assert len(warned) == 1 and "SHADOW - not actuating" in warned[0]
    line = [r.getMessage() for r in caplog.records
            if r.getMessage().startswith("BT[") and "selected=" in r.getMessage()][0]
    taus = line.rsplit("tau=", 1)[1].split(",")
    assert len(taus) == 3 and all(t.endswith("s") for t in taus)


def test_one_tick_without_a_reading_resets_the_streak_once(analyzer):
    handler, _ = _handler(analyzer, None, [])       # padlock unconfirmed: no grabs
    handler._loom.pairs_per_tick = 3
    handler._loom.update(_closing())
    handler._loom.update(_closing())
    handler.tick(_texture(9), GameState.GAME_BATTLE, {"health": 100})
    assert handler._loom.update(_closing()) is False


def test_the_frozen_picture_wait_is_budgeted_per_tick(analyzer):
    a = _texture(32)
    handler, grab = _handler(analyzer, False, [])
    grab.side_effect = lambda: a.copy()
    import time as _time
    t0 = _time.monotonic()
    pairs, waited = handler._grab_loom_pairs(3)
    loom = handler._loom
    assert len(pairs) == 3
    assert waited <= loom.same_frame_wait_s + 0.1
    assert _time.monotonic() - t0 < 3 * loom.pair_interval_s + loom.same_frame_wait_s + 0.5


def test_fmt_taus():
    from wingman.terrain_loom import fmt_taus
    assert fmt_taus([]) == "n/a"
    assert fmt_taus([_closing(4.04), LoomReading(status="few-points")]) == "4.0s,n/a(few-points)"


# ---------------------------------------------------------------------------
# The path box has no bottom edge: a descent expands from below the boresight.
# ---------------------------------------------------------------------------

def test_a_descent_into_the_ground_is_on_course():
    """05:36:04 on 2026-10-03: `6.7s` with the expansion point at (958, 2165)
    on a 1920 by 1200 frame was classed as passing, eight readings in a row,
    and the aircraft hit the ground 10 s later."""
    loom = TerrainLoom({"enabled": True})
    frame = (1200, 1920)
    assert loom.in_path_box((958, 2165), frame)          # far below the frame
    assert loom.in_path_box((975, 1117), frame)          # below the old box
    assert loom.in_path_box((962, 576), frame)           # inside the old box
    assert not loom.in_path_box((1313, -2), frame)       # above: a pull-up's rotation
    assert not loom.in_path_box((1588, 790), frame)      # below but off to the right
    assert not loom.in_path_box((306, 1587), frame)      # below but off to the left


def test_the_closed_box_is_still_available():
    loom = TerrainLoom({"enabled": True, "path_open_below": False})
    assert not loom.in_path_box((958, 2165), (1200, 1920))
    assert loom.in_path_box((962, 576), (1200, 1920))


def test_a_zoom_about_a_point_below_the_view_reads_on_course():
    loom = _warm()
    a = _texture(40)
    reading = loom.measure(a, _zoomed(a, 1.03, (_W / 2, 1.6 * _H)), _DT)
    assert reading.readable and reading.tau is not None
    assert reading.fixed[1] > _H
    assert reading.on_course


def test_hud_draws_the_open_box_without_a_bottom_edge(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    loom = TerrainLoom({"enabled": True})
    renderer.set_loom_source(loom.path_box_pct, loom.tau_warn_s, loom.path_open_below)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0)
    left, top, bottom = int(_W * 0.38), int(_H * 0.30), int(_H * 0.62)
    red = hud_module._RED
    assert tuple(int(c) for c in canvas[top, left + 60]) == red          # top edge
    assert tuple(int(c) for c in canvas[_H - 5, left]) == red            # side runs to the foot
    assert tuple(int(c) for c in canvas[bottom, left + 100]) == (0, 0, 0)  # no bottom edge

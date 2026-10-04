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
    """The operator's first rule: an outline that slides sideways is off the
    path. Still available as `path_open_sides: false`; no longer the shipped
    setting, because live it blocked the warning before terrain deaths."""
    loom = _warm()
    loom.path_open_sides = False
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
    a._game_battle_alive = True         # health is being read: the battle HUD is up
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


def _field(line, name):
    """One `name=value` field of the BT log line, wherever it sits."""
    import re
    return re.search(rf" {name}=(\S+)", line).group(1)


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
    tau = _field(line, "tau")
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
    assert _field(line, "tau") == "n/a"


def test_tick_takes_no_reading_while_health_is_not_being_read(analyzer, caplog):
    """The round-end screen, a respawn, a kill-cam: the state can still say
    battle, and what is on screen is not a forward view to measure."""
    a = _texture(6)
    handler, grab = _handler(analyzer, False, [a, _zoomed(a, 1.03, (_W / 2, 0.4 * _H))])
    analyzer._game_battle_alive = False
    line = _bt_line(handler, caplog, GameState.GAME_BATTLE)
    assert grab.call_count == 0
    assert _field(line, "tau") == "n/a" and _field(line, "shape") == "n/a"


def test_a_handler_without_the_loom_grabs_nothing(analyzer, caplog):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = False
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    assert _field(_bt_line(handler, caplog, GameState.GAME_BATTLE), "tau") == "n/a"


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
    taus = _field(line, "tau").split(",")
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
    loom = TerrainLoom({"enabled": True, "path_open_sides": False})
    frame = (1200, 1920)
    assert loom.in_path_box((958, 2165), frame)          # far below the frame
    assert loom.in_path_box((975, 1117), frame)          # below the old box
    assert loom.in_path_box((962, 576), frame)           # inside the old box
    assert not loom.in_path_box((1313, -2), frame)       # above: a pull-up's rotation
    assert not loom.in_path_box((1588, 790), frame)      # below but off to the right
    assert not loom.in_path_box((306, 1587), frame)      # below but off to the left


def test_the_closed_box_is_still_available():
    loom = TerrainLoom({"enabled": True, "path_open_below": False,
                        "path_open_sides": False})
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
    renderer.set_loom_source(loom.path_box_pct, loom.tau_warn_s, open_below=True)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0)
    left, top, bottom = int(_W * 0.38), int(_H * 0.30), int(_H * 0.62)
    red = hud_module._RED
    assert tuple(int(c) for c in canvas[top, left + 60]) == red          # top edge
    assert tuple(int(c) for c in canvas[_H - 5, left]) == red            # side runs to the foot
    assert tuple(int(c) for c in canvas[bottom, left + 100]) == (0, 0, 0)  # no bottom edge


# ---------------------------------------------------------------------------
# No left or right edge either: only a point above the top edge is off course.
# ---------------------------------------------------------------------------

def test_an_expansion_point_off_to_a_side_is_on_course():
    """06:22, 06:33 and 06:44 on 2026-10-03: low and manoeuvring, short times
    to contact with the expansion point just left of the box at x 730, classed
    as passing until the aircraft hit the ground."""
    loom = TerrainLoom({"enabled": True})
    frame = (1200, 1920)
    for point in ((724, 735), (395, 1024), (664, 1000), (556, 1719),   # 06:22
                  (1, 968), (473, 1490), (707, 791),                   # 06:33
                  (1588, 790)):                                        # right of the box
        assert loom.in_path_box(point, frame), point
    for point in ((606, 128), (883, 330), (949, 138), (1313, -2)):     # above the top edge
        assert not loom.in_path_box(point, frame), point


def test_a_zoom_about_a_point_low_and_to_the_side_reads_on_course():
    loom = _warm()
    a = _texture(41)
    reading = loom.measure(a, _zoomed(a, 1.03, (0.08 * _W, 0.9 * _H)), _DT)
    assert reading.readable and reading.tau is not None
    assert reading.on_course


def test_a_zoom_about_a_point_above_the_view_is_still_passing():
    loom = _warm()
    a = _texture(42)
    reading = loom.measure(a, _zoomed(a, 1.03, (_W / 2, -0.5 * _H)), _DT)
    assert reading.readable and reading.tau is not None
    assert not reading.on_course


def test_hud_draws_only_the_top_edge_when_the_sides_are_open(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    loom = TerrainLoom({"enabled": True})
    renderer.set_loom_source(loom.path_box_pct, loom.tau_warn_s,
                             loom.path_open_below, loom.path_open_sides)
    renderer.set_loom_reading(_reading_with_points(), True, 100.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_loom(canvas, 100.0)
    left, top = int(_W * 0.38), int(_H * 0.30)
    red = hud_module._RED
    assert tuple(int(c) for c in canvas[top, 30]) == red              # runs the full width
    assert tuple(int(c) for c in canvas[top, _W - 30]) == red
    assert tuple(int(c) for c in canvas[_H - 5, left]) == (0, 0, 0)   # no side edges


# ---------------------------------------------------------------------------
# Shapes: the dots grouped into objects with outlines (operator's design).
# An object here is a patch of clutter on a plain background, like a mesa
# against open sky: the background has nothing to track.
# ---------------------------------------------------------------------------

from wingman.terrain_shapes import ShapeReading, TerrainShapes, fmt_shape  # noqa: E402

_SKY = (200, 150, 90)       # plain, and not a HUD colour


def _rock(seed: int) -> np.ndarray:
    """Dense clutter with no flat patch wider than a few pixels, as rock has.

    `_texture` leaves flat areas tens of pixels across. To an outline finder
    those are gaps, and an object made of it reads as several objects.
    """
    rng = np.random.default_rng(seed)
    img = rng.integers(60, 200, (_H // 6, _W // 6), dtype=np.uint8)
    img = cv2.resize(img, (_W, _H), interpolation=cv2.INTER_NEAREST)
    for _ in range(1500):
        x, y = int(rng.integers(0, _W)), int(rng.integers(0, _H))
        w, h = int(rng.integers(4, 14)), int(rng.integers(4, 14))
        cv2.rectangle(img, (x, y), (x + w, y + h), int(rng.integers(30, 230)), -1)
    return cv2.cvtColor(cv2.GaussianBlur(img, (3, 3), 0), cv2.COLOR_GRAY2BGR)


def _scene(*rects, seed=50):
    """Plain background with rock inside each (x1, y1, x2, y2) rectangle."""
    clutter = _rock(seed)
    out = np.full((_H, _W, 3), _SKY, np.uint8)
    for x1, y1, x2, y2 in rects:
        out[y1:y2, x1:x2] = clutter[y1:y2, x1:x2]
    return out


def _growing(scene, about, steps=4, rate=0.02):
    """`steps` frames in which the scene grows about a point, `rate` a frame."""
    return [_zoomed(scene, 1.0 + rate * k, about) for k in range(steps)]


def _shapes(**cfg):
    loom = _warm()
    settings = {"enabled": True}
    settings.update(cfg)
    return TerrainShapes(loom, settings)


_SPAN = 3 * _DT


def test_a_growing_object_in_the_middle_is_a_threat_with_its_time_to_contact():
    scene = _scene((380, 130, 620, 300))
    reading = _shapes().measure(_growing(scene, (500, 215)), _SPAN)
    assert reading.readable and len(reading.shapes) == 1
    threat = reading.threat
    assert threat is not None and threat.centred
    # 2 percent a frame for three frames is 6 percent over the span.
    assert threat.tau == pytest.approx(_SPAN / 0.06, rel=0.25)
    assert threat.area_last > threat.area_first
    xs, ys = threat.outline[:, 0], threat.outline[:, 1]
    assert 360 < xs.min() < 420 and 590 < xs.max() < 650      # the outline hugs the object
    assert 110 < ys.min() < 170 and 270 < ys.max() < 330


def test_an_object_that_is_not_growing_is_not_a_threat():
    scene = _scene((380, 130, 620, 300))
    frames = [_shifted(scene, 2 * k, 0) for k in range(4)]      # slides, same size
    reading = _shapes().measure(frames, _SPAN)
    assert reading.readable and len(reading.shapes) == 1
    assert reading.threat is None and fmt_shape(reading) == "clear"


def test_a_growing_object_off_to_the_side_is_not_a_threat():
    """The operator's rule: it has to be in the middle of the screen."""
    scene = _scene((210, 130, 330, 260))
    reading = _shapes().measure(_growing(scene, (270, 195)), _SPAN)
    assert reading.readable and reading.shapes
    assert any(sh.tau is not None for sh in reading.shapes)      # it is growing
    assert not any(sh.centred for sh in reading.shapes)          # but not in the middle
    assert reading.threat is None


def test_two_objects_are_two_shapes_with_their_own_growth():
    """One zoom fitted to every dot would average these; each has its own."""
    scene = _scene((210, 120, 330, 240), (420, 140, 600, 300))
    # Only the middle one grows: zoom the scene about its centre, then put the
    # left one back where it was.
    frames = []
    for k in range(4):
        f = _zoomed(scene, 1.0 + 0.02 * k, (510, 220))
        f[100:260, 190:350] = scene[100:260, 190:350]
        frames.append(f)
    reading = _shapes().measure(frames, _SPAN)
    assert len(reading.shapes) == 2
    still, growing = sorted(reading.shapes, key=lambda sh: sh.outline[:, 0].mean())
    assert still.tau is None
    assert growing.tau is not None and reading.threat is growing


@pytest.mark.parametrize("rect, about, way", [
    ((440, 110, 700, 300), (570, 205), "left"),     # left edge is the near one
    ((260, 110, 520, 300), (390, 205), "right"),    # right edge is the near one
])
def test_the_way_out_is_past_the_nearest_side_edge(rect, about, way):
    reading = _shapes().measure(_growing(_scene(rect), about), _SPAN)
    assert reading.threat is not None
    assert reading.avoid == way


def test_up_only_when_the_top_edge_is_clearly_the_nearest():
    """Pulling up is what the aircraft already does; it is not the default."""
    low_wide = _scene((250, 200, 710, 330))         # wide, top edge just above centre
    reading = _shapes().measure(_growing(low_wide, (480, 265)), _SPAN)
    assert reading.threat is not None and reading.avoid == "up"
    tall = _scene((400, 90, 640, 330))              # top edge far above centre
    reading = _shapes().measure(_growing(tall, (520, 210)), _SPAN)
    assert reading.threat is not None and reading.avoid in ("left", "right")


def test_an_edge_at_the_border_of_the_view_is_not_a_way_out():
    """The view is x 192 to 768. A shape that runs off its left border only
    left the picture there; the object goes on."""
    scene = _scene((150, 110, 560, 300))
    reading = _shapes().measure(_growing(scene, (380, 205)), _SPAN)
    assert reading.threat is not None
    assert reading.threat.clipped[0] is True
    assert reading.avoid == "right"


def test_a_shape_that_fills_the_view_leaves_only_up():
    scene = _scene((100, 60, 860, 420))
    reading = _shapes().measure(_growing(scene, (480, 240)), _SPAN)
    assert reading.threat is not None and reading.avoid == "up"


def test_plain_sky_has_no_shapes_and_is_never_a_threat():
    sky = np.full((_H, _W, 3), _SKY, np.uint8)
    frames = [sky.copy() + k for k in range(4)]
    reading = _shapes().measure(frames, _SPAN)
    assert reading.readable and reading.shapes == () and reading.threat is None
    assert fmt_shape(reading) == "clear"


def test_no_shapes_until_the_hud_mask_has_warmed_up():
    shapes = TerrainShapes(TerrainLoom({"enabled": True}), {"enabled": True})
    scene = _scene((380, 130, 620, 300))
    assert shapes.measure(_growing(scene, (500, 215)), _SPAN).status == "warming"


def test_the_threat_must_stay_for_consecutive_ticks(caplog):
    shapes = _shapes()
    scene = _scene((380, 130, 620, 300))
    reading = shapes.measure(_growing(scene, (500, 215)), _SPAN)
    with caplog.at_level(logging.WARNING, logger="wingman.terrain_shapes"):
        assert shapes.update(reading) is False          # first sight
        assert shapes.update(reading) is True           # still there, same place
        assert shapes.update(reading) is True
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 1
    assert "way out" in lines[0] and "SHADOW - not actuating" in lines[0]


def test_a_gap_or_a_different_object_starts_the_count_again():
    shapes = _shapes()
    middle = shapes.measure(_growing(_scene((380, 130, 620, 300)), (500, 215)), _SPAN)
    shapes.update(middle)
    assert shapes.update(None) is False                 # a tick with no reading
    assert shapes.update(middle) is False
    assert shapes.update(ShapeReading(status="few-points")) is False
    assert shapes.update(middle) is False


def test_tick_logs_the_shape_verdict_on_the_bt_line(analyzer, caplog):
    scene = _scene((380, 130, 620, 300))
    handler, grab = _handler(analyzer, False, _growing(scene, (500, 215)))
    handler._loom.pairs_per_tick = 3
    handler._shapes = TerrainShapes(handler._loom, {"enabled": True})
    line = _bt_line(handler, caplog, GameState.GAME_BATTLE)
    assert grab.call_count == 4
    verdict = _field(line, "shape")
    tau, way = verdict.split(":")
    assert tau.endswith("s") and 1.0 < float(tau[:-1]) < 12.0
    assert way in ("left", "right", "up")


def test_tick_without_shapes_logs_no_reading(analyzer, caplog):
    handler, _ = _handler(analyzer, None, [])
    assert _field(_bt_line(handler, caplog, GameState.GAME_BATTLE), "shape") == "n/a"


def test_hud_draws_the_threat_outline_and_the_way_out(tmp_path):
    shapes = _shapes()
    scene = _scene((440, 110, 700, 300))
    frames = _growing(scene, (570, 205))
    reading = shapes.measure(frames, _SPAN)
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer.set_shape_reading(reading, True, 100.0)
    canvas = frames[-1].copy()
    renderer._draw_shapes(canvas, 100.0)
    red = np.all(canvas == hud_module._RED, axis=2)
    ys, xs = np.nonzero(red)
    assert red.sum() > 400
    assert xs.min() < _W // 2 - 60                    # the arrow reaches left of centre
    assert xs.max() > 640                             # and the outline's right side is drawn
    stale = frames[-1].copy()
    renderer._draw_shapes(stale, 100.0 + hud_module._TERRAIN_STALE_S + 1.0)
    assert not np.all(stale == hud_module._RED, axis=2).any()


# ---------------------------------------------------------------------------
# Outlines from the picture itself, not a loose line round the dots.
# ---------------------------------------------------------------------------

from wingman.terrain_shapes import fmt_middle  # noqa: E402


def _ellipse_scene(centre, axes, seed=60):
    rock = _rock(seed)
    out = np.full((_H, _W, 3), _SKY, np.uint8)
    inside = np.zeros((_H, _W), np.uint8)
    cv2.ellipse(inside, centre, axes, 0, 0, 360, 255, -1)
    out[inside > 0] = rock[inside > 0]
    return out


def test_the_outline_follows_the_objects_own_edge():
    """An ellipse is outlined as an ellipse. A line round the dots, or a
    bounding box, would enclose a quarter more area."""
    centre, axes = (480, 220), (130, 80)
    reading = _shapes().measure(_growing(_ellipse_scene(centre, axes), centre), _SPAN)
    assert len(reading.shapes) == 1
    shape = reading.shapes[0]
    ellipse_area = np.pi * axes[0] * axes[1] * 1.06 ** 2          # after three frames of growth
    assert shape.area_last == pytest.approx(ellipse_area, rel=0.2)
    box_area = (2 * axes[0]) * (2 * axes[1]) * 1.06 ** 2
    assert shape.area_last < 0.9 * box_area
    assert len(shape.outline) > 8                                  # a curve, not four corners


def test_a_smooth_patch_inside_an_object_is_not_a_way_out():
    """A shadowed face or a snowfield inside a mesa is not sky. The way out is
    still past the object's real edge."""
    scene = _scene((300, 110, 560, 310))          # right edge is the near one
    scene[190:240, 430:470] = _SKY                # a smooth patch just left of the middle
    reading = _shapes().measure(_growing(scene, (430, 210)), _SPAN)
    assert reading.threat is not None
    assert reading.avoid == "right"


def test_an_object_in_the_middle_with_no_dots_to_follow_is_blind_not_clear():
    """Near the ground the dots stop tracking. That is not the same as nothing
    being there, and the verdict must not read as clear."""
    frames = _growing(_scene((380, 130, 620, 300)), (500, 215))
    # The frame before the last is smeared, as motion blur does at speed: no
    # corner in it is sharp enough to follow into the last one.
    frames[-2] = cv2.GaussianBlur(frames[-2], (0, 0), 12)
    reading = _shapes().measure(frames, _SPAN)
    assert reading.middle.dots < 8
    assert reading.readable and reading.middle is not None
    assert reading.threat is None and reading.blind is True
    assert fmt_shape(reading) == "blind"
    assert _shapes().update(reading) is False


def test_a_view_that_goes_unreadable_straight_after_a_threat_is_blind_not_clear():
    """2026-10-03: before 4 of 6 terrain deaths a confirmed threat was followed
    by ticks that found no outline and followed under a dozen dots, and the
    verdict read `clear`. A threat does not vanish between ticks."""
    shapes = _shapes()
    threat = shapes.measure(_growing(_scene((380, 130, 620, 300)), (500, 215)), _SPAN)
    assert threat.threat is not None
    shapes.update(threat)
    smear = [cv2.GaussianBlur(f, (0, 0), 25) for f in _growing(_scene((380, 130, 620, 300)), (500, 215))]
    reading = shapes.measure(smear, _SPAN)
    assert reading.middle is None and reading.tracked < 25
    assert reading.lost is True and fmt_shape(reading) == "blind"
    shapes.update(reading)
    assert fmt_shape(shapes.measure(smear, _SPAN)) == "blind"      # and it stays so while unreadable


def test_an_unreadable_view_with_no_threat_before_it_is_clear():
    """Plain sky also has nothing to follow. Without a threat just before, it is clear."""
    smear = [cv2.GaussianBlur(f, (0, 0), 25) for f in _growing(_scene((380, 130, 620, 300)), (500, 215))]
    reading = _shapes().measure(smear, _SPAN)
    assert reading.middle is None and reading.lost is False
    assert fmt_shape(reading) == "clear"


def test_a_readable_open_view_ends_the_wait_after_a_threat():
    shapes = _shapes()
    shapes.update(shapes.measure(_growing(_scene((380, 130, 620, 300)), (500, 215)), _SPAN))
    off_to_the_side = _growing(_scene((40, 130, 200, 300)), (120, 215))
    reading = shapes.measure(off_to_the_side, _SPAN)
    assert reading.middle is None and reading.tracked >= 25 and reading.lost is False
    shapes.update(reading)
    smear = [cv2.GaussianBlur(f, (0, 0), 25) for f in off_to_the_side]
    assert fmt_shape(shapes.measure(smear, _SPAN)) == "clear"


def test_hud_strokes_on_plain_sky_make_no_shapes():
    sky = np.full((_H, _W, 3), _SKY, np.uint8)
    frames = [_with_moving_hud(sky, 5 * k) for k in range(4)]
    reading = _shapes().measure(frames, _SPAN)
    assert reading.readable and reading.shapes == ()


def test_the_log_carries_both_growth_figures_for_what_covers_the_middle():
    """So `clear` can be told from "measured slow" and from "not measured"."""
    scene = _scene((380, 130, 620, 300))
    still = [_shifted(scene, 2 * k, 0) for k in range(4)]
    reading = _shapes().measure(still, _SPAN)
    assert fmt_shape(reading) == "clear"
    text = fmt_middle(reading)
    assert text.startswith("outline ") and " dots - " in text and " n=" in text
    growing = _shapes().measure(_growing(scene, (500, 215)), _SPAN)
    assert " dots 6." in fmt_middle(growing) or " dots 5." in fmt_middle(growing)
    assert fmt_middle(None) == "none"

"""Design 020 Phase 4: the shadow terrain service — fix on the map, time to impact, and its TERRAIN: line.

Synthetic data only: a smooth random texture standing for the game's minimap, a
height map with a wall, and minimap crops cut from the texture at a known place
and heading. The real map data is built offline and is not in the repository.
"""
import logging
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import GameStateAnalyzer
from wingman.state import GameState
from wingman.terrain_service import TerrainService
from wingman.tick_handlers import BehaviorTreeHandler

SPAN = 20000.0          # the texture covers ±10 km
MPP = 34.0              # metres per crop pixel at 320 px


@pytest.fixture(scope="module")
def texture():
    rng = np.random.default_rng(7)
    small = cv2.GaussianBlur(rng.random((400, 400)).astype(np.float32), (0, 0), 2.0)
    small = (small - small.min()) / (small.max() - small.min()) * 255.0
    return cv2.resize(small, (2048, 2048), interpolation=cv2.INTER_CUBIC).clip(0, 255).astype(np.uint8)


def _write_map(tmp_path, texture, top=None):
    mm = tmp_path / "minimap.png"
    cv2.imwrite(str(mm), texture)
    if top is None:
        top = np.full((1600, 1600), -232.3, np.float32)
    hm = tmp_path / "height.npz"
    np.savez(hm, top=top, cell=16.0, origin=-12800.0)
    return {"minimap": str(mm), "height": str(hm)}


def _crop(texture, x, z, north_bearing, size=320):
    """The minimap as the game draws it at (x, z): north-up texture, turned so N sits at `north_bearing`."""
    s = texture.shape[0] / SPAN                              # texture pixels per metre
    centre = ((x + SPAN / 2) * s, (SPAN / 2 - z) * s)
    side = int(round(size * MPP * s))
    patch = cv2.getRectSubPix(texture, (side, side), centre)
    north_up = cv2.resize(patch, (size, size), interpolation=cv2.INTER_LINEAR)
    turn = cv2.getRotationMatrix2D(((size - 1) / 2, (size - 1) / 2), -north_bearing, 1.0)
    return cv2.cvtColor(cv2.warpAffine(north_up, turn, (size, size)), cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize("x, z, bearing", [(0, 0, 0), (1200, -2500, 37), (-3000, 1800, -120)])
def test_a_fix_lands_where_the_crop_was_cut(tmp_path, texture, x, z, bearing):
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture)}})
    fix = svc.fix("M", _crop(texture, x, z, bearing), bearing, now=0.0)
    assert fix is not None
    assert abs(fix.x - x) < 2 * MPP and abs(fix.z - z) < 2 * MPP


def test_a_fix_reaches_the_arena_edge(tmp_path, texture):
    """The first search could not place a fix more than 5.7 km out: the template had to fit inside
    the texture, so spawns near the edge were pinned to it (Design 020, Phase 1 correction)."""
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture)}})
    fix = svc.fix("M", _crop(texture, -4300, -8000, 20), 20, now=0.0)
    assert fix is not None
    assert abs(fix.x + 4300) < 2 * MPP and abs(fix.z + 8000) < 2 * MPP


def test_the_search_near_the_last_fix_agrees_with_the_whole_map(tmp_path, texture):
    """Near the last fix the search covers a window (a fifth of the cost); it must find the same place."""
    maps = {"M": _write_map(tmp_path, texture)}
    near, whole = TerrainService({"maps": maps}), TerrainService({"maps": maps, "max_fix_gap_s": 0.2})
    for svc in (near, whole):
        svc.observe("M", _crop(texture, 2000, 3000, 45), 45, 1500.0, 0.0, now=0.0)
    a = near.observe("M", _crop(texture, 2200, 3150, 50), 50, 1500.0, 0.0, now=1.5).fix
    b = whole.observe("M", _crop(texture, 2200, 3150, 50), 50, 1500.0, 0.0, now=1.5).fix
    assert a is not None and b is not None
    assert abs(a.x - b.x) < 1.0 and abs(a.z - b.z) < 1.0


def test_a_featureless_crop_gives_no_fix(tmp_path, texture):
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture)}})
    assert svc.fix("M", np.full((317, 320, 3), 90, np.uint8), 0.0, now=0.0) is None


def test_time_to_impact_meets_a_wall_and_the_margin_reading_is_logged(tmp_path, texture):
    top = np.full((1600, 1600), -232.3, np.float32)
    top[:, 800 + 500 // 16:] = 1000.0                       # a 1,000 m wall from x = 500 m eastward
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}, "horizon_s": 3.0,
                          "clearance_margin_m": 200.0})
    data = svc._data("M")
    assert svc.time_to_impact(data, 0.0, 0.0, 250.0, 0.0, 500.0, 0.0) == pytest.approx(2.0, abs=0.15)
    assert svc.time_to_impact(data, 0.0, 0.0, 250.0, 0.0, 1500.0, 0.0) is None      # clears the wall
    assert svc.time_to_impact(data, 0.0, 0.0, 250.0, 0.0, 1150.0, -100.0) == pytest.approx(2.0, abs=0.15)

    # Through observe(): two fixes 1 s apart, flying east at 250 m/s toward the wall at HUD 1,150 m.
    # It clears the wall by 150 m, inside the 200 m margin: ttc_y is clear and ttc_m is not.
    first = svc.observe("M", _crop(texture, -250, 0, 90), 90, 1150.0, 0.0, now=10.0)
    second = svc.observe("M", _crop(texture, 0, 0, 90), 90, 1150.0, 0.0, now=11.0)
    assert first.fix is not None and first.ttc_y is None
    assert second.speed == pytest.approx(250, abs=60)
    assert second.ttc_y is None
    assert second.ttc_m == pytest.approx(2.0, abs=0.4)            # the wall is 500 m ahead
    assert "ttc_y=clear" in second.log_text("M") and "ttc_m=2" in second.log_text("M")


@pytest.mark.parametrize("margin, warned", [(0.0, False), (100.0, False), (200.0, True)])
def test_the_margin_reading_warns_only_when_the_path_clears_by_less(tmp_path, texture, margin, warned):
    """HUD 1,150 m over a 1,000 m wall: clear by 150 m. A margin above that warns, one below does not."""
    top = np.full((1600, 1600), -232.3, np.float32)
    top[:, 800 + 500 // 16:] = 1000.0
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}, "horizon_s": 3.0,
                          "clearance_margin_m": margin})
    svc.observe("M", _crop(texture, -250, 0, 90), 90, 1150.0, 0.0, now=10.0)
    r = svc.observe("M", _crop(texture, 0, 0, 90), 90, 1150.0, 0.0, now=11.0)
    assert (r.ttc_m is not None) == warned


def test_a_negative_hud_altitude_is_a_height_below_the_ground_above_it(tmp_path, texture):
    """The HUD is the mesh height, signed: at -200 m over Crimson Canyon's water (-232) the 1,000 m
    wall ahead is a certain impact, where the sign-dropped +200 it used to read also was."""
    top = np.full((1600, 1600), -232.3, np.float32)
    top[:, 800 + 500 // 16:] = 1000.0
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}, "horizon_s": 3.0})
    data = svc._data("M")
    assert svc.time_to_impact(data, 0.0, 0.0, 250.0, 0.0, -200.0, 0.0) == pytest.approx(2.0, abs=0.15)
    assert svc.time_to_impact(data, 0.0, 0.0, 0.0, 250.0, -200.0, 0.0) is None     # along the water


def test_no_reading_without_data_for_the_map(tmp_path, caplog):
    svc = TerrainService({"maps": {"M": {"minimap": str(tmp_path / "none.png"),
                                         "height": str(tmp_path / "none.npz")}}})
    assert svc.observe("OTHER", None, 0.0, 1000.0, 0.0, now=0.0) is None          # no entry
    with caplog.at_level(logging.WARNING, logger="wingman.terrain_service"):
        assert svc.observe("M", None, 0.0, 1000.0, 0.0, now=0.0) is None           # files missing
        assert svc.observe("M", None, 0.0, 1000.0, 0.0, now=1.0) is None
    assert sum("no data for M" in r.getMessage() for r in caplog.records) == 1     # said once


# --- the TERRAIN: line from the BT tick --------------------------------------

_BATTLE_FRAME = "test_screenshots/integration_test/P1_030_BATTLE_HUD_MISSILES_4.png"


@pytest.fixture
def analyzer():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        a = GameStateAnalyzer(yaml.safe_load(f))
    a.state = GameState.GAME_BATTLE.name
    a._game_battle_alive = True
    try:
        yield a
    finally:
        a.cleanup()


def _terrain_lines(analyzer, caplog, tmp_path, texture, state, match_map):
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = False
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    handler.set_terrain_service(TerrainService({"maps": {"TEST MAP": _write_map(tmp_path, texture)}}))
    analyzer.match_map = match_map
    frame = cv2.imread(_BATTLE_FRAME)
    assert frame is not None
    with caplog.at_level(logging.DEBUG, logger="wingman.tick_handlers"):
        handler.tick(frame, state, {"health": 100})
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("TERRAIN:")]


def test_a_battle_tick_on_a_mapped_map_logs_one_terrain_line(analyzer, caplog, tmp_path, texture):
    lines = _terrain_lines(analyzer, caplog, tmp_path, texture, GameState.GAME_BATTLE, "TEST MAP")
    assert len(lines) == 1 and "map=TEST MAP fix=" in lines[0]


def test_no_terrain_line_off_the_map_or_outside_battle(analyzer, caplog, tmp_path, texture):
    assert _terrain_lines(analyzer, caplog, tmp_path, texture, GameState.GAME_BATTLE, "STONE FOREST") == []
    caplog.clear()
    assert _terrain_lines(analyzer, caplog, tmp_path, texture, GameState.GAME_LOBBY, "TEST MAP") == []


# --- ADR 174: the map warning flies in half the rounds ---------------------------

def test_the_arm_is_drawn_per_round_at_the_share(tmp_path, texture):
    import random as _random
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture)}, "trial_share": 0.5})
    svc._rng = _random.Random(4)
    arms = [svc.new_round() for _ in range(400)]
    assert set(arms) == {"map", "control"} and svc.rounds == 400
    assert 0.4 < arms.count("map") / 400 < 0.6
    off = TerrainService({"maps": {}, "trial_share": 0.0})
    assert off.new_round() is None and not off.flies()


def test_a_warning_is_ttc_y_within_warn_s_and_flies_only_in_the_map_arm(tmp_path, texture):
    from wingman.terrain_service import Fix, TerrainReading
    svc = TerrainService({"maps": {}, "warn_s": 2.0})
    f = Fix(0.0, 0.0, 0.0, 0.6, 0.1)
    assert svc.warns(TerrainReading(f, ttc_y=1.5)) and svc.warns(TerrainReading(f, ttc_y=2.0))
    assert not svc.warns(TerrainReading(f, ttc_y=2.5))
    assert not svc.warns(TerrainReading(f, ttc_y=None, ttc_m=0.5))      # the margin reading is not it
    svc.arm = "control"
    assert not svc.flies()
    svc.arm = "map"
    assert svc.flies()


def test_a_map_warning_on_the_snapshot_forces_the_climb(caplog):
    from tests.test_behavior_tree import FakeClock, make_snap
    from wingman.behavior_tree import make_climb_condition
    cond = make_climb_condition(500, 1000, confirm_reads=1, clock=FakeClock())
    with caplog.at_level(logging.WARNING, logger="wingman.behavior_tree"):
        assert cond(make_snap(altitude=5000.0, terrain_map_warning=True)) is True
    assert cond.emergency_active
    assert sum("MAP WARNING" in r.getMessage() for r in caplog.records) == 1
    assert cond(make_snap(altitude=5000.0, terrain_map_warning=False)) is False


@pytest.mark.parametrize("arm, flies", [("map", True), ("control", False)])
def test_the_tick_draws_the_arm_at_battle_entry_and_flies_only_the_map_arm(
        analyzer, tmp_path, texture, arm, flies):
    """The arm is drawn on entering battle and tagged on the round; the warning reaches the
    snapshot only in the `map` arm."""
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    ctrl = MagicMock()
    ctrl.padlock_state.return_value = False
    notes = []
    handler = BehaviorTreeHandler(analyzer, ctrl, bt_cfg)
    svc = TerrainService({"maps": {"TEST MAP": _write_map(tmp_path, texture)}, "trial_share": 0.5})
    svc.new_round = lambda: (setattr(svc, "arm", arm), setattr(svc, "rounds", svc.rounds + 1), arm)[2]
    svc.observe = lambda *a, **k: object()              # any reading
    svc.action = lambda reading: "climb" if svc.flies() else None
    handler.set_terrain_service(svc, round_note=lambda name, ts: notes.append(name))
    analyzer.match_map = "TEST MAP"
    handler._terrain_round_edge(GameState.GAME_LOBBY)
    handler._terrain_round_edge(GameState.GAME_BATTLE)
    handler._terrain_round_edge(GameState.GAME_BATTLE)  # still the same round
    assert notes == [f"map_arm_{arm}"] and svc.rounds == 1
    frame = cv2.imread(_BATTLE_FRAME)
    svc.observe = lambda *a, **k: _Reading()
    assert (handler._observe_terrain(frame, GameState.GAME_BATTLE, 1000.0, 0.0) == "climb") is flies


class _Reading:
    inside_ground = False
    ttc_y = None

    def log_text(self, map_name):
        return f"map={map_name} fix=0,0"


# --- Design 020 way_out: which manoeuvre clears ---------------------------------

def _grid_map(tmp_path, texture, top):
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}})
    return svc, svc._data("M")


def _cell(v):
    return int((v + 12800) / 16)


def test_a_canyon_bend_is_cleared_by_turning_not_by_climbing(tmp_path, texture):
    """North at 250 m/s, 300 m up, along a canyon floor; a 3,000 m wall 750 m ahead and to the
    right, open ground to the left. Climbing cannot top the wall in time; turning left can."""
    top = np.full((1600, 1600), -200.0, np.float32)
    top[_cell(750):, _cell(-600):] = 3000.0           # the wall ahead, from x = -600 m eastward
    svc, data = _grid_map(tmp_path, texture, top)
    outs, best = svc.way_out(data, 0.0, 0.0, 0.0, 250.0, 300.0, 0.0)
    assert outs["hold"] is not None and outs["climb"] is not None
    assert outs["left"] is None and best == "left"


def test_a_low_ridge_ahead_is_cleared_by_climbing(tmp_path, texture):
    """At 400 m/s a 25 deg/s turn reaches about 1.1 km forward, so a 400 m ridge 900 m ahead, across
    the whole map, cannot be turned away from at 300 m; climbing tops it, and the plain climb wins
    the tie with the climbing turns because it turns least."""
    top = np.full((1600, 1600), -200.0, np.float32)
    top[_cell(900):_cell(1100), :] = 400.0
    svc, data = _grid_map(tmp_path, texture, top)
    outs, best = svc.way_out(data, 0.0, 0.0, 0.0, 400.0, 300.0, 0.0)
    assert outs["hold"] is not None and outs["left"] is not None and outs["right"] is not None
    assert outs["climb"] is None and best == "climb"


def test_open_ground_leaves_every_candidate_clear(tmp_path, texture):
    top = np.full((1600, 1600), -200.0, np.float32)
    svc, data = _grid_map(tmp_path, texture, top)
    outs, best = svc.way_out(data, 0.0, 0.0, 0.0, 250.0, 300.0, 0.0)
    assert all(v is None for v in outs.values()) and best == "hold"


def test_a_warning_tick_logs_the_way_out(tmp_path, texture):
    top = np.full((1600, 1600), -232.3, np.float32)
    top[:, 800 + 500 // 16:] = 1000.0                 # the 1,000 m wall east, as above
    svc = TerrainService({"maps": {"M": _write_map(tmp_path, texture, top)}})
    svc.observe("M", _crop(texture, -250, 0, 90), 90, 500.0, 0.0, now=10.0)
    r = svc.observe("M", _crop(texture, 0, 0, 90), 90, 500.0, 0.0, now=11.0)
    assert r.ttc_y is not None and r.way_out is not None
    assert set(r.way_out) == {"hold", "climb", "left", "right", "left+climb", "right+climb"}
    assert f"way_out={r.way_out_best} [" in r.log_text("M")



# --- ADR 174 revised: the way out flies --------------------------------------------

def _reading_for(svc, data, vx, vz, alt, rate=0.0, x=0.0, z=0.0):
    from wingman.terrain_service import Fix, TerrainReading, fly_candidate
    hold = fly_candidate(data, x, z, vx, vz, alt, rate, 0.0, 0.0, 5.0)
    outs = best = None
    if hold is not None and hold <= 5.0:
        outs, best = svc.way_out(data, x, z, vx, vz, alt, rate)
    return TerrainReading(Fix(0.0, x, z, 0.6, 0.1), ground=float(data.ground(x, z)),
                          ttc_y=svc.time_to_impact(data, x, z, vx, vz, alt, rate),
                          way_out=outs, way_out_best=best)


@pytest.mark.parametrize("arm, expect", [("map", "left"), ("control", None)])
def test_the_action_at_a_canyon_bend_is_a_left_turn_in_the_map_arm(tmp_path, texture, arm, expect):
    top = np.full((1600, 1600), -200.0, np.float32)
    top[_cell(750):, _cell(-600):] = 3000.0
    svc, data = _grid_map(tmp_path, texture, top)
    svc.arm = arm
    assert svc.action(_reading_for(svc, data, 0.0, 250.0, 300.0)) == expect


def test_the_action_before_a_low_ridge_is_a_climb(tmp_path, texture):
    top = np.full((1600, 1600), -200.0, np.float32)
    top[_cell(900):_cell(1100), :] = 400.0
    svc, data = _grid_map(tmp_path, texture, top)
    svc.arm = "map"
    assert svc.action(_reading_for(svc, data, 0.0, 400.0, 300.0)) == "climb"


def test_no_action_on_open_ground_or_inside_the_ground(tmp_path, texture):
    from wingman.terrain_service import Fix, TerrainReading
    top = np.full((1600, 1600), -200.0, np.float32)
    svc, data = _grid_map(tmp_path, texture, top)
    svc.arm = "map"
    assert svc.action(_reading_for(svc, data, 0.0, 250.0, 300.0)) is None
    inside = TerrainReading(Fix(0.0, 0.0, 0.0, 0.6, 0.1), ground=1000.0, ttc_y=0.1,
                            way_out={"hold": 0.1, "climb": 0.1}, way_out_best="hold",
                            inside_ground=True)
    assert svc.action(inside) is None and not svc.warns(inside)


def test_the_tree_selects_terrain_turn_above_boundary_turn_and_climb():
    """With its actuator wired, a terrain turn outranks the boundary turn and the climb;
    without one (replay lanes) the leaf is not built."""
    import py_trees
    from tests.test_behavior_tree import make_snap
    from wingman.behavior_tree import (SNAPSHOT_KEY, TACTIC_TERRAIN_TURN, build_tree,
                                       selected_tactic)
    with open(CONFIG_PATH, encoding="utf-8") as f:
        bt_cfg = yaml.safe_load(f)["behavior_tree"]
    started = []
    tree = build_tree(bt_cfg, actuators={TACTIC_TERRAIN_TURN: (lambda: started.append(1), lambda: False)})
    writer = py_trees.blackboard.Client(name="TestWriter")
    writer.register_key(key=SNAPSHOT_KEY, access=py_trees.common.Access.WRITE)
    writer.set(SNAPSHOT_KEY, make_snap(altitude=300.0, terrain_turn="left", terrain_map_warning=True))
    tree.tick()
    assert selected_tactic(tree) == TACTIC_TERRAIN_TURN and started == [1]
    bare = build_tree(bt_cfg)
    assert TACTIC_TERRAIN_TURN not in [c.name for c in bare.root.children]


def test_blurring_fixes_a_crop_speckled_like_stone_forest_trees(tmp_path, texture):
    """Stone Forest's minimap draws trees as fine speckle its texture lacks: unblurred, 0 of 55
    recorded crops fixed; blurred by 100 m, 50 of 55 (2026-11-08)."""
    rng = np.random.default_rng(3)
    crop = _crop(texture, 1500, -2000, 30).astype(np.float32)
    speckle = (rng.random(crop.shape[:2]) < 0.6) * rng.uniform(-255, 255, crop.shape[:2])
    crop = np.clip(crop + speckle[..., None], 0, 255).astype(np.uint8)
    maps = {"M": _write_map(tmp_path, texture)}
    assert TerrainService({"maps": maps, "match_blur_m": 0}).fix("M", crop, 30, now=0.0) is None
    blurred = TerrainService({"maps": maps}).fix("M", crop, 30, now=0.0)
    assert blurred is not None
    assert abs(blurred.x - 1500) < 3 * MPP and abs(blurred.z + 2000) < 3 * MPP


def test_a_turn_levels_a_dive_as_the_bank_and_pull_does(tmp_path, texture):
    """2026-11-08 07:42, after the resupply: diving into a canyon with a wall ahead and room to the
    side. A turn held at the dive angle meets the floor; the flown bank-and-pull also levels the
    dive (06:31:08: nose -32 to +8 deg in about 3 s), so the turn clears."""
    from wingman.terrain_service import fly_candidate
    top = np.full((1600, 1600), 0.0, np.float32)            # canyon floor at 0 m
    top[_cell(750):, _cell(-600):] = 3000.0                  # the wall ahead and to the right
    svc, data = _grid_map(tmp_path, texture, top)
    held = fly_candidate(data, 0.0, 0.0, 0.0, 250.0, 500.0, -120.0, -25.0, 0.0, 5.0)
    levelled = fly_candidate(data, 0.0, 0.0, 0.0, 250.0, 500.0, -120.0, -25.0, 0.0, 5.0, level_deg_s=13.0)
    assert held is not None and levelled is None
    outs, best = svc.way_out(data, 0.0, 0.0, 0.0, 250.0, 500.0, -120.0)
    assert outs["climb"] is not None and outs["left"] is None and best == "left"

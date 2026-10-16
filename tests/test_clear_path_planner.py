"""Code Review 019 step 3d: the clear-path planner, in shadow.

Checked on views built from known outlines: where the free air is, what is
closing and where the aircraft is going are given, so each answer can be
worked out by hand. The geometry is that of a 1920 by 1200 frame: the
analysed view is x 384 to 1536, y 144 to 720, at half scale.
"""

import logging
import time
from dataclasses import replace

import numpy as np
import pytest

from wingman import hud as hud_module
from wingman.clear_path import (
    PathGoal, PathPerception, PathPlan, PathPlanner, PathView, _side, fmt_plan,
)
from wingman.hud import HudRenderer
from wingman.terrain_loom import LoomReading
from wingman.terrain_shapes import Shape, ShapeReading

_ORIGIN = (384, 144)
_SCALE = 0.5
_CENTRE = (960.0, 600.0)


def _block(x1, y1, x2, y2, tau, measured=True, centred=True):
    outline = np.float32([[x1, y1], [x2, y1], [x2, y2], [x1, y2]])
    return Shape(outline=outline, dots=40, area_first=1.0, area_last=1.0, tau=tau,
                 centred=centred, measured=measured)


def _view(*shapes, heading=None, status="ok"):
    reading = ShapeReading(status=status, shapes=tuple(shapes),
                           texture=np.zeros((288, 576), np.uint8),
                           view_origin=_ORIGIN, view_scale=_SCALE)
    pair = None
    if heading is not None:
        pair = LoomReading(status="ok", dt=0.17, scale=1.02, tau=5.0, fixed=heading)
    return PathView(status=status, picture_ts=10.0, dt=0.17, shape=reading, pair=pair,
                    tau_warn_s=8.0, frame_size=(1920, 1200))


def _planner(**cfg):
    settings = {"clear_px": 60, "hold_s": 1.0, "escape_tau_s": 3.0, "nearer_frac": 0.7,
                "wall_warn_s": 3.0}
    settings.update(cfg)
    return PathPlanner(settings)


def _crosses(plan, x1, y1, x2, y2, samples=200):
    """Whether the straight line from the plan's origin to its aim enters a box."""
    t = np.linspace(0.0, 1.0, samples)
    xs = plan.origin[0] + (plan.aim[0] - plan.origin[0]) * t
    ys = plan.origin[1] + (plan.aim[1] - plan.origin[1]) * t
    return bool(((xs > x1) & (xs < x2) & (ys > y1) & (ys < y2)).any())


def test_open_air_is_direct_and_the_aim_is_the_goal():
    plan = _planner().plan(_view(), PathGoal("resupply", 1100, 400), 10.0)
    assert plan.mode == "DIRECT"
    assert plan.aim == (1100, 400)
    assert plan.origin == pytest.approx(_CENTRE)


def test_terrain_on_the_line_to_the_goal_is_a_detour_around_it():
    """A ridge closing in 5 s lies across the line from the flight path to the
    goal: the aim moves to free air the line can reach."""
    ridge = (800, 400, 1300, 500)
    plan = _planner().plan(_view(_block(*ridge, tau=5.0)), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "DETOUR"
    assert plan.blocker_tau == pytest.approx(5.0)
    assert not _crosses(plan, *ridge)
    assert plan.reason == "terrain on the line to the goal"


def test_a_goal_against_terrain_is_reached_over_the_top():
    """The resupply marker just above a spire the aircraft is flying at (the
    rock-map reference frame): the spire is being left, so it does not block,
    but the marker has no free air round it, so the aim goes over the top."""
    spire = (900, 330, 1020, 720)
    plan = _planner().plan(_view(_block(*spire, tau=5.0)), PathGoal("resupply", 960, 300), 10.0)
    assert plan.mode == "DETOUR" and plan.side == "over"
    assert plan.aim[1] < 300
    assert plan.reason == "the goal is against nearer terrain"


def test_flying_at_terrain_about_to_be_hit_escapes_whatever_the_goal():
    spire = (900, 330, 1020, 720)
    plan = _planner().plan(_view(_block(*spire, tau=2.0)), PathGoal("resupply", 960, 300), 10.0)
    assert plan.mode == "ESCAPE"
    assert plan.blocker_tau == pytest.approx(2.0)
    ax, ay = plan.aim
    assert not (900 <= ax <= 1020 and 330 <= ay <= 720)   # out of the spire


def test_what_is_far_or_not_closing_is_no_obstacle():
    far = _block(800, 400, 1300, 500, tau=20.0)
    receding = _block(800, 400, 1300, 500, tau=None)
    for shape in (far, receding):
        plan = _planner().plan(_view(shape), PathGoal("resupply", 1000, 300), 10.0)
        assert plan.mode == "DIRECT"


def test_a_goal_against_terrain_that_could_not_be_measured_is_blind():
    """2026-10-15, five hours in shadow: counting such outlines as obstacles
    raised an alarm on 82% of objective approaches and on 6 of 7 successful
    pickups. Nothing in the view can say whether the goal is in front of them."""
    ground = _block(700, 250, 1300, 700, tau=None, measured=False)
    plan = _planner().plan(_view(ground), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "BLIND"
    assert plan.reason == "the goal is against terrain it cannot measure"


def test_unmeasured_terrain_on_the_line_is_blind_not_a_detour():
    ridge = _block(800, 400, 1300, 500, tau=None, measured=False)
    plan = _planner().plan(_view(ridge), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "BLIND"
    assert plan.reason == "terrain it cannot measure on the line to the goal"


def test_terrain_behind_the_goal_does_not_block():
    """The goal is drawn against a cliff closing in 4 s; a ridge on the line
    closes in 3.5 s, no sooner than 0.7 of the cliff's time: it is at the
    goal's depth or behind it, not in front."""
    cliff = _block(950, 200, 1150, 380, tau=4.0)
    ridge = _block(900, 400, 1100, 470, tau=3.5)
    plan = _planner().plan(_view(cliff, ridge), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "DIRECT"


def test_terrain_nearer_than_the_goals_surface_blocks():
    cliff = _block(950, 200, 1150, 380, tau=6.0)
    ridge = _block(900, 400, 1100, 470, tau=2.5)       # under 0.7 of 6 s
    plan = _planner().plan(_view(cliff, ridge), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "DETOUR"
    assert plan.blocker_tau == pytest.approx(2.5)
    assert plan.wall_tau == pytest.approx(6.0)          # the cliff, still behind it


def test_open_air_has_unlimited_room_and_says_so():
    plan = _planner().plan(_view(), PathGoal("resupply", 1100, 400), 10.0)
    assert plan.goal_clear_px == float("inf")
    assert " clear=inf " in fmt_plan(plan)


def test_an_unreadable_view_is_blind_and_aims_at_the_goal():
    plan = _planner().plan(_view(status="few-points"), PathGoal("crown", 1000, 300), 10.0)
    assert plan.mode == "BLIND" and plan.aim == (1000, 300)


def test_a_goal_outside_the_analysed_view_is_blind():
    """Below y 720 the own airframe fills the picture: the planner cannot vouch
    for the line to a marker down there (CR-019-12)."""
    plan = _planner().plan(_view(), PathGoal("resupply", 1000, 900), 10.0)
    assert plan.mode == "BLIND"
    assert plan.reason == "goal outside the analysed view"


def test_the_corridor_starts_where_the_picture_expands_from():
    plan = _planner().plan(_view(heading=(1100.0, 650.0)), PathGoal("resupply", 1200, 400), 10.0)
    assert plan.origin == pytest.approx((1100.0, 650.0))


def test_an_expansion_point_off_the_view_falls_back_to_the_bore_sight():
    plan = _planner().plan(_view(heading=(-6711.0, 4094.0)), PathGoal("resupply", 1200, 400), 10.0)
    assert plan.origin == pytest.approx(_CENTRE)


def test_a_detour_keeps_its_side_for_hold_s():
    """A narrow bar between the flight path and the goal: a goal just right of
    the bar's middle goes round the right at once, but after a detour round
    the left it stays left until hold_s has passed."""
    bar = (930, 380, 990, 560)
    assert _planner(hold_s=0.0).plan(
        _view(_block(*bar, tau=5.0)), PathGoal("resupply", 970, 300), 10.0).side == "right"
    planner = _planner(hold_s=5.0)
    first = planner.plan(_view(_block(*bar, tau=5.0)), PathGoal("resupply", 950, 300), 10.0)
    kept = planner.plan(_view(_block(*bar, tau=5.0)), PathGoal("resupply", 970, 300), 11.0)
    later = planner.plan(_view(_block(*bar, tau=5.0)), PathGoal("resupply", 970, 300), 16.5)
    assert (first.side, kept.side, later.side) == ("left", "left", "right")


@pytest.mark.parametrize("dx, dy, side", [
    (0, -50, "over"), (0, 50, "under"), (-50, 0, "left"), (50, 0, "right"), (40, -40, "over")])
def test_side_names(dx, dy, side):
    assert _side(dx, dy) == side


def test_fmt_plan():
    plan = PathPlan("DETOUR", PathGoal("control point", 1000, 300, (40, 41)), (990.4, 250.2),
                    (960, 600), side="over", blocker_tau=3.14, goal_clear_px=12.0,
                    reason="the goal is against terrain")
    assert fmt_plan(plan) == ("mode=DETOUR_OVER goal=control_point at=(1000,300) size=40x41 "
                              "aim=(990,250) origin=(960,600) clear=12px blocker=3.1s "
                              "wall=- wall_local=- why=the_goal_is_against_terrain")
    assert " wall=2.2s wall_local=4.0s " in fmt_plan(replace(plan, wall_tau=2.24,
                                                             wall_local_tau=4.0))
    assert fmt_plan(None) == "n/a"


def test_a_view_with_a_goal_carries_the_plan_and_logs_it(caplog):
    perception = PathPerception({}, {"enabled": True})
    view = _view(_block(900, 330, 1020, 720, tau=5.0))
    with caplog.at_level(logging.DEBUG, logger="wingman.clear_path"):
        planned = perception.plan(view, PathGoal("resupply", 960, 300))
    assert planned.plan is not None and planned.plan.mode == "DETOUR"
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("PATH: mode=DETOUR_OVER goal=resupply")
               and m.endswith(" spd=- path=-") for m in messages)
    caplog.clear()
    with caplog.at_level(logging.DEBUG, logger="wingman.clear_path"):
        perception.plan(view, PathGoal("resupply", 960, 300), own=(1134.4, -12.6))
    assert any(r.getMessage().endswith(" spd=1134 path=-13") for r in caplog.records)
    assert any(m.startswith("PATH[shadow]: DETOUR OVER for the resupply") for m in messages)
    assert perception.plan(view, None) is view           # no goal, no plan


def test_the_worker_plans_with_the_goal_handed_over_with_the_picture():
    class _Fixed(PathPerception):
        def observe(self, frame, picture_ts):
            return _view(_block(900, 330, 1020, 720, tau=5.0))

    perception = _Fixed({}, {"enabled": True})
    got = []
    perception.start()
    try:
        perception.submit(np.zeros((1200, 1920, 3), np.uint8), 10.0,
                          lambda frame, view: got.append(view), goal=PathGoal("crown", 960, 300))
        deadline = time.time() + 3.0
        while not got and time.time() < deadline:
            time.sleep(0.02)
    finally:
        perception.stop()
    assert got and got[0].plan is not None and got[0].plan.goal.kind == "crown"
    assert perception.last is got[0]
    assert perception._plan_modes["DETOUR"] == 1


# ---------------------------------------------------------------------------
# The wall: the surface behind the goal. 2026-10-15, 62 resupply climb-outs:
# the fastest-closing middle outline in the 2 s before the climb-out was a
# median 2.2 s ahead of the 26 that ended in a death within 10 s, 4.8 s ahead
# of the 36 that did not. Reported only: no mode depends on it.
# ---------------------------------------------------------------------------

def test_the_closing_surface_behind_the_goal_is_reported_as_the_wall():
    """The marker drawn against a cliff closing in 2.5 s with nothing nearer:
    the line is clear, so the plan stays direct, and it says how soon the
    cliff is closing."""
    cliff = _block(950, 200, 1150, 380, tau=2.5)
    plan = _planner().plan(_view(cliff), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "DIRECT"
    assert plan.wall_tau == pytest.approx(2.5)
    assert plan.wall_close                                  # under wall_warn_s, 3 s
    assert not _planner(wall_warn_s=2.0).plan(
        _view(cliff), PathGoal("resupply", 1000, 300), 10.0).wall_close


@pytest.mark.parametrize("tau", [None, 20.0])
def test_a_surface_not_closing_within_the_horizon_is_no_wall(tau):
    cliff = _block(950, 200, 1150, 380, tau=tau)
    plan = _planner().plan(_view(cliff), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "DIRECT" and plan.wall_tau is None


def test_open_air_and_unmeasured_surfaces_have_no_wall():
    assert _planner().plan(_view(), PathGoal("resupply", 1000, 300), 10.0).wall_tau is None
    ground = _block(700, 250, 1300, 700, tau=None, measured=False)
    plan = _planner().plan(_view(ground), PathGoal("resupply", 1000, 300), 10.0)
    assert plan.mode == "BLIND" and plan.wall_tau is None


def test_flying_at_the_goals_own_surface_escapes_and_reports_the_wall():
    spire = _block(900, 330, 1020, 720, tau=2.0)
    plan = _planner().plan(_view(spire), PathGoal("resupply", 960, 400), 10.0)
    assert plan.mode == "ESCAPE"
    assert plan.wall_tau == pytest.approx(2.0)


def test_a_close_wall_is_logged_once_and_counted(caplog):
    perception = PathPerception({}, {"enabled": True, "planner": {"wall_warn_s": 3.0}})
    goal = PathGoal("resupply", 1000, 300)
    close = _view(_block(950, 200, 1150, 380, tau=2.5))
    farther = _view(_block(950, 200, 1150, 380, tau=5.0))
    with caplog.at_level(logging.INFO, logger="wingman.clear_path"):
        for ts, view in ((10.0, close), (10.2, close), (10.4, close), (10.6, farther)):
            perception.plan(replace(view, picture_ts=ts), goal)
    walls = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("PATH[shadow]: wall ")]
    assert walls == ["PATH[shadow]: wall behind the resupply closing in 2.5 s, "
                     "round the goal -"]
    perception._statuses["ok"] += 1         # end() summarises only after a reading
    perception._costs.append(30.0)
    assert perception.end().endswith(" wall_close=3")


def test_the_wall_is_written_beside_the_goals_ring(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    view = _view(_block(950, 200, 1150, 380, tau=2.5))
    plan = _planner().plan(view, PathGoal("resupply", 1000, 300), 10.0)
    renderer._draw_path_view(np.zeros((1200, 1920, 3), np.uint8), replace(view, plan=plan))
    wall = [t for t in renderer._texts if t[0] == "wall 2.5s"]
    assert wall and wall[0][3] == hud_module._RED


def _with_dots(view, before, after):
    shape = replace(view.shape, points=np.float32(after), points_before=np.float32(before))
    return replace(view, shape=shape)


def _grown(points, about, scale):
    points = np.float32(points)
    return np.float32(about) + scale * (points - np.float32(about))


_NEAR = [(x, y) for x in range(960, 1101, 20) for y in range(220, 361, 20)]   # round (1000, 300)
_FAR = [(x, y) for x in range(560, 701, 20) for y in range(560, 641, 20)]     # the ground below


def test_the_local_wall_is_the_growth_of_the_dots_round_the_goal_alone():
    """The ground under the aircraft grows by half in the interval, the cliff
    behind the goal by a tenth: the outline-wide figure says nothing about
    which, the local one is the cliff's, dt / 0.1."""
    cliff = _block(950, 200, 1150, 380, tau=0.4)          # its one fit, set by the fastest part
    ground = _block(500, 540, 760, 700, tau=0.4)
    before = _NEAR + _FAR
    after = np.vstack([_grown(_NEAR, (1000, 300), 1.1), _grown(_FAR, (630, 600), 1.5)])
    view = _with_dots(_view(cliff, ground), before, after)
    plan = _planner(wall_radius_px=150, wall_min_dots=8).plan(
        view, PathGoal("resupply", 1000, 300), 10.0)
    assert plan.wall_tau == pytest.approx(0.4)
    assert plan.wall_local_tau == pytest.approx(0.17 / 0.1, rel=0.02)
    assert " wall_local=1.7s " in fmt_plan(plan)


def test_no_local_wall_against_open_air_with_too_few_dots_or_none_growing():
    cliff = _block(950, 200, 1150, 380, tau=None)
    planner = _planner(wall_radius_px=150, wall_min_dots=8)
    grown = _with_dots(_view(cliff), _NEAR, _grown(_NEAR, (1000, 300), 1.1))
    assert planner.plan(grown, PathGoal("resupply", 700, 300), 10.0).wall_local_tau is None
    few = _with_dots(_view(cliff), _NEAR[:5], _grown(_NEAR[:5], (1000, 300), 1.1))
    assert planner.plan(few, PathGoal("resupply", 1000, 300), 10.0).wall_local_tau is None
    still = _with_dots(_view(cliff), _NEAR, _NEAR)
    assert planner.plan(still, PathGoal("resupply", 1000, 300), 10.0).wall_local_tau is None


def test_the_local_wall_is_written_beside_the_wall():
    renderer = HudRenderer("/dev/null", interval_sec=0.0)
    view = _view(_block(950, 200, 1150, 380, tau=2.5))
    plan = replace(_planner().plan(view, PathGoal("resupply", 1000, 300), 10.0),
                   wall_local_tau=6.0)
    renderer._draw_path_view(np.zeros((1200, 1920, 3), np.uint8), replace(view, plan=plan))
    wall = [t for t in renderer._texts if t[0] == "wall 2.5s  local 6.0s"]
    assert wall and wall[0][3] == hud_module._RED          # the nearer of the two

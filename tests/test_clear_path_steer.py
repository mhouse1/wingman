"""Code Review 019 step 3e: the resupply steering follows the planner's aim.

Through the real pursue_and_engage loop. The perception is a stand-in that
plans on every picture, as the worker does, so what is checked is only what
the steering does with a plan: follow its aim while it detours or escapes,
the marker otherwise, and the marker always while steering is off.
"""

import pytest

import wingman.controller as controller_module
from wingman.clear_path import PathGoal, PathPlan, PathView
from wingman.controller import ROLL_LEFT_KEY, ROLL_RIGHT_KEY
from wingman.resupply import ResupplyMarker

from test_clear_path import _AliveAnalyzer, _FrameCapture
from test_pursuit_mode import _TrackerStub, _keys, _make_ctrl, _wait_for_pursuit_to_settle

# The test frames are 960 by 600 (_FrameCapture): the bore sight is at x 480.
_MARKER = ResupplyMarker(800, 300, 43, 42, 425, 0)     # right of the bore sight


class _Planning:
    """Plans on each picture it is handed: `mode`, aim 600 px left of the goal."""

    enabled = True

    def __init__(self, mode="DETOUR", age_s=0.0):
        self.mode = mode
        self.age_s = age_s
        self.last = None

    def reset(self):
        self.last = None

    def start(self):
        pass

    def stop(self, timeout=1.0):
        pass

    def end(self):
        return None

    def submit(self, frame, picture_ts, on_view=None, goal=None, own=None):
        if goal is None:
            return
        plan = PathPlan(self.mode, goal, (goal.x - 600.0, goal.y), (480.0, 300.0),
                        side="left", blocker_tau=3.5, reason="test")
        self.last = PathView(status="ok", picture_ts=picture_ts - self.age_s, plan=plan)


def _rolls(monkeypatch, perception, steer=True):
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _f, **_kw: _MARKER)
    ctrl = _make_ctrl(monkeypatch, analyzer=_AliveAnalyzer(ammo=0), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.8,
                      empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.set_path_perception(perception)
    ctrl._clear_path_steer["resupply"] = steer
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    return [key for action, key in _keys(ctrl)
            if action == "key_press" and key in (ROLL_LEFT_KEY, ROLL_RIGHT_KEY)]


def test_a_detour_steers_at_its_aim_not_at_the_marker(monkeypatch):
    rolls = _rolls(monkeypatch, _Planning("DETOUR"))
    assert ROLL_LEFT_KEY in rolls and rolls[-1] == ROLL_LEFT_KEY


def test_an_escape_steers_at_its_aim_too(monkeypatch):
    rolls = _rolls(monkeypatch, _Planning("ESCAPE"))
    assert rolls and rolls[-1] == ROLL_LEFT_KEY


@pytest.mark.parametrize("perception, steer", [
    (_Planning("DETOUR"), False),          # steering off: the plan is shadow
    (_Planning("DIRECT"), True),           # the line is clear
    (_Planning("BLIND"), True),            # the approach decision, not the planner's
    (_Planning("DETOUR", age_s=1.0), True),  # a plan from a picture too old
])
def test_otherwise_the_marker_is_flown_at(monkeypatch, perception, steer):
    rolls = _rolls(monkeypatch, perception, steer=steer)
    assert rolls and ROLL_LEFT_KEY not in rolls


def test_the_aim_moves_with_the_marker_since_the_planned_picture(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl._clear_path_steer["resupply"] = True
    perception = _Planning("DETOUR")
    perception.submit(None, 10.0, goal=PathGoal("resupply", 800.0, 300.0))
    moved = ResupplyMarker(820, 290, 43, 42, 425, 0)
    aim, plan = ctrl._planned_aim("resupply", moved, 10.2, perception)
    assert plan is not None and aim == pytest.approx((220.0, 290.0))


def test_steering_is_off_unless_configured(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    assert ctrl._clear_path_steer == {"resupply": False, "control point": False,
                                      "crown": False, "target": False}


def test_a_plan_for_another_goal_is_not_followed(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    ctrl._clear_path_steer["resupply"] = True
    perception = _Planning("DETOUR")
    perception.submit(None, 10.0, goal=PathGoal("crown", 800.0, 300.0))
    aim, plan = ctrl._planned_aim("resupply", _MARKER, 10.1, perception)
    assert plan is None and aim == (800.0, 300.0)

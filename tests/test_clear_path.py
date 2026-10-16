"""Code Review 019 step 3a: terrain perception on the pursuit's own frames.

`PathPerception` is checked on pictures whose motion is known exactly (an
object growing about the middle, a repeated picture), the HUD layer on the
real `HudRenderer`, and the wiring through the real `pursue_and_engage` loop.
Shadow throughout: nothing here may change what the pursuit presses.
"""

import inspect
import logging
import time

import cv2
import numpy as np
import pytest

from wingman import hud as hud_module
from wingman import main as main_module
from wingman.clear_path import PathPerception, PathView, fmt_view
from wingman.controller import FIRE_ACTIVE_WEAPON
from wingman.hud import HudRenderer
from wingman.terrain_loom import LoomReading
from wingman.terrain_shapes import Shape, ShapeReading

from test_pursuit_mode import (
    _AnalyzerStub, _TrackerStub, _keys, _make_ctrl, _wait_for_pursuit_to_settle,
)

_H, _W = 600, 960
_DT = 0.12
_SKY = (200, 150, 90)       # plain, and not a HUD colour


def _texture(seed: int) -> np.ndarray:
    """Clutter at random places, so no edge repeats from picture to picture."""
    rng = np.random.default_rng(seed)
    img = np.full((_H, _W), 110, np.uint8)
    for _ in range(500):
        x, y = int(rng.integers(0, _W)), int(rng.integers(0, _H))
        w, h = int(rng.integers(8, 40)), int(rng.integers(8, 40))
        cv2.rectangle(img, (x, y), (x + w, y + h), int(rng.integers(30, 230)), -1)
    return cv2.cvtColor(cv2.GaussianBlur(img, (3, 3), 0), cv2.COLOR_GRAY2BGR)


def _rock(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = rng.integers(60, 200, (_H // 6, _W // 6), dtype=np.uint8)
    img = cv2.resize(img, (_W, _H), interpolation=cv2.INTER_NEAREST)
    for _ in range(1500):
        x, y = int(rng.integers(0, _W)), int(rng.integers(0, _H))
        w, h = int(rng.integers(4, 14)), int(rng.integers(4, 14))
        cv2.rectangle(img, (x, y), (x + w, y + h), int(rng.integers(30, 230)), -1)
    return cv2.cvtColor(cv2.GaussianBlur(img, (3, 3), 0), cv2.COLOR_GRAY2BGR)


def _scene(*rects, seed=50):
    """Plain sky with rock inside each (x1, y1, x2, y2) rectangle."""
    clutter = _rock(seed)
    out = np.full((_H, _W, 3), _SKY, np.uint8)
    for x1, y1, x2, y2 in rects:
        out[y1:y2, x1:x2] = clutter[y1:y2, x1:x2]
    return out


def _zoomed(frame, scale, about):
    cx, cy = about
    m = np.float32([[scale, 0, (1 - scale) * cx], [0, scale, (1 - scale) * cy]])
    return cv2.warpAffine(frame, m, (_W, _H), borderMode=cv2.BORDER_REFLECT)


def _shifted(frame, dx, dy):
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    return cv2.warpAffine(frame, m, (_W, _H), borderMode=cv2.BORDER_REFLECT)


def _warm(perception: PathPerception, t0: float = 100.0) -> float:
    """Show the perception twelve unrelated pictures, past the HUD mask's
    warm-up; the picture time after the last."""
    t = t0
    for seed in range(100, 112):
        perception.observe(_texture(seed), t)
        t += _DT
    return t


def _perception(**cfg) -> PathPerception:
    settings = {"enabled": True}
    settings.update(cfg)
    return PathPerception({}, settings)


# ---------------------------------------------------------------------------
# The perception
# ---------------------------------------------------------------------------

def test_the_first_picture_gives_no_view():
    perception = _perception()
    assert perception.observe(_texture(1), 1.0) is None


def test_views_wait_for_the_hud_mask_to_warm_up():
    perception = _perception()
    perception.observe(_texture(1), 1.0)
    view = perception.observe(_texture(2), 1.0 + _DT)
    assert view is not None and view.status == "warming" and not view.readable


def test_an_object_growing_in_the_middle_reads_closing_with_its_time_to_contact():
    perception = _perception()
    t = _warm(perception)
    scene = _scene((380, 130, 620, 300))
    perception.observe(_zoomed(scene, 1.00, (500, 215)), t)
    view = perception.observe(_zoomed(scene, 1.02, (500, 215)), t + _DT)
    assert view is not None and view.readable
    near = view.nearest
    assert near is not None and near.centred
    # 2 percent in 0.12 s: contact in 6 s.
    assert near.tau == pytest.approx(_DT / 0.02, rel=0.25)
    assert view.verdict().startswith("closing") and view.verdict().endswith("middle")
    assert view.dt == pytest.approx(_DT)
    assert 0.0 < view.shape.free_frac < 1.0


def test_an_object_that_slides_without_growing_is_not_closing():
    perception = _perception()
    t = _warm(perception)
    scene = _scene((380, 130, 620, 300))
    perception.observe(scene, t)
    view = perception.observe(_shifted(scene, 3, 0), t + _DT)
    assert view is not None and view.readable
    assert not view.verdict().startswith("closing")


def test_a_repeated_picture_gives_no_view_and_the_pair_keeps_its_first_picture():
    """Two equal pictures show no growth, and no growth must not read as far.
    The next new picture pairs with the last one that was different, over the
    whole interval since it."""
    perception = _perception()
    t = _warm(perception)
    scene = _scene((380, 130, 620, 300))
    first = _zoomed(scene, 1.00, (500, 215))
    perception.observe(first, t)
    assert perception.observe(first.copy(), t + _DT) is None
    view = perception.observe(_zoomed(scene, 1.04, (500, 215)), t + 2 * _DT)
    assert view is not None and view.readable
    assert view.dt == pytest.approx(2 * _DT)
    assert view.nearest.tau == pytest.approx(2 * _DT / 0.04, rel=0.25)


def test_switched_off_it_takes_no_view():
    perception = PathPerception({}, {"enabled": False})
    for k in range(4):
        assert perception.observe(_texture(k), 1.0 + k * _DT) is None


def test_it_reads_on_its_own_detectors_whatever_the_main_ticks_switches_say():
    perception = PathPerception({"enabled": False, "shapes": {"enabled": False}},
                                {"enabled": True})
    t = _warm(perception)
    scene = _scene((380, 130, 620, 300))
    perception.observe(scene, t)
    view = perception.observe(_zoomed(scene, 1.02, (500, 215)), t + _DT)
    assert view is not None and view.readable


def test_reset_forgets_the_last_picture_but_keeps_the_hud_mask():
    perception = _perception()
    t = _warm(perception)
    perception.reset()
    assert perception.last is None
    scene = _scene((380, 130, 620, 300))
    assert perception.observe(scene, t) is None        # the first picture again
    view = perception.observe(_zoomed(scene, 1.02, (500, 215)), t + _DT)
    assert view is not None and view.readable           # no second warm-up


def test_a_failing_measurement_is_an_error_view_not_an_exception(monkeypatch):
    perception = _perception()
    t = _warm(perception)

    def broken(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(perception._shapes, "measure", broken)
    perception.observe(_texture(1), t)
    view = perception.observe(_texture(2), t + _DT)
    assert view is not None and view.status == "error" and not view.readable
    assert view.verdict() == "n/a(error)"


def test_each_view_is_one_debug_line_and_each_pursuit_one_summary(caplog):
    perception = _perception()
    with caplog.at_level(logging.DEBUG, logger="wingman.clear_path"):
        t = _warm(perception)
        scene = _scene((380, 130, 620, 300))
        perception.observe(scene, t)
        perception.observe(scene.copy(), t + _DT)         # a repeat: no view
        perception.observe(_zoomed(scene, 1.04, (500, 215)), t + 2 * _DT)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("PATHVIEW: ")]
    # Eleven warm-up pairs (nine before the HUD mask is ready), the scene's
    # first picture against the last warm-up one, and the growing pair.
    assert len(lines) == 13
    assert "verdict=closing" in lines[-1] and " heading=(" in lines[-1]
    summary = perception.end()
    assert summary.startswith("PATHVIEW SUMMARY: readings=13 ")
    assert "warming=9" in summary and "repeats=1" in summary
    assert perception.end() is None     # the counts start again


def test_fmt_view_without_a_view():
    assert fmt_view(None) == "n/a"


# ---------------------------------------------------------------------------
# The HUD layer
# ---------------------------------------------------------------------------

def _shape(outline, tau, centred=True, measured=True):
    return Shape(outline=np.float32(outline), dots=40, area_first=1.0, area_last=1.0,
                 tau=tau, centred=centred, measured=measured)


def _view(shapes, texture=None, pair=None, origin=(192, 72), scale=1.0):
    if texture is None:
        texture = np.zeros((288, 576), np.uint8)
    reading = ShapeReading(status="ok", shapes=tuple(shapes), texture=texture,
                           view_origin=origin, view_scale=scale)
    return PathView(status="ok", picture_ts=1.0, dt=0.17, shape=reading, pair=pair,
                    tau_warn_s=8.0, cost_ms=30.0)


def _square(x1, y1, x2, y2):
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def test_free_air_is_tinted_and_outside_the_view_is_dimmed(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    texture = np.zeros((288, 576), np.uint8)
    texture[100:200, 200:300] = 255                  # one textured block
    canvas = np.full((_H, _W, 3), 120, np.uint8)
    renderer._draw_path_view(canvas, _view([], texture=texture))
    outside = canvas[20, 20]
    free = canvas[72 + 50, 192 + 50]
    solid = canvas[72 + 150, 192 + 250]
    assert int(outside[0]) < 120 and int(outside[1]) < 120      # dimmed
    assert int(free[1]) > 120 and int(free[0]) < 120            # greener
    assert tuple(int(c) for c in solid) == (120, 120, 120)      # left as it was


@pytest.mark.parametrize("tau, colour", [
    (2.0, hud_module._RED), (6.0, hud_module._AMBER), (None, hud_module._WHITE)])
def test_an_outline_is_coloured_by_its_time_to_contact(tmp_path, tau, colour):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_path_view(canvas, _view([_shape(_square(300, 200, 500, 350), tau)]))
    assert tuple(int(c) for c in canvas[200, 400]) == colour


def test_the_heading_point_is_drawn_where_the_picture_expands_from(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    pair = LoomReading(status="ok", dt=0.17, scale=1.03, tau=5.0, fixed=(480.0, 300.0))
    canvas = np.zeros((_H, _W, 3), np.uint8)
    renderer._draw_path_view(canvas, _view([], pair=pair))
    assert tuple(int(c) for c in canvas[300, 480 - 25]) == hud_module._CYAN   # a wing
    assert any(t[0] == "heading" for t in renderer._texts)


def test_the_banner_says_what_the_view_saw(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer._draw_path_view(np.zeros((_H, _W, 3), np.uint8),
                             _view([_shape(_square(300, 200, 500, 350), 3.0)]))
    banner = [t for t in renderer._texts if t[0].startswith("PATH[SHADOW]: ")]
    assert banner and banner[0][0] == "PATH[SHADOW]: closing 3.0s middle"
    assert banner[0][3] == hud_module._RED


def _render_labels(monkeypatch, renderer, frame, **kwargs):
    labels = []
    original = hud_module._txt

    def record(canvas, text, x, y, *args, **kw):
        labels.append((text, x, y, args[1] if len(args) > 1 else kw.get("scale")))
        original(canvas, text, x, y, *args, **kw)

    monkeypatch.setattr(hud_module, "_txt", record)
    thread = renderer.maybe_render(frame, None, "PURSUIT_MODE", 100, 2, 4, **kwargs)
    thread.join(timeout=5)
    return labels


def test_with_a_view_the_main_ticks_overlays_are_not_drawn(tmp_path, monkeypatch):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer.set_loom_source((0.38, 0.30, 0.62, 0.62), 8.0)
    renderer.set_loom_reading(LoomReading(status="ok", dt=0.12, tau=4.0), False, time.time())
    labels = _render_labels(monkeypatch, renderer, np.zeros((_H, _W, 3), np.uint8),
                            path_view=_view([]))
    texts = [t[0] for t in labels]
    assert any(t.startswith("PATH[SHADOW]: ") for t in texts)
    assert not any(t.startswith("Loom[SHADOW]") for t in texts)
    assert "acq" not in texts           # it would sit on the PATH line


def test_without_a_view_the_main_ticks_overlays_are_drawn_as_before(tmp_path, monkeypatch):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    renderer.set_loom_source((0.38, 0.30, 0.62, 0.62), 8.0)
    renderer.set_loom_reading(LoomReading(status="ok", dt=0.12, tau=4.0), False, time.time())
    labels = _render_labels(monkeypatch, renderer, np.zeros((_H, _W, 3), np.uint8))
    texts = [t[0] for t in labels]
    assert any(t.startswith("Loom[SHADOW]") for t in texts)
    assert not any(t.startswith("PATH[SHADOW]") for t in texts)


def test_text_is_drawn_after_the_downscale_at_a_size_that_reads(tmp_path, monkeypatch):
    """CR-019-09: drawn before the halving, the status lines came out about
    6 px tall. Now they are drawn on the halved picture at their own size."""
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    labels = _render_labels(monkeypatch, renderer, np.zeros((1200, 1920, 3), np.uint8))
    time_line = next(t for t in labels if "[PURSUIT_MODE]" in t[0])
    assert (time_line[1], time_line[2]) == (4, hud_module._row(0) // 2)
    assert time_line[3] == pytest.approx(0.52)
    written = cv2.imread(str(tmp_path / "live_hud.png"))
    assert written.shape == (600, 960, 3)


# ---------------------------------------------------------------------------
# The pursuit loop
# ---------------------------------------------------------------------------

class _AliveAnalyzer(_AnalyzerStub):
    """Health being read: the pursuit hands its pictures to the perception."""

    game_battle_alive = True


class _FrameCapture:
    """A new picture on every grab: rock growing about the middle."""

    def __init__(self):
        self.grabs = 0
        self._scene = _scene((380, 130, 620, 300))

    def grab_from_thread(self):
        self.grabs += 1
        return _zoomed(self._scene, 1.0 + 0.005 * (self.grabs % 40), (500, 215))


class _StubPerception:
    """Stands in for the worker: each submitted picture gives a view at once."""

    enabled = True

    def __init__(self, fail=False):
        self.frames = []
        self.goals = []
        self.owns = []
        self.resets = 0
        self.starts = 0
        self.stops = 0
        self.fail = fail

    def reset(self):
        self.resets += 1

    def start(self):
        self.starts += 1

    def stop(self, timeout=1.0):
        self.stops += 1

    def submit(self, frame, picture_ts, on_view=None, goal=None, own=None):
        if self.fail:
            raise RuntimeError("perception broke")
        self.frames.append(frame)
        self.goals.append(goal)
        self.owns.append(own)
        if on_view is not None:
            on_view(frame, PathView(status="ok", picture_ts=picture_ts, dt=0.17))

    def end(self):
        return "PATHVIEW SUMMARY: readings=3 ok=3 repeats=0 dropped=0 cost_median=1ms cost_max=2ms"


class _RecordingHud:
    def __init__(self):
        self.calls = []

    def maybe_render(self, frame, obs, state, health, missiles, flares, **kwargs):
        self.calls.append((frame, state, kwargs.get("path_view")))
        return object()


def _pursue(monkeypatch, perception, hud=None, seconds=0.6, analyzer=None):
    ctrl = _make_ctrl(monkeypatch, analyzer=analyzer or _AliveAnalyzer(ammo=2),
                      capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=seconds)
    ctrl.set_target_tracker(_TrackerStub())
    if hud is not None:
        ctrl.set_hud_renderer(hud)
    ctrl.set_path_perception(perception)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl, timeout=seconds + 3.0)
    return ctrl


def test_the_pursuit_takes_a_view_of_each_picture_and_draws_the_hud_from_it(monkeypatch):
    perception = _StubPerception()
    hud = _RecordingHud()
    _pursue(monkeypatch, perception, hud)
    assert perception.resets == 1 and perception.starts == 1 and perception.stops == 1
    assert len(perception.frames) >= 3
    drawn = [frame for frame, _state, view in hud.calls if view is not None]
    assert drawn, "the HUD was never drawn with the pursuit's own view"
    for frame in drawn:
        assert any(frame is seen for seen in perception.frames)   # the picture it was taken on


def test_a_failing_view_costs_neither_the_steering_nor_the_firing(monkeypatch, caplog):
    with caplog.at_level(logging.ERROR, logger="wingman.controller"):
        ctrl = _pursue(monkeypatch, _StubPerception(fail=True))
    assert ("key_press", FIRE_ACTIVE_WEAPON) in _keys(ctrl)
    failures = [r for r in caplog.records if "pursuit terrain view failed" in r.getMessage()]
    assert len(failures) == 1                    # logged once, not every cycle
    assert not [r for r in caplog.records if "loop cycle failed" in r.getMessage()]


def test_the_pursuit_logs_the_views_summary_when_it_ends(monkeypatch, caplog):
    with caplog.at_level(logging.INFO, logger="wingman.controller"):
        _pursue(monkeypatch, _StubPerception())
    assert any(r.getMessage().startswith("PATHVIEW SUMMARY: ") for r in caplog.records)


def test_a_switched_off_perception_leaves_the_hud_to_the_engage_cadence(monkeypatch):
    perception = _StubPerception()
    perception.enabled = False
    hud = _RecordingHud()
    _pursue(monkeypatch, perception, hud)
    assert perception.frames == []
    assert hud.calls and all(view is None for _f, _s, view in hud.calls)


class _SlowPerception(PathPerception):
    """The real worker around a measurement that takes 0.3 s."""

    def __init__(self):
        super().__init__({}, {"enabled": True})
        self.measured = 0

    def observe(self, frame, picture_ts):
        time.sleep(0.3)
        self.measured += 1
        return PathView(status="ok", picture_ts=picture_ts)


def test_the_steering_does_not_wait_for_the_measurement(monkeypatch):
    """2026-10-15 07:42: measured on the steering thread, a median 46 ms a
    picture slowed the steering cycle from 0.165 s to 0.210 s. On the worker,
    a 0.3 s measurement costs the steering nothing."""
    tracker = _TrackerStub()
    perception = _SlowPerception()
    ctrl = _make_ctrl(monkeypatch, analyzer=_AliveAnalyzer(ammo=2), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=1.0)
    ctrl.set_target_tracker(tracker)
    ctrl.set_path_perception(perception)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl, timeout=4.0)
    # At 0.1 s a cycle a second holds about nine; waiting 0.3 s on each would
    # leave about three.
    assert tracker.updates >= 6
    assert 1 <= perception.measured <= 5
    assert perception._thread is None            # stopped with the pursuit


def test_the_worker_measures_the_newest_picture_and_drops_the_ones_it_missed():
    perception = _SlowPerception()
    seen = []
    perception.start()
    try:
        for k in range(5):
            perception.submit(_texture(k), 1.0 + 0.05 * k,
                              lambda frame, view: seen.append(view.picture_ts))
            time.sleep(0.05)
        deadline = time.time() + 3.0
        while (not seen or seen[-1] != pytest.approx(1.2)) and time.time() < deadline:
            time.sleep(0.02)
    finally:
        perception.stop()
    assert seen[0] == pytest.approx(1.0)       # the first was taken at once
    assert seen[-1] == pytest.approx(1.2)      # and the newest last
    assert len(seen) < 5 and perception._dropped >= 1


def test_a_stopped_worker_takes_nothing_more():
    perception = _SlowPerception()
    perception.start()
    perception.stop()
    perception.submit(_texture(1), 1.0, lambda frame, view: None)
    time.sleep(0.4)
    assert perception.measured == 0


def test_cleanup_stops_the_worker(monkeypatch):
    ctrl = _make_ctrl(monkeypatch)
    perception = _SlowPerception()
    ctrl.set_path_perception(perception)
    perception.start()
    assert perception._thread is not None and perception._thread.is_alive()
    ctrl.cleanup()
    assert perception._thread is None


def test_main_wires_the_path_perception_for_live_runs_only():
    """The replay and capture lanes hand out scripted screenshots on every
    grab, as for the main tick's loom."""
    src = inspect.getsource(main_module.main)
    guard = src.index("if not replay_mode and not capture_mode:\n        behavior_tree.set_terrain_loom(")
    wiring = src.index("ctrl.set_path_perception(PathPerception(")
    assert guard < wiring < guard + 900


# ---------------------------------------------------------------------------
# Step 3b: the markers' size on every sighting, for their own time to contact
# ---------------------------------------------------------------------------

def test_the_resupply_line_carries_the_size_of_this_cycles_sighting(monkeypatch, caplog):
    import wingman.controller as controller_module
    from wingman.resupply import ResupplyMarker
    marker = ResupplyMarker(1400, 600, 43, 42, 425, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    ctrl = _make_ctrl(monkeypatch, analyzer=_AnalyzerStub(ammo=0), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.6,
                      empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    with caplog.at_level(logging.DEBUG, logger="wingman.controller"):
        ctrl.pursue_and_engage()
        _wait_for_pursuit_to_settle(ctrl)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("RESUPPLY: spent=")]
    # The cycle that first confirms the urgency scanned before it, so it shows no
    # sighting; every line with the marker carries this cycle's size.
    sighted = [line for line in lines if "marker=(1400,600)" in line]
    assert sighted and all(line.endswith(" size=43x42") for line in sighted)


def test_fmt_marker_size():
    from wingman.controller import _fmt_marker_size
    from wingman.resupply import ResupplyMarker
    assert _fmt_marker_size(None) == "-"
    assert _fmt_marker_size(ResupplyMarker(1, 2, 30, 31, 100, 0)) == "30x31"


def test_the_pursuit_hands_the_planner_the_marker_it_steers_at(monkeypatch):
    """Step 3d: the goal travels with the picture it was seen on."""
    import wingman.controller as controller_module
    from wingman.resupply import ResupplyMarker
    marker = ResupplyMarker(1400, 600, 43, 42, 425, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    perception = _StubPerception()
    ctrl = _make_ctrl(monkeypatch, analyzer=_AliveAnalyzer(ammo=0), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.8,
                      empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.set_path_perception(perception)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    goals = [g for g in perception.goals if g is not None]
    assert goals and goals[-1].kind == "resupply"
    assert (goals[-1].x, goals[-1].y, goals[-1].size) == (1400, 600, (43, 42))


def test_path_goal_prefers_the_marker_with_the_steering_then_the_target():
    from wingman.controller import _path_goal
    from wingman.priority_target import PriorityMarker
    from wingman.resupply import ResupplyMarker
    resupply = ResupplyMarker(1400, 600, 43, 42, 425, 0)
    crown = PriorityMarker(900, 500, 30, 30, 70, 0.0)
    target = {"centroid_x": 700.0, "centroid_y": 650.0}
    assert _path_goal(resupply, crown, "priority_target", target).kind == "resupply"
    assert _path_goal(None, crown, "priority_target", target).kind == "crown"
    assert _path_goal(None, crown, "air_superiority", target).kind == "control point"
    assert _path_goal(None, None, None, target).kind == "target"
    assert _path_goal(None, None, None, {"centroid_x": None}) is None
    assert _path_goal(None, None, None, None) is None


def test_no_picture_is_handed_over_while_health_is_not_being_read(monkeypatch):
    """2026-10-15 08:24: the view was taken of the round-end MVP card, which the
    state still called a pursuit. The main tick's detectors had this gate."""
    class _Counting(_StubPerception):
        def __init__(self):
            super().__init__()
            self.skipped = 0

        def skip_no_health(self):
            self.skipped += 1

    perception = _Counting()
    _pursue(monkeypatch, perception, analyzer=_AnalyzerStub(ammo=2))   # health not read
    assert perception.frames == [] and perception.skipped >= 3


def test_a_planner_alarm_asks_the_black_box_for_a_clip(monkeypatch):
    """Cycles 5 and 7: ESCAPE on any goal, DETOUR on an objective and a close
    wall behind the resupply marker leave a clip; DIRECT and BLIND do not, nor
    DETOUR on a target, nor a close wall behind anything else."""
    from wingman.clear_path import PathGoal, PathPlan

    class _Hud(_RecordingHud):
        def __init__(self):
            super().__init__()
            self.alarms = []

        def note_alarm(self, reason):
            self.alarms.append(reason)

    ctrl = _make_ctrl(monkeypatch)
    hud = _Hud()
    ctrl.set_hud_renderer(hud)
    on_view = ctrl._path_view_renderer(None, 100, 2, 4, None, None, False, {"ts": 0.0})
    frame = np.zeros((600, 960, 3), np.uint8)
    goal = PathGoal("resupply", 500.0, 300.0)
    for mode in ("DIRECT", "BLIND", "DETOUR", "ESCAPE"):
        plan = PathPlan(mode, goal, (400.0, 200.0), (480.0, 300.0))
        on_view(frame, PathView(status="ok", picture_ts=1.0, plan=plan))
    assert hud.alarms == ["detour-resupply", "escape-resupply"]
    hud.alarms.clear()
    target = PathGoal("target", 500.0, 300.0)
    for kind, mode, wall_close in (("target", "DETOUR", False), ("target", "ESCAPE", False),
                                   ("control point", "DETOUR", False),
                                   ("resupply", "DIRECT", True), ("target", "DIRECT", True)):
        plan = PathPlan(mode, PathGoal(kind, 500.0, 300.0) if kind != "target" else target,
                        (400.0, 200.0), (480.0, 300.0), wall_tau=2.0 if wall_close else None,
                        wall_close=wall_close)
        on_view(frame, PathView(status="ok", picture_ts=1.0, plan=plan))
    assert hud.alarms == ["escape-target", "detour-control-point", "wall-resupply"]


# ---------------------------------------------------------------------------
# Cycle 8: the aircraft's airspeed and flight-path angle on the PATH lines
# ---------------------------------------------------------------------------

def _snapshot(speed_kph, alt_rate, at=100.0, age=0.0):
    from wingman.telemetry import TelemetrySignal, TelemetrySnapshot
    return TelemetrySnapshot(
        speed=TelemetrySignal(value=speed_kph, ts=at - age),
        altitude=TelemetrySignal(value=1600, ts=at - age, rate=alt_rate),
        taken_at_s=at)


class _TelemetryAnalyzer(_AliveAnalyzer):
    def __init__(self, snap, **kw):
        super().__init__(**kw)
        self._snap = snap

    def get_telemetry(self):
        return self._snap


def test_the_pursuit_hands_over_its_airspeed_and_flight_path(monkeypatch):
    """2026-10-15 17:35 and 17:51: both climb-out deaths looked at passed the
    marker at over 1,100 KPH; the log had no speed to count it with."""
    perception = _StubPerception()
    ctrl = _make_ctrl(monkeypatch, analyzer=_TelemetryAnalyzer(_snapshot(1134, -70.0), ammo=2),
                      capture=_FrameCapture(), pursuit_enabled=True,
                      pursuit_max_duration_s=0.8, empty_confirm_reads=1)
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.set_path_perception(perception)
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    assert perception.owns
    speed, path = perception.owns[-1]
    assert speed == 1134.0
    assert path == pytest.approx(-12.8, abs=0.1)        # asin(70 / 315 m/s)


def test_own_state_is_unknown_without_a_fresh_reading(monkeypatch):
    ctrl = _make_ctrl(monkeypatch, analyzer=_TelemetryAnalyzer(None, ammo=2))
    assert ctrl._path_own_state() == (None, None)
    ctrl = _make_ctrl(monkeypatch, analyzer=_TelemetryAnalyzer(
        _snapshot(900, -40.0, age=30.0), ammo=2))           # stale
    assert ctrl._path_own_state() == (None, None)
    ctrl = _make_ctrl(monkeypatch, analyzer=_AliveAnalyzer(ammo=2))   # no telemetry at all
    assert ctrl._path_own_state() == (None, None)


def test_the_pursuit_brakes_on_a_fast_resupply_approach(monkeypatch):
    """Code Review 019 cycle 9, end to end: a pursuit steering at the resupply
    marker at 1,100 KPH holds the airbrake under the approach brake's name."""
    import wingman.controller as controller_module
    from wingman.keybindings import AIRBRAKE_KEY
    from wingman.resupply import ResupplyMarker
    marker = ResupplyMarker(1000, 600, 43, 42, 425, 0)
    monkeypatch.setattr(controller_module, "find_resupply_marker", lambda _frame, **_kw: marker)
    snap = _snapshot(1100, -40.0)

    class _Live(_TelemetryAnalyzer):
        def get_telemetry(self):
            return _snapshot(1100, -40.0, at=time.time())     # fresh on every read

    ctrl = _make_ctrl(monkeypatch, analyzer=_Live(snap, ammo=0), capture=_FrameCapture(),
                      pursuit_enabled=True, pursuit_max_duration_s=0.8,
                      empty_confirm_reads=1, resupply_priority_actuate=True)
    ctrl._approach_brake_cfg = {"enabled": True, "resupply": True, "cap_kph": 850.0,
                                "target_kph": 800.0, "decel_kph_s": 150.0, "max_dose_s": 3.0}
    ctrl.set_target_tracker(_TrackerStub(visible=False))
    ctrl.pursue_and_engage()
    _wait_for_pursuit_to_settle(ctrl)
    brakes = [i for i in ctrl.get_action_intents()
              if i.get("key") == AIRBRAKE_KEY and i.get("action") == "approach_brake"]
    assert brakes and brakes[0]["action_type"] == "key_press"
    assert brakes[-1]["action_type"] == "key_release"     # let go at the loop's exit
    assert not ctrl._approach_brake_held

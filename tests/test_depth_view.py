"""Code Review 019 cycle 11: terrain depth from one picture, in shadow.

The statistics are checked on depth maps whose answer is known; the worker
process and its protocol on the real `wingman.depth_worker` in its `--fake`
mode (no torch, no model download), the same process and pipes the session
runs.
"""

import sys
import time

import numpy as np
import pytest

from wingman.depth_view import MID_BOX, VIEW_BOX, DepthPerception, DepthReading, depth_stats, fmt_depth

_FAKE = [sys.executable, "-m", "wingman.depth_worker", "--fake", "--input-px", "60"]


def _map(h=120, w=200, far=1.0):
    return np.full((h, w), far, np.float32)


def _paint(depth, frac, value):
    h, w = depth.shape
    x1, y1, x2, y2 = frac
    depth[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)] = value
    return depth


def test_flying_at_the_nearest_thing_in_view_ranks_the_middle_first():
    depth = _paint(_map(), MID_BOX, 9.0)              # a wall right ahead
    near, mid_rank, nearest = depth_stats(depth)
    assert mid_rank > 0.9
    assert MID_BOX[0] <= nearest[0] <= MID_BOX[2] and MID_BOX[1] <= nearest[1] <= MID_BOX[3]
    assert near.shape == (int(round(0.6 * 120)) - int(round(0.12 * 120)),
                          int(round(0.8 * 200)) - int(round(0.2 * 200)))
    assert near.min() >= 0.0 and near.max() <= 1.0


def test_an_open_middle_with_terrain_at_the_side_ranks_it_last():
    depth = _paint(_map(far=5.0), (0.20, 0.12, 0.32, 0.60), 9.0)     # a cliff on the left
    depth = _paint(depth, MID_BOX, 0.5)                                # open sky ahead
    _, mid_rank, nearest = depth_stats(depth)
    assert mid_rank < 0.05
    assert nearest[0] < 0.32


def test_what_lies_outside_the_view_does_not_count():
    """The own airframe below the view and the HUD above it read nearest of
    all on every picture (the offline spike); they must not set the ranks."""
    depth = _paint(_map(), (0.30, 0.65, 0.70, 1.00), 50.0)            # the airframe
    depth = _paint(depth, (0.30, 0.00, 0.70, 0.08), 50.0)             # the top bar
    depth = _paint(depth, (0.20, 0.12, 0.80, 0.30), 4.0)              # a ridge, high in view
    _, _, nearest = depth_stats(depth)
    assert VIEW_BOX[1] <= nearest[1] <= 0.30


def test_fmt_depth():
    reading = DepthReading("ok", 1.0, 120.4, np.zeros((2, 2), np.float32), 0.834, (0.5, 0.45))
    assert fmt_depth(reading) == "status=ok ms=120 mid_rank=0.83 nearest=(0.50,0.45)"
    assert fmt_depth(DepthReading("error", detail="boom")) == "status=error boom"
    assert fmt_depth(None) == "status=none"


def _wait(predicate, timeout=20.0):
    deadline = time.time() + timeout
    while not predicate() and time.time() < deadline:
        time.sleep(0.02)
    return predicate()


def _frame():
    return np.random.default_rng(1).integers(0, 255, (300, 480, 3), np.uint8)


def test_the_worker_answers_through_its_pipes(caplog):
    perception = DepthPerception({"enabled": True, "min_interval_s": 0.0}, command=_FAKE)
    perception.start()
    try:
        assert _wait(lambda: perception.status == "ready")
        perception.submit(_frame(), 7.5)
        assert _wait(lambda: perception.last is not None)
        reading = perception.last
        assert reading.status == "ok" and reading.picture_ts == 7.5
        # The fake is near at the bottom, far at the top: the middle ranks above
        # the top of the view and below its bottom.
        assert 0.2 < reading.mid_rank < 0.8
        assert reading.nearest[1] > 0.5
        summary = perception.end()
        assert summary.startswith("DEPTHVIEW SUMMARY: readings=1 ok=1 dropped=0 ")
        assert perception.end() is None
    finally:
        perception.stop()
    assert perception._proc is None and perception.status == "off"


def test_pictures_wait_for_the_model_and_the_newest_wins():
    perception = DepthPerception({"enabled": True, "min_interval_s": 5.0}, command=_FAKE)
    perception.submit(_frame(), 1.0)                  # not started: not taken
    assert perception._pending is None
    perception.start()
    try:
        assert _wait(lambda: perception.status == "ready")
        perception._sent_at = time.monotonic()        # as if one just went
        for ts in (1.0, 2.0, 3.0):
            perception.submit(_frame(), ts)
        assert perception._dropped == 2
        assert perception._pending[1] == 3.0
    finally:
        perception.stop()


def test_a_worker_without_a_model_is_said_once_and_left_alone(caplog):
    broken = [sys.executable, "-c", "import sys; sys.exit(3)"]
    perception = DepthPerception({"enabled": True}, command=broken)
    with caplog.at_level("WARNING", logger="wingman.depth_view"):
        perception.start()
        assert _wait(lambda: perception.status == "unavailable")
    perception.submit(_frame(), 1.0)
    assert perception._pending is None
    assert sum("no model" in r.getMessage() for r in caplog.records) == 1
    perception.stop()


def test_off_unless_configured():
    perception = DepthPerception({}, command=_FAKE)
    perception.start()
    assert perception._proc is None and perception.status == "off"
    perception.stop()                                 # safe without a start


def test_the_real_worker_reports_a_missing_model_instead_of_crashing():
    """`transformers` absent or the model not loadable: an ERROR response,
    then the worker ends. Run against a name that cannot exist."""
    perception = DepthPerception(
        {"enabled": True}, command=[sys.executable, "-m", "wingman.depth_worker",
                                    "--model", "/nonexistent/model-path"])
    perception.start()
    try:
        assert _wait(lambda: perception.status == "unavailable", timeout=60.0)
    finally:
        perception.stop()


@pytest.mark.parametrize("box", [VIEW_BOX, MID_BOX])
def test_the_boxes_are_fractions(box):
    x1, y1, x2, y2 = box
    assert 0.0 <= x1 < x2 <= 1.0 and 0.0 <= y1 < y2 <= 1.0


# ---------------------------------------------------------------------------
# The HUD and the wiring
# ---------------------------------------------------------------------------

def _path_view(picture_ts, depth):
    """A readable view with no outlines, as the pursuit's perception gives one."""
    from dataclasses import replace
    from test_clear_path import _view
    return replace(_view([]), picture_ts=picture_ts, depth=depth)


def test_the_hud_draws_what_the_depth_model_saw(tmp_path):
    from wingman.hud import HudRenderer
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    near = np.linspace(0.0, 1.0, 48 * 96, dtype=np.float32).reshape(48, 96)
    reading = DepthReading("ok", 10.0, 130.0, near, 0.81, (0.5, 0.5))
    canvas = np.zeros((1200, 1920, 3), np.uint8)
    renderer._draw_path_view(canvas, _path_view(10.2, reading))
    texts = [t[0] for t in renderer._texts]
    assert "DEPTH[SHADOW] mid 0.81  130 ms" in texts
    assert canvas[252:470, 15:380].any()                      # the map, left of the view
    renderer._draw_path_view(np.zeros((1200, 1920, 3), np.uint8), _path_view(12.0, reading))
    assert any(t[0].endswith("(old)") for t in renderer._texts)


def test_no_inset_without_a_reading(tmp_path):
    from wingman.hud import HudRenderer
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    for depth in (None, DepthReading("error", 1.0, detail="boom")):
        renderer._draw_path_view(np.zeros((600, 960, 3), np.uint8), _path_view(1.0, depth))
        assert not any(t[0].startswith("DEPTH[SHADOW]") for t in renderer._texts)


def test_the_pursuit_hands_the_depth_view_its_pictures():
    import inspect
    from wingman.controller import Controller
    src = inspect.getsource(Controller.pursue_and_engage)
    assert ("if self._depth_perception is not None and path_health:\n"
            "                            try:\n"
            "                                self._depth_perception.submit(frame, picture_ts)") in src
    assert "_depth_line = self._depth_perception.end()" in src
    assert "self._depth_perception.stop(timeout=1.0)" in inspect.getsource(Controller.cleanup)
    assert 'getattr(self._depth_perception, "last", None)' in inspect.getsource(
        Controller._path_view_renderer)


def test_main_starts_it_on_live_runs_only():
    import inspect
    from wingman import main as main_module
    src = inspect.getsource(main_module.main)
    guard = src.index("if not replay_mode and not capture_mode:")
    start = src.index('depth_view = DepthPerception(cfg.get("depth_view"))')
    assert start > guard
    assert src.index("ctrl.set_depth_perception(depth_view)", start) > start

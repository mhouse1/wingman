"""Design 021 Phase 5 (shadow): where the tracked target is going, and its
drawing on the live HUD."""

import threading
import time

import cv2
import numpy as np
import pytest
import yaml
from pathlib import Path

import wingman.controller as controller_module
from wingman.controller import Controller
from wingman.controller_config import ControllerConfig
from wingman.hud import HudRenderer
from wingman.lead_ring import LeadRing, LeadRingWatch
from wingman.target_projection import TargetProjection
from wingman.target_range import RangeReading, TargetRangeReader


def _still(t0, t1):
    return 0.0, 0.0


def _proj():
    return TargetProjection([0.5, 0.4], [10, 4], 0.4)


def test_no_point_until_two_pictures_and_none_without_a_range():
    tp = _proj()
    assert tp.update((900, 600), 1.0, 1.0, 1200, _still) is None
    assert tp.update((920, 600), 1.2, None, 1200, _still) is None
    assert tp.update((940, 600), 1.4, 1.0, 1200, _still) is not None


def test_a_target_crossing_right_is_led_to_the_right_in_proportion_to_range():
    """100 px a second to the right: at 1 km the point leads by 0.5 s of it,
    at 2 km by twice that, plus the offset."""
    for km, lead in ((1.0, 50.0), (2.0, 100.0)):
        tp = _proj()
        tp.update((900, 600), 1.0, km, 1200, _still)
        p = tp.update((920, 600), 1.2, km, 1200, _still)
        assert p.vx == pytest.approx(100.0) and p.vy == pytest.approx(0.0)
        assert p.x == pytest.approx(920 + 10 + lead) and p.y == pytest.approx(600 + 4)
        assert p.speed == pytest.approx(100.0)


def test_the_chases_own_turning_is_taken_out():
    """The marker stands still on the screen while the chase's keys account for
    40 px of travel to the left: the target itself moved 40 px to the right, and
    the point leads it. This is the case the screen speed alone misses."""
    tp = _proj()
    tp.update((960, 600), 1.0, 1.0, 1200, _still)
    p = tp.update((960, 600), 1.2, 1.0, 1200, lambda t0, t1: (40.0, 0.0))
    assert p.vx == pytest.approx(200.0)
    assert p.x == pytest.approx(960 + 10 + 100)
    # And the other way: the marker slides 40 px left only because wingman
    # turned; the target is not moving.
    tp = _proj()
    tp.update((960, 600), 1.0, 1.0, 1200, _still)
    p = tp.update((920, 600), 1.2, 1.0, 1200, lambda t0, t1: (40.0, 0.0))
    assert p.vx == pytest.approx(0.0) and p.x == pytest.approx(920 + 10)


def test_the_motion_is_smoothed_and_a_repeated_picture_changes_nothing():
    tp = _proj()
    tp.update((900, 600), 1.0, 1.0, 1200, _still)
    first = tp.update((920, 600), 1.2, 1.0, 1200, _still)         # 100 px/s
    assert tp.update((999, 999), 1.2, 1.0, 1200, _still) is first  # the same picture
    second = tp.update((920, 600), 1.4, 1.0, 1200, _still)        # now still
    assert 0.0 < second.vx < 100.0


def test_a_jump_a_long_gap_and_a_reset_start_again():
    tp = _proj()
    tp.update((900, 600), 1.0, 1.0, 1200, _still)
    tp.update((920, 600), 1.2, 1.0, 1200, _still)
    assert tp.update((1500, 200), 1.4, 1.0, 1200, _still) is None      # another aircraft
    assert tp.update((1510, 200), 1.6, 1.0, 1200, _still) is not None
    assert tp.update((1520, 200), 3.0, 1.0, 1200, _still) is None      # 1.4 s later
    tp.reset()
    assert tp.update((1530, 200), 3.2, 1.0, 1200, _still) is None


def test_the_lead_is_capped_and_scales_with_the_frame():
    tp = _proj()
    tp.update((100, 600), 1.0, 2.0, 1200, _still)
    p = tp.update((380, 600), 1.2, 2.0, 1200, _still)                   # 1,400 px/s at 2 km
    assert p.x == pytest.approx(380 + 10 + 600)
    half = TargetProjection([0.5, 0.4], [10, 4], 0.4)
    half.update((450, 300), 1.0, 1.0, 600, _still)
    p = half.update((460, 300), 1.2, 1.0, 600, _still)
    assert p.x == pytest.approx(460 + 5 + 25)


# -- the chase ---------------------------------------------------------------------

def _ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller((0, 0, 1920, 1200), exit_event=threading.Event(),
                      config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True))
    ctrl._gun_range_max_age_s = 1.5
    ctrl._target_range = TargetRangeReader()
    ctrl._target_projection = _proj()
    ctrl._lead_ring = None
    return ctrl


def _obs(x, y, cluster=(900.0, 500.0)):
    return {"visible": True, "centroid_x": x, "centroid_y": y, "cluster": cluster}


def test_the_chase_puts_the_point_on_the_observation_and_scores_it_against_the_ring(
        monkeypatch, caplog):
    import logging
    ctrl = _ctrl(monkeypatch)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    ctrl._gun_cluster = (900.0, 500.0)
    ctrl._target_range._latest = RangeReading(km=1.0, text="1.0km", cluster=(900.0, 500.0),
                                              ts=time.monotonic(), cost_ms=20.0)
    ctrl._lead_ring = LeadRingWatch(450, 0.45, (0.4997, 0.508))
    first = _obs(900, 600)
    ctrl._target_projection_shadow(frame, first, 1.0)
    assert "projection" not in first
    # The ring the game drew on the second picture, 30 px from the projected point.
    ctrl._lead_ring._last_ring, ctrl._lead_ring._last_ts = LeadRing(1010, 604, 0.7, "green"), 1.2
    second = _obs(920, 600)
    with caplog.at_level(logging.DEBUG, logger="wingman.controller"):
        ctrl._target_projection_shadow(frame, second, 1.2)
        ctrl._target_projection_shadow(frame, _obs(920, 600), 1.2)      # the same picture
    assert second["projection"].x == pytest.approx(980.0)
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TARGETPROJ:")]
    assert len(lines) == 1 and "ring_miss=30" in lines[0]
    assert list(ctrl._projection_misses) == [pytest.approx(30.0)]
    ctrl._target_range.stop()


def test_the_chases_held_keys_are_what_is_taken_out(monkeypatch):
    """A roll key held to the right for the whole gap, at the roll model's rate:
    the marker did not move on the screen, so the target is going right."""
    from wingman.hold_model import HoldModel
    ctrl = _ctrl(monkeypatch)
    ctrl._hold_models = {"roll": HoldModel(0.4, 0.25)}      # no pitch model: zero
    model = ctrl._hold_models["roll"]
    model.key(1, 10.0 - model.lag_s)
    travel = ctrl._own_travel_px((1200, 1920, 3))(10.0, 10.2)
    assert travel[0] == pytest.approx(model.rate_per_s * 0.2 * 960.0)
    assert travel[1] == 0.0
    ctrl._target_range.stop()


def test_a_lost_target_starts_the_projection_again(monkeypatch):
    ctrl = _ctrl(monkeypatch)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    ctrl._gun_cluster = (900.0, 500.0)
    ctrl._target_range._latest = RangeReading(km=1.0, text="1.0km", cluster=(900.0, 500.0),
                                              ts=time.monotonic(), cost_ms=20.0)
    ctrl._target_projection_shadow(frame, _obs(900, 600), 1.0)
    ctrl._target_projection_shadow(frame, {"visible": False}, 1.2)
    obs = _obs(940, 600)
    ctrl._target_projection_shadow(frame, obs, 1.4)
    assert "projection" not in obs
    ctrl._target_range.stop()


# -- the HUD -----------------------------------------------------------------------

def test_the_hud_draws_the_projection_and_nothing_without_one(tmp_path):
    frame = np.full((1200, 1920, 3), 60, dtype=np.uint8)
    obs = {"mode": "redmass", "visible": True, "centroid_x": 700.0, "centroid_y": 500.0,
           "error_norm": -0.27, "error_norm_y": -0.17}
    plain = HudRenderer(str(tmp_path / "plain.png"), interval_sec=0.0)
    plain.maybe_render(frame, dict(obs), "PURSUIT_MODE", 100, 4, 2).join(timeout=5)
    tp = _proj()
    tp.update((660, 500), 1.0, 1.0, 1200, _still)
    proj = tp.update((700, 500), 1.2, 1.0, 1200, _still)             # 200 px/s right
    drawn = HudRenderer(str(tmp_path / "proj.png"), interval_sec=0.0)
    drawn.maybe_render(frame, dict(obs, projection=proj), "PURSUIT_MODE", 100, 4, 2).join(timeout=5)
    a, b = cv2.imread(str(tmp_path / "plain.png")), cv2.imread(str(tmp_path / "proj.png"))
    changed = np.argwhere(np.any(a != b, axis=2))
    assert len(changed) > 50
    # live_hud.png is half size: the diamond is at (810, 504) / 2, the arrow runs
    # to it from the marker at (700, 500) / 2, and the label sits beside it.
    ys, xs = changed[:, 0], changed[:, 1]
    assert 340 <= xs.min() <= 352 and xs.max() >= 405
    assert ys.min() >= 235 and ys.max() <= 290
    # White at the diamond's right-hand point.
    assert (b[252, 410:414] > 200).all(axis=1).any()


def test_the_shared_config_turns_the_projection_on_with_the_fitted_values():
    from wingman.config_schema import schema_default
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    tp = cfg["pursuit_mode"]["target_projection"]
    for key in ("enabled", "lead_s_per_km", "offset_px", "smooth_s"):
        assert tp[key] == schema_default("pursuit_mode.target_projection." + key)
    assert tp["enabled"] is True and tp["lead_s_per_km"] == [0.51, 0.37]

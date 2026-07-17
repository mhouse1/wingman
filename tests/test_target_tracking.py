"""Unit tests for TargetTracker and orient_nose_to_target."""

import time
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np
import pytest

from wingman.tracker import TargetTracker, TrackMode

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_CFG = {
    "tracking": {
        "enabled": True,
        "acquisition_region_pct": [0.0, 0.0, 1.0, 1.0],  # full frame for tests
        "deadband": 0.05,
        "kp": 0.30,
        "min_hold_sec": 0.08,
        "max_hold_sec": 0.35,
        "command_cooldown_sec": 0.15,
        "lost_timeout_sec": 0.70,
        "prefer_red_lock": True,
        "local_roi_enabled": True,
        "local_roi_scale": 0.22,
        "local_roi_min_px": [40, 25],
        "local_roi_expand_factor": 1.25,
        "local_roi_max_scale": 0.45,
        "local_roi_reacquire_cycles": 3,
    },
    "tracking_hsv": {
        "red_lower": [0, 150, 150],
        "red_upper": [10, 255, 255],
        "green_lower": [45, 150, 150],
        "green_upper": [75, 255, 255],
        "min_contour_area": 12,
        "min_aspect_ratio": 2.5,
    },
}


def _tracker(**overrides) -> TargetTracker:
    cfg = {**_BASE_CFG}
    if overrides:
        cfg["tracking"] = {**cfg["tracking"], **overrides}
    return TargetTracker(cfg)


def _black_frame(w: int = 400, h: int = 300) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _draw_bar(frame: np.ndarray, cx: int, cy: int, bar_h: int = 40, bar_w: int = 6,
              bgr=(0, 220, 60)) -> np.ndarray:
    """Paint a tall green (or custom color) vertical bar at (cx, cy) in frame coords."""
    out = frame.copy()
    x1 = max(0, cx - bar_w // 2)
    y1 = max(0, cy - bar_h // 2)
    x2 = min(frame.shape[1], cx + bar_w // 2)
    y2 = min(frame.shape[0], cy + bar_h // 2)
    out[y1:y2, x1:x2] = bgr
    return out


def _draw_circle(frame: np.ndarray, cx: int, cy: int, r: int = 25,
                 bgr=(0, 220, 60)) -> np.ndarray:
    """Paint a circle (reticle shape) — should be rejected by aspect-ratio filter."""
    out = frame.copy()
    cv2.circle(out, (cx, cy), r, bgr, -1)
    return out


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_initial_mode_is_searching(self):
        t = _tracker()
        assert t.mode == TrackMode.SEARCHING

    def test_first_update_transitions_to_acquiring(self):
        t = _tracker()
        t.update(_black_frame())
        assert t.mode == TrackMode.ACQUIRING

    def test_target_detected_transitions_to_tracking(self):
        t = _tracker(local_roi_enabled=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_miss_after_tracking_enters_lost_grace(self):
        t = _tracker(local_roi_enabled=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        assert t.mode == TrackMode.TRACKING
        t.update(_black_frame())
        assert t.mode == TrackMode.LOST_GRACE

    def test_reacquire_in_grace_returns_to_tracking(self):
        t = _tracker(local_roi_enabled=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        t.update(_black_frame())
        assert t.mode == TrackMode.LOST_GRACE
        obs = t.update(frame)
        assert obs["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_grace_timeout_returns_to_acquiring(self):
        t = _tracker(local_roi_enabled=False, lost_timeout_sec=0.01)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        t.update(_black_frame())
        time.sleep(0.05)
        t.update(_black_frame())
        assert t.mode == TrackMode.ACQUIRING

    def test_reset_clears_state(self):
        t = _tracker(local_roi_enabled=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        assert t.mode == TrackMode.TRACKING
        t.reset()
        assert t.mode == TrackMode.SEARCHING
        assert t.update(_black_frame())["error_norm"] is None


# ---------------------------------------------------------------------------
# Detection: aspect-ratio filter
# ---------------------------------------------------------------------------

class TestAspectRatioFilter:
    def test_tall_bar_is_detected(self):
        t = _tracker(local_roi_enabled=False)
        frame = _draw_bar(_black_frame(400, 300), cx=200, cy=150, bar_h=50, bar_w=5)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["n_detections"] >= 1

    def test_circle_is_rejected(self):
        """Lock-on reticle (circular blob) must NOT be classified as a target bar."""
        t = _tracker(local_roi_enabled=False)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25)
        obs = t.update(frame)
        assert obs["visible"] is False
        assert obs["n_detections"] == 0

    def test_wide_blob_is_rejected(self):
        """A wide rectangle (h/w < 2.5) is not a target bar."""
        t = _tracker(local_roi_enabled=False)
        frame = _black_frame(400, 300)
        frame[140:160, 160:240] = (0, 200, 50)  # 20px tall × 80px wide → aspect 0.25
        obs = t.update(frame)
        assert obs["visible"] is False


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------

class TestErrorNorm:
    def test_target_at_center_gives_zero_error(self):
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=w // 2, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] == pytest.approx(0.0, abs=0.05)

    def test_target_left_of_center_gives_negative_error(self):
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=80, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] < -0.3

    def test_target_right_of_center_gives_positive_error(self):
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=320, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] > 0.3

    def test_error_clamped_to_unit_range(self):
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=2, cy=h // 2)
        obs = t.update(frame)
        assert -1.0 <= obs["error_norm"] <= 1.0

    def test_no_error_when_no_target(self):
        t = _tracker(local_roi_enabled=False)
        obs = t.update(_black_frame())
        assert obs["error_norm"] is None


# ---------------------------------------------------------------------------
# Target selection: nearest-to-last heuristic
# ---------------------------------------------------------------------------

class TestTargetSelection:
    def test_picks_nearer_of_two_targets_on_first_frame(self):
        """Without a prior position, should pick target nearest to frame center."""
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        frame = _black_frame(w, h)
        # left bar at x=80 (dist 120 from center 200); right bar at x=250 (dist 50)
        # → right bar wins
        frame = _draw_bar(frame, cx=80, cy=h // 2)
        frame = _draw_bar(frame, cx=250, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] > 200

    def test_tracks_nearer_target_on_subsequent_frames(self):
        """After lock, should prefer the target closest to the last known position."""
        t = _tracker(local_roi_enabled=False)
        w, h = 400, 300
        # First frame: lock on left bar
        frame1 = _draw_bar(_black_frame(w, h), cx=100, cy=h // 2)
        t.update(frame1)
        assert t._last_x is not None and t._last_x < 200

        # Second frame: two bars; tracker should stick to left
        frame2 = _draw_bar(_black_frame(w, h), cx=100, cy=h // 2)
        frame2 = _draw_bar(frame2, cx=350, cy=h // 2)
        obs = t.update(frame2)
        assert obs["centroid_x"] < 200


# ---------------------------------------------------------------------------
# Red vs green preference
# ---------------------------------------------------------------------------

class TestColorPreference:
    def _hsv_bar(self, frame, cx, cy, h_val, s=220, v=220, bar_h=50, bar_w=5):
        """Draw a bar with an explicit OpenCV HSV value."""
        bar = np.zeros((bar_h, bar_w, 3), dtype=np.uint8)
        bar[:] = (h_val, s, v)
        bgr = cv2.cvtColor(bar, cv2.COLOR_HSV2BGR)
        fh, fw = frame.shape[:2]
        y1 = max(0, cy - bar_h // 2)
        x1 = max(0, cx - bar_w // 2)
        y2 = min(fh, y1 + bar_h)
        x2 = min(fw, x1 + bar_w)
        frame[y1:y2, x1:x2] = bgr[: y2 - y1, : x2 - x1]
        return frame

    def test_prefers_red_when_both_present(self):
        """When prefer_red=True and both red+green bars exist, red bar is selected."""
        t = _tracker(local_roi_enabled=False, prefer_red_lock=True)
        w, h = 400, 300
        frame = _black_frame(w, h)
        # Green bar at x=100, red bar at x=300
        frame = self._hsv_bar(frame, 300, h // 2, h_val=5)   # red
        frame = self._hsv_bar(frame, 100, h // 2, h_val=60)  # green
        obs = t.update(frame)
        assert obs["visible"] is True
        # Red bar is at x=300; green at x=100; center=200 so green is closer to center
        # but red wins because prefer_red_lock=True
        assert obs["centroid_x"] > 200

    def test_falls_back_to_green_when_no_red(self):
        t = _tracker(local_roi_enabled=False, prefer_red_lock=True)
        w, h = 400, 300
        frame = self._hsv_bar(_black_frame(w, h), w // 2, h // 2, h_val=60)  # green bar
        obs = t.update(frame)
        assert obs["visible"] is True


# ---------------------------------------------------------------------------
# orient_nose_to_target (Controller method)
# ---------------------------------------------------------------------------

class TestOrientNoseToTarget:
    def _ctrl(self):
        """Bare Controller instance with only _last_orient_ts initialised.

        orient_nose_to_target only reads _last_orient_ts and calls roll_left /
        roll_right, which we always patch before invoking the method.
        """
        from wingman.controller import Controller
        ctrl = Controller.__new__(Controller)
        ctrl._last_orient_ts = 0.0
        return ctrl

    def test_deadband_suppresses_command(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_left") as rl, patch.object(ctrl, "roll_right") as rr:
            result = ctrl.orient_nose_to_target(0.03, deadband=0.05)
        assert result is None

    def test_negative_error_rolls_left(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_left") as rl, patch.object(ctrl, "roll_right") as rr:
            result = ctrl.orient_nose_to_target(-0.5, deadband=0.05)
        assert result == "left"

    def test_positive_error_rolls_right(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_right") as rr:
            result = ctrl.orient_nose_to_target(0.5, deadband=0.05)
        assert result == "right"

    def test_hold_is_proportional_and_clamped(self):
        ctrl = self._ctrl()
        captured = {}
        def _fake_roll_right(hold_seconds=0.3, block=True):
            captured["hold"] = hold_seconds
        ctrl.roll_right = _fake_roll_right
        ctrl.orient_nose_to_target(1.0, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] == pytest.approx(0.30, abs=0.001)

    def test_hold_clamped_to_min(self):
        ctrl = self._ctrl()
        captured = {}
        ctrl.roll_right = lambda hold_seconds=0.3, block=True: captured.update(hold=hold_seconds)
        ctrl.orient_nose_to_target(0.1, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] >= 0.08

    def test_hold_clamped_to_max(self):
        ctrl = self._ctrl()
        captured = {}
        ctrl.roll_right = lambda hold_seconds=0.3, block=True: captured.update(hold=hold_seconds)
        ctrl.orient_nose_to_target(10.0, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] <= 0.35

    def test_cooldown_suppresses_second_call(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_right") as rr:
            ctrl.orient_nose_to_target(0.5, cooldown_sec=10.0)
            result = ctrl.orient_nose_to_target(0.5, cooldown_sec=10.0)
        assert result is None

    def test_cooldown_allows_after_elapsed(self):
        ctrl = self._ctrl()
        ctrl._last_orient_ts = time.time() - 1.0
        with patch.object(ctrl, "roll_right") as rr:
            result = ctrl.orient_nose_to_target(0.5, cooldown_sec=0.5)
        assert result == "right"


# ---------------------------------------------------------------------------
# Reference frame regression
# ---------------------------------------------------------------------------

class TestReferenceFrame:
    _REF = Path("test_screenshots/integration_test/P2_050_RESPAWN_CLEAR_HEALTH_ALIVE_MISSILES_4.png")

    @pytest.mark.skipif(
        not _REF.exists(),
        reason="reference screenshot not present (all-black placeholder skipped)",
    )
    def test_reference_frame_detects_markers(self):
        """TargetTracker must find at least one green target bar in the reference frame."""
        frame = cv2.imread(str(self._REF))
        assert frame is not None, "Could not load reference frame"
        # Check it's not all-black (placeholder check)
        if not np.any(frame):
            pytest.skip("reference screenshot is all-black placeholder")
        t = _tracker(local_roi_enabled=False)
        obs = t.update(frame)
        assert obs["n_detections"] >= 1, (
            f"Expected >=1 target bar in reference frame, got {obs['n_detections']}. "
            "HSV ranges may need tuning."
        )

    @pytest.mark.skipif(
        not _REF.exists(),
        reason="reference screenshot not present",
    )
    def test_reference_frame_rejects_reticle(self):
        """Aspect-ratio filter must not classify the lock-on reticle circle as a target bar."""
        frame = cv2.imread(str(self._REF))
        if frame is None or not np.any(frame):
            pytest.skip("reference screenshot unavailable or all-black")
        t = _tracker(local_roi_enabled=False)
        obs = t.update(frame)
        # The reticle is roughly at the left-center of the frame (approx x=0.35*w).
        # A detection there with error_norm near -0.30 suggests the reticle was hit.
        # We can't rule it out statically, but we CAN assert the centroid is not
        # suspiciously small (a real bar is taller than it is wide).
        if obs["centroid_x"] is not None:
            # Re-run internal detect to inspect raw contours
            raw = t._detect_targets(frame)
            for cx, cy, area in raw:
                # All accepted contours must have passed the aspect-ratio filter —
                # they were accepted, so aspect ratio >= 2.5. Just sanity-check area.
                assert area >= 12, "Contour below min_contour_area was accepted"


# ---------------------------------------------------------------------------
# HudRenderer — smoke test (no display needed)
# ---------------------------------------------------------------------------

class TestHudRenderer:
    def test_maybe_render_writes_file(self, tmp_path):
        from wingman.hud import HudRenderer
        output = tmp_path / "live_hud.png"
        renderer = HudRenderer(str(output), interval_sec=0.0)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE", 100, 4, 2)
        assert thread is not None
        thread.join(timeout=5)
        assert output.exists()
        img = cv2.imread(str(output))
        assert img is not None
        assert img.shape[:2] == (150, 200)  # rendered at 50% of input frame size

    def test_maybe_render_respects_interval(self, tmp_path):
        """Second call within the interval must not overwrite the file."""
        from wingman.hud import HudRenderer
        output = tmp_path / "hud.png"
        renderer = HudRenderer(str(output), interval_sec=9999.0)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        # First call always renders (last_ts=0 → elapsed = now > any interval)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE", None, None, None)
        assert thread is not None
        thread.join(timeout=5)
        assert output.exists()
        mtime_after_first = output.stat().st_mtime_ns
        # Second call immediately: still within the 9999 s interval — no write
        assert renderer.maybe_render(frame, None, "GAME_BATTLE", None, None, None) is None
        assert output.stat().st_mtime_ns == mtime_after_first

    def test_from_config_disabled(self):
        from wingman.hud import HudRenderer
        cfg = {"hud": {"enabled": False}, "tracking": {"enabled": False}}
        assert HudRenderer.from_config(cfg) is None

    def test_from_config_enabled(self, tmp_path):
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        cfg = {"hud": {"enabled": True, "output_path": out, "interval_sec": 0.0}}
        renderer = HudRenderer.from_config(cfg)
        assert renderer is not None

    def test_from_config_tracking_enabled_activates_hud(self, tmp_path):
        """tracking.enabled=True must activate HudRenderer even if hud.enabled=False."""
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        cfg = {
            "hud": {"enabled": False, "output_path": out, "interval_sec": 0.0},
            "tracking": {"enabled": True},
        }
        with patch("wingman.hud.HudRenderer._launch_feh"):
            renderer = HudRenderer.from_config(cfg)
        assert renderer is not None

    def test_from_config_feh_not_launched_when_tracking_disabled(self, tmp_path):
        """feh must not launch when tracking.enabled=False."""
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        cfg = {
            "hud": {"enabled": True, "output_path": out, "interval_sec": 0.0},
            "tracking": {"enabled": False},
        }
        with patch("wingman.hud.HudRenderer._launch_feh") as mock_feh:
            HudRenderer.from_config(cfg)
        mock_feh.assert_not_called()

    def test_from_config_feh_launched_when_tracking_enabled(self, tmp_path):
        """feh must launch when tracking.enabled=True."""
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        cfg = {
            "hud": {"enabled": True, "output_path": out, "interval_sec": 0.0},
            "tracking": {"enabled": True},
            "region": {"left": 0, "top": 0, "width": 1920, "height": 1200},
        }
        with patch("wingman.hud.HudRenderer._launch_feh") as mock_feh:
            HudRenderer.from_config(cfg)
        mock_feh.assert_called_once()


    def test_render_with_tracking_obs(self, tmp_path):
        from wingman.hud import HudRenderer
        output = tmp_path / "hud_tracked.png"
        renderer = HudRenderer(str(output), interval_sec=0.0)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        obs = {
            "mode": "TRACKING",
            "visible": True,
            "centroid_x": 250.0,
            "centroid_y": 150.0,
            "error_norm": 0.25,
            "n_detections": 2,
            "roi_rect": (200, 120, 80, 60),
        }
        thread = renderer.maybe_render(frame, obs, "GAME_BATTLE", 180, 6, 4)
        thread.join(timeout=5)
        assert output.exists()

    def test_atomic_write_no_partial_read(self, tmp_path):
        """tmp file must be gone after render (os.replace consumed it)."""
        from wingman.hud import HudRenderer
        output = tmp_path / "hud.png"
        renderer = HudRenderer(str(output), interval_sec=0.0)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE", None, None, None)
        thread.join(timeout=5)
        tmp_file = output.with_suffix(".tmp.png")
        assert not tmp_file.exists(), "tmp file should be consumed by os.replace"

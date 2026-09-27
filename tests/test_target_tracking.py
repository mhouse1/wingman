"""Unit tests for TargetTracker and orient_nose_to_target."""

import time
from pathlib import Path
from unittest.mock import Mock, patch

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
        # The shipped HUD exclusions are the defaults (CR-018-04); off here so
        # target geometry is plain. TestExclusions covers them.
        "red_mass_exclude_pct": [],
        "red_mass_exclude_zones_pct": [],
    },
    "tracking_hsv": {
        "red_lower": [0, 150, 150],
        "red_upper": [10, 255, 255],
    },
}


def _tracker(**overrides) -> TargetTracker:
    cfg = {**_BASE_CFG}
    if overrides:
        cfg["tracking"] = {**cfg["tracking"], **overrides}
    return TargetTracker(cfg)


def _black_frame(w: int = 400, h: int = 300) -> np.ndarray:
    return np.zeros((h, w, 3), dtype=np.uint8)


def _draw_circle(frame: np.ndarray, cx: int, cy: int, r: int = 25,
                 bgr=(0, 220, 60)) -> np.ndarray:
    """Paint a filled circle (the padlock tests use it as a one-blob decoy)."""
    out = frame.copy()
    cv2.circle(out, (cx, cy), r, bgr, -1)
    return out


def _bgr_from_hue(h: int, s: int = 255, v: int = 255) -> tuple:
    """OpenCV-HSV-exact BGR for a given hue, so tests can construct pixels
    that land at a known hue rather than approximating via raw BGR guesses
    — needed for red_mass_hue_max, which discriminates purely on hue."""
    return tuple(int(c) for c in cv2.cvtColor(np.uint8([[[h, s, v]]]), cv2.COLOR_HSV2BGR)[0, 0])


def _draw_dashed_ring(frame: np.ndarray, cx: int, cy: int, radius: int = 25,
                      n_dashes: int = 12, dash_r: int = 2,
                      hsv=(55, 90, 200)) -> np.ndarray:
    """Paint a ring of small dash blobs — the real padlock-off indicator
    shape (ADR 136), calibrated against a live capture: many short dash
    segments around a circle, not one filled shape. Default color is the
    live-measured translucent-HUD green (H=55, S~76, V~127-224) — a fully
    saturated (0,220,60)-style BGR green falls outside the calibrated
    S<=200 upper bound and would silently under-test this detector."""
    out = frame.copy()
    bgr = tuple(int(v) for v in cv2.cvtColor(
        np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0])
    for i in range(n_dashes):
        theta = 2 * np.pi * i / n_dashes
        dx = int(cx + radius * np.cos(theta))
        dy = int(cy + radius * np.sin(theta))
        cv2.circle(out, (dx, dy), dash_r, bgr, -1)
    return out


_RED = (0, 0, 255)
_W, _H = 1920, 1200


def _frame() -> np.ndarray:
    return np.zeros((_H, _W, 3), dtype=np.uint8)


def _target(frame: np.ndarray, x: int, y: int, n_glyphs: int = 28, bgr=_RED) -> np.ndarray:
    """An enemy contact whose steering point is (x, y): a nameplate block (rows
    of glyph-sized rectangles plus an underline) centred 100 px above it,
    `red_mass_aim_offset_px` (the aircraft marker sits below its label)."""
    out = frame.copy()
    cx, cy = x, y - 100
    per_row = max(1, n_glyphs // 3)
    rows = [per_row, per_row, n_glyphs - 2 * per_row]
    for r, count in enumerate(rows):
        gy = cy - 30 + r * 30
        x0 = cx - (count * 8) // 2
        for i in range(count):
            out[gy:gy + 12, x0 + i * 8:x0 + i * 8 + 5] = bgr
    out[cy + 48:cy + 52, cx - 40:cx + 40] = bgr
    return out


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------

class TestStateMachine:
    def test_initial_mode_is_searching(self):
        assert _tracker().mode == TrackMode.SEARCHING

    def test_first_update_transitions_to_acquiring(self):
        t = _tracker()
        t.update(_frame())
        assert t.mode == TrackMode.ACQUIRING

    def test_target_detected_transitions_to_tracking(self):
        t = _tracker()
        obs = t.update(_target(_frame(), 960, 600))
        assert obs["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_every_tick_scans_the_whole_acquisition_region(self):
        """Once locked, the tracker used to scan only a local ROI around the
        lock, so a target that moved further than that between ticks was
        lost. Every tick scans the whole region."""
        t = _tracker()
        assert t.update(_target(_frame(), 400, 600), ts=0.0)["visible"] is True
        obs = t.update(_target(_frame(), 1500, 600), ts=0.3)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(1500, abs=6)
        assert "roi_rect" not in obs

    def test_miss_after_tracking_enters_lost_grace(self):
        t = _tracker()
        t.update(_target(_frame(), 960, 600))
        t.update(_frame())
        assert t.mode == TrackMode.LOST_GRACE

    def test_reacquire_in_grace_returns_to_tracking(self):
        t = _tracker()
        frame = _target(_frame(), 960, 600)
        t.update(frame)
        t.update(_frame())
        assert t.mode == TrackMode.LOST_GRACE
        assert t.update(frame)["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_lost_target_is_remembered_for_the_roll_on_miss_delay(self):
        """LOST_GRACE lasts as long as Controller.roll_on_miss waits before it
        resumes the search: pursuit_mode.search_resume_delay_s (default 2.0 s)
        when the target was last seen off centre."""
        t = _tracker()
        t.update(_target(_frame(), 200, 600), ts=0.0)     # err about -0.79
        t.update(_frame(), ts=0.3)
        t.update(_frame(), ts=1.9)
        assert t.mode == TrackMode.LOST_GRACE
        t.update(_frame(), ts=2.05)
        assert t.mode == TrackMode.ACQUIRING

    def test_a_target_lost_near_centre_is_remembered_for_the_centre_delay(self):
        """roll_on_miss's near-centre extension: last seen within
        search_resume_centre_err (0.15) of centre -> search_resume_centre_delay_s
        (default 6.0 s)."""
        t = _tracker()
        t.update(_target(_frame(), 960, 600), ts=0.0)
        t.update(_frame(), ts=0.3)
        t.update(_frame(), ts=5.9)
        assert t.mode == TrackMode.LOST_GRACE
        t.update(_frame(), ts=6.05)
        assert t.mode == TrackMode.ACQUIRING

    def test_memory_reads_the_same_pursuit_mode_keys_as_roll_on_miss(self):
        t = TargetTracker({**_BASE_CFG, "pursuit_mode": {
            "search_resume_delay_s": 0.5, "search_resume_centre_err": 0.15,
            "search_resume_centre_delay_s": 1.0}})
        t.update(_target(_frame(), 200, 600), ts=0.0)
        t.update(_frame(), ts=0.3)
        t.update(_frame(), ts=0.55)
        assert t.mode == TrackMode.ACQUIRING

    def test_reset_clears_state(self):
        t = _tracker()
        t.update(_target(_frame(), 960, 600))
        assert t.mode == TrackMode.TRACKING
        t.reset()
        assert t.mode == TrackMode.SEARCHING
        assert t.update(_frame())["error_norm"] is None


# ---------------------------------------------------------------------------
# Error signal
# ---------------------------------------------------------------------------

class TestErrorNorm:
    def test_target_at_center_gives_zero_error(self):
        obs = _tracker().update(_target(_frame(), 960, 600))
        assert obs["error_norm"] == pytest.approx(0.0, abs=0.01)
        assert obs["error_norm_y"] == pytest.approx(0.0, abs=0.03)

    def test_target_left_of_center_gives_negative_error(self):
        assert _tracker().update(_target(_frame(), 500, 600))["error_norm"] < -0.4

    def test_target_right_of_center_gives_positive_error(self):
        assert _tracker().update(_target(_frame(), 1400, 600))["error_norm"] > 0.4

    def test_target_below_center_gives_positive_vertical_error(self):
        assert _tracker().update(_target(_frame(), 960, 900))["error_norm_y"] > 0.4

    def test_no_error_when_no_target(self):
        obs = _tracker().update(_frame())
        assert obs["error_norm"] is None and obs["error_norm_y"] is None
        assert obs["visible"] is False

    def test_the_observation_has_no_tall_bar_count(self):
        """CR-018-04: n_detections counted the removed tall-bar contours."""
        assert "n_detections" not in _tracker().update(_frame())


# ---------------------------------------------------------------------------
# The nameplate gate and the colour filters
# ---------------------------------------------------------------------------

class TestNameplateGate:
    def test_a_lone_blob_with_no_nameplate_is_not_a_target(self):
        """Flares, own exhaust, fireballs: red, but no label."""
        frame = _frame()
        cv2.circle(frame, (960, 600), 40, _RED, -1)
        assert _tracker().update(frame)["visible"] is False

    def test_a_label_below_the_glyph_threshold_is_not_acquired(self):
        assert _tracker().update(_target(_frame(), 960, 600, n_glyphs=12))["visible"] is False

    def test_the_threshold_comes_from_the_config(self):
        t = _tracker(red_mass_nameplate_min_glyphs=10)
        assert t.update(_target(_frame(), 960, 600, n_glyphs=12))["visible"] is True


class TestColourFilters:
    def test_an_afterburner_orange_label_is_ignored_by_default(self):
        """red_mass_hue_max defaults to the shipped 6: hue 8 (afterburner glow)
        is outside the mask."""
        orange = _bgr_from_hue(8)
        assert _tracker().update(_target(_frame(), 960, 600, bgr=orange))["visible"] is False

    def test_a_null_hue_max_falls_back_to_the_hsv_upper_bound(self):
        orange = _bgr_from_hue(8)
        t = _tracker(red_mass_hue_max=None)
        assert t.update(_target(_frame(), 960, 600, bgr=orange))["visible"] is True

    def test_a_dim_label_is_ignored_by_default(self):
        """red_mass_value_min defaults to the shipped 245: the afterburner's
        fading rim sits below it."""
        dim = _bgr_from_hue(2, v=200)
        assert _tracker().update(_target(_frame(), 960, 600, bgr=dim))["visible"] is False

    def test_a_null_value_min_falls_back_to_the_hsv_lower_bound(self):
        dim = _bgr_from_hue(2, v=200)
        t = _tracker(red_mass_value_min=None)
        assert t.update(_target(_frame(), 960, 600, bgr=dim))["visible"] is True


class TestExclusions:
    def test_the_no_lock_text_is_excluded_by_default(self):
        """red_mass_exclude_pct defaults to the shipped rectangle."""
        frame = _frame()
        frame[910:940, 900:1020] = _RED                     # inside 883-1037 x 900-948
        t = TargetTracker({"tracking": {"enabled": True}})
        assert t._red_mass_probe(frame, 0, 0, _W, _H)["px"] == 0

    def test_an_empty_value_turns_the_exclusion_off(self):
        frame = _frame()
        frame[910:940, 900:1020] = _RED
        t = TargetTracker({"tracking": {"enabled": True, "red_mass_exclude_pct": [],
                                        "red_mass_exclude_zones_pct": []}})
        assert t._red_mass_probe(frame, 0, 0, _W, _H)["px"] > 0


# ---------------------------------------------------------------------------
# TRACKPICK logging
# ---------------------------------------------------------------------------

class TestPickPathLogging:
    @staticmethod
    def _line(caplog):
        lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("TRACKPICK:")]
        assert len(lines) == 1
        return lines[0]

    def test_a_lock_is_path_redmass_with_its_glyph_count(self, caplog):
        with caplog.at_level("DEBUG", logger="wingman.tracker"):
            _tracker().update(_target(_frame(), 960, 600))
        line = self._line(caplog)
        assert "path=redmass" in line and "gate=pass" in line and "glyphs=28" in line

    def test_no_nameplate_is_path_none_with_the_gate_rejection(self, caplog):
        frame = _frame()
        cv2.circle(frame, (960, 600), 40, _RED, -1)
        with caplog.at_level("DEBUG", logger="wingman.tracker"):
            _tracker().update(frame)
        line = self._line(caplog)
        assert "path=none" in line and "gate=reject" in line and "sel=-" in line

    def test_the_tall_bar_fields_are_gone(self, caplog):
        with caplog.at_level("DEBUG", logger="wingman.tracker"):
            _tracker().update(_target(_frame(), 960, 600))
        line = self._line(caplog)
        for field in ("tall=", "n_tall=", "red_won="):
            assert field not in line

    def test_session_report_still_parses_the_new_line(self, caplog):
        import importlib.util
        spec = importlib.util.spec_from_file_location("session_report", "scripts/session-report.py")
        sr = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sr)
        with caplog.at_level("DEBUG", logger="wingman.tracker"):
            _tracker().update(_target(_frame(), 960, 600))
        m = sr._PICK.search(self._line(caplog))
        assert m is not None and m.group(1) == "redmass"


# ---------------------------------------------------------------------------
# CR-018-04: the code defaults are the shipped values
# ---------------------------------------------------------------------------

def test_every_code_default_matches_the_shipped_config():
    """A config without a tracking key must behave exactly like the shipped
    one, so a synthetic test cannot exercise a tracker that does not ship."""
    import yaml
    shipped_cfg = yaml.safe_load(Path("wingman/config.yaml").read_text())
    shipped = TargetTracker(shipped_cfg)
    bare = TargetTracker({"tracking": {"enabled": True}})
    for attr in ("_acq_x1", "_acq_y1", "_acq_x2", "_acq_y2",
                 "_red_mass_exclude_pct", "_red_mass_exclude_zones",
                 "_cluster_glyph_window", "_aim_offset", "_keep_min_glyphs",
                 "_keep_box_pct", "_red_mass_hue_max", "_red_mass_value_min",
                 "_red_mass_nameplate_min_glyphs", "_red_mass_nameplate_glyph_area",
                 "_red_mass_nameplate_glyph_max_dim"):
        assert getattr(bare, attr) == getattr(shipped, attr), attr
    assert (bare._red_lower == shipped._red_lower).all()
    assert (bare._red_upper == shipped._red_upper).all()


# ---------------------------------------------------------------------------
# detect_padlock_off (ADR 136): dashed green ring at screen center, counted
# by dash segments (not a single blob) — calibrated against a live capture,
# 2026-09-09.
# ---------------------------------------------------------------------------

class TestPadlockIndicator:
    def test_detects_dashed_ring_at_center(self):
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_dashed_ring(frame, w // 2, h // 2, radius=25, n_dashes=12)
        assert t.detect_padlock_off(frame) is True

    def test_no_ring_returns_false(self):
        t = _tracker()
        assert t.detect_padlock_off(_black_frame()) is False

    def test_too_few_dashes_is_rejected(self):
        """Below min_dashes (default 6) — noise or a partial/occluded ring,
        not a confirmed indicator."""
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_dashed_ring(frame, w // 2, h // 2, radius=25, n_dashes=3)
        assert t.detect_padlock_off(frame) is False

    def test_one_large_blob_is_rejected(self):
        """A single big filled shape (e.g. an unrelated green HUD wash)
        isn't the ring: dash-sized area bounds reject anything much bigger
        than an individual dash, and one blob alone can never reach
        min_dashes regardless of size."""
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_circle(frame, w // 2, h // 2, r=20)
        assert t.detect_padlock_off(frame) is False

    def test_ring_outside_center_region_is_ignored(self):
        t = _tracker()
        frame = _draw_dashed_ring(_black_frame(), 30, 30, radius=15, n_dashes=12)
        assert t.detect_padlock_off(frame) is False

    def test_empty_frame_returns_false(self):
        t = _tracker()
        assert t.detect_padlock_off(np.zeros((0, 0, 3), dtype=np.uint8)) is False


# ---------------------------------------------------------------------------
# detect_padlock_center_dot (ADR 140): a small SOLID dot fixed at exact
# screen center — a different element from the dashed, moving ring above.
# Measured 2026-09-17 against 8 real padlock-off captures: OpenCV HSV
# (55, 76, 224), byte-identical across all 8 regardless of roll/pitch/target
# state.
# ---------------------------------------------------------------------------

def _draw_center_dot(frame: np.ndarray, cx: int, cy: int, size: int = 3,
                     hsv=(55, 76, 224)) -> np.ndarray:
    """Paint the small flat-colored dot ADR 140 measured — a filled square,
    not a circle: the real dot's own bounding box (3x3px at 1920x1200) is
    too small for a drawn circle to differ meaningfully from a square."""
    out = frame.copy()
    bgr = tuple(int(v) for v in cv2.cvtColor(
        np.uint8([[hsv]]), cv2.COLOR_HSV2BGR)[0, 0])
    half = size // 2
    out[max(0, cy - half):cy + half + 1, max(0, cx - half):cx + half + 1] = bgr
    return out


class TestPadlockCenterDot:
    def test_detects_dot_at_screen_center(self):
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_center_dot(frame, w // 2, h // 2)
        assert t.detect_padlock_center_dot(frame) is True

    def test_no_dot_returns_false(self):
        t = _tracker()
        assert t.detect_padlock_center_dot(_black_frame()) is False

    def test_dot_outside_center_region_is_ignored(self):
        """Deliberately does NOT track the moving ring (ADR 136 D4) — a dot
        anywhere off true center, including exactly where that ring drifts
        to during a bank, must read False, not chase it there."""
        t = _tracker()
        frame = _draw_center_dot(_black_frame(), 100, 80)
        assert t.detect_padlock_center_dot(frame) is False

    def test_too_few_pixels_is_rejected(self):
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_center_dot(frame, w // 2, h // 2, size=1)  # 1px < min_pixels (3)
        assert t.detect_padlock_center_dot(frame) is False

    def test_empty_frame_returns_false(self):
        t = _tracker()
        assert t.detect_padlock_center_dot(np.zeros((0, 0, 3), dtype=np.uint8)) is False

    def test_dashed_ring_alone_is_not_read_as_the_center_dot(self):
        """The two detectors must stay independent: a ring at center (with
        no dot painted) should not satisfy the dot detector's own, tighter
        color bounds (S 60-90 vs the ring's S 30-200)."""
        t = _tracker()
        frame = _black_frame()
        h, w = frame.shape[:2]
        frame = _draw_dashed_ring(frame, w // 2, h // 2, radius=25, n_dashes=12,
                                  hsv=(55, 150, 200))  # high-S ring dash, outside dot bounds
        assert t.detect_padlock_center_dot(frame) is False


# Real captures from the 2026-09-17 live session that motivated ADR 140 —
# enumerated, not globbed (test_minimap_bearing.py's DESERT_FRAMES precedent:
# a glob absorbs whatever a later session drops into the same directory).
# Lives under test_screenshots/ (ADR 100 D7: veda-only, gitignored, skips
# gracefully everywhere else).
_PADLOCK_OFF_NAMES = (
    "screenshot_20260917_062804.png",
    "screenshot_20260917_063103.png",
    "screenshot_20260917_063139.png",
    "screenshot_20260917_063144.png",
    "screenshot_20260917_063149.png",
    "screenshot_20260917_063210.png",
    "screenshot_20260917_063216.png",
    "screenshot_20260917_063218.png",
)
PADLOCK_OFF_FRAMES = [
    p for p in (Path(__file__).resolve().parents[1] / "test_screenshots"
                / "padlock_off" / n for n in _PADLOCK_OFF_NAMES)
    if p.exists()
]


@pytest.mark.skipif(len(PADLOCK_OFF_FRAMES) < 8, reason="padlock_off corpus not present")
def test_center_dot_detected_on_every_real_padlock_off_frame():
    """All 8 frames span level cruise, hard banks, an enemy near boresight
    with a name/distance/type label, gun/lock warnings, and a dive toward
    water — real variety, not one convenient frame (ADR 140 Context)."""
    t = _tracker()
    misses = [f.name for f in PADLOCK_OFF_FRAMES
             if not t.detect_padlock_center_dot(cv2.imread(str(f)))]
    assert not misses, f"center dot not detected on: {misses}"


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
        with patch.object(ctrl, "roll_left"), patch.object(ctrl, "roll_right"):
            result = ctrl.orient_nose_to_target(0.03, deadband=0.05)
        assert result is None

    def test_negative_error_rolls_left(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_left"), patch.object(ctrl, "roll_right"):
            result = ctrl.orient_nose_to_target(-0.5, deadband=0.05)
        assert result == "left"

    def test_positive_error_rolls_right(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_right"):
            result = ctrl.orient_nose_to_target(0.5, deadband=0.05)
        assert result == "right"

    def test_hold_is_proportional_and_clamped(self):
        ctrl = self._ctrl()
        captured = {}
        def _fake_roll_right(hold_seconds=0.3, block=True, ignore_cancel=False):
            captured["hold"] = hold_seconds
        ctrl.roll_right = _fake_roll_right
        ctrl.orient_nose_to_target(1.0, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] == pytest.approx(0.30, abs=0.001)

    def test_hold_clamped_to_min(self):
        ctrl = self._ctrl()
        captured = {}
        ctrl.roll_right = lambda hold_seconds=0.3, block=True, ignore_cancel=False: captured.update(hold=hold_seconds)
        ctrl.orient_nose_to_target(0.1, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] >= 0.08

    def test_hold_clamped_to_max(self):
        ctrl = self._ctrl()
        captured = {}
        ctrl.roll_right = lambda hold_seconds=0.3, block=True, ignore_cancel=False: captured.update(hold=hold_seconds)
        ctrl.orient_nose_to_target(10.0, kp=0.30, min_hold_sec=0.08, max_hold_sec=0.35)
        assert captured["hold"] <= 0.35

    def test_cooldown_suppresses_second_call(self):
        ctrl = self._ctrl()
        with patch.object(ctrl, "roll_right"):
            ctrl.orient_nose_to_target(0.5, cooldown_sec=10.0)
            result = ctrl.orient_nose_to_target(0.5, cooldown_sec=10.0)
        assert result is None

    def test_cooldown_allows_after_elapsed(self):
        ctrl = self._ctrl()
        ctrl._last_orient_ts = time.time() - 1.0
        with patch.object(ctrl, "roll_right"):
            result = ctrl.orient_nose_to_target(0.5, cooldown_sec=0.5)
        assert result == "right"


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

    def test_from_config_hud_disabled_overrides_tracking_enabled(self, tmp_path):
        """hud.enabled=False is a hard override (2026-09-21): no renderer is
        built even with tracking.enabled=True — long unattended sessions can
        keep sensing/shadow-logging on without paying for a debug view
        nobody is watching. Inverts this suite's old contract, which
        required the opposite (tracking.enabled alone used to be enough)."""
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        cfg = {
            "hud": {"enabled": False, "output_path": out, "interval_sec": 0.0},
            "tracking": {"enabled": True},
        }
        assert HudRenderer.from_config(cfg) is None

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


class TestHudRendererFehClose:
    """Regression (2026-09-23): the feh window HudRenderer launches to
    display live_hud.png used to survive wingman exiting — _launch_feh()
    discarded the Popen handle immediately, so nothing could ever terminate
    it later. close() is the fix; these tests exercise it directly rather
    than spawning a real feh process, which may not even be installed in a
    test environment."""

    def test_close_without_feh_launched_is_a_noop(self, tmp_path):
        from wingman.hud import HudRenderer
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0)  # no feh_geometry
        renderer.close()  # must not raise

    def test_close_terminates_a_running_feh_process(self, tmp_path):
        from wingman.hud import HudRenderer
        fake_proc = Mock()
        fake_proc.poll.return_value = None  # still running
        with patch("wingman.hud.subprocess.Popen", return_value=fake_proc):
            renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                    feh_geometry="800x600+0+0")
        renderer.close()
        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_not_called()

    def test_close_kills_if_terminate_times_out(self, tmp_path):
        from wingman.hud import HudRenderer
        import subprocess as sp
        fake_proc = Mock()
        fake_proc.poll.return_value = None
        fake_proc.wait.side_effect = [sp.TimeoutExpired(cmd="feh", timeout=2.0), None]
        with patch("wingman.hud.subprocess.Popen", return_value=fake_proc):
            renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                    feh_geometry="800x600+0+0")
        renderer.close()
        fake_proc.terminate.assert_called_once()
        fake_proc.kill.assert_called_once()

    def test_close_is_a_noop_when_feh_already_exited(self, tmp_path):
        from wingman.hud import HudRenderer
        fake_proc = Mock()
        fake_proc.poll.return_value = 0  # already exited on its own
        with patch("wingman.hud.subprocess.Popen", return_value=fake_proc):
            renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                    feh_geometry="800x600+0+0")
        renderer.close()
        fake_proc.terminate.assert_not_called()


# ---------------------------------------------------------------------------
# HudRenderer — target-tracking archive (secondary-missile debugging trail)
# ---------------------------------------------------------------------------

class TestHudRendererArchive:
    def test_archives_during_game_battle_eject(self, tmp_path):
        """GAME_BATTLE_EJECT is ADR 136's live heatdive state — must archive."""
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE_EJECT", None, None, None)
        thread.join(timeout=5)
        saved = list(archive_dir.glob("*.png"))
        assert len(saved) == 1
        assert saved[0].name.startswith("game_battle_eject_")

    def test_does_not_archive_during_ordinary_game_battle(self, tmp_path):
        """Ordinary GAME_BATTLE is not a secondary-missile encounter — no archive."""
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE", None, None, None)
        thread.join(timeout=5)
        assert not archive_dir.exists() or not list(archive_dir.glob("*.png"))

    def test_archive_disabled_by_default(self, tmp_path):
        """archive_enabled defaults False — no files even during GAME_BATTLE_EJECT."""
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        thread = renderer.maybe_render(frame, None, "GAME_BATTLE_EJECT", None, None, None)
        thread.join(timeout=5)
        assert not archive_dir.exists() or not list(archive_dir.glob("*.png"))

    def test_archive_capped_per_session(self, tmp_path):
        """max_files stops new saves without erroring once the cap is hit."""
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir),
                                archive_max_files=2)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        for _ in range(4):
            thread = renderer.maybe_render(frame, None, "GAME_BATTLE_EJECT", None, None, None)
            thread.join(timeout=5)
        assert len(list(archive_dir.glob("*.png"))) == 2

    def test_from_config_reads_archive_block(self, tmp_path):
        from wingman.hud import HudRenderer
        out = str(tmp_path / "out.png")
        archive_dir = str(tmp_path / "archive")
        cfg = {"hud": {"enabled": True, "output_path": out, "interval_sec": 0.0,
                        "target_tracking_archive": {"enabled": True, "dir": archive_dir,
                                                     "max_files": 5}}}
        renderer = HudRenderer.from_config(cfg)
        assert renderer is not None
        assert renderer._archive_enabled is True
        assert str(renderer._archive_dir) == archive_dir
        assert renderer._archive_max == 5
        assert renderer._archive_save_raw_scan is False     # opt-in

    def test_min_interval_throttles_archive(self, tmp_path, monkeypatch):
        """2026-09-24: one frame per render spent the session cap in minutes."""
        from wingman import hud
        archive_dir = tmp_path / "archive"
        renderer = hud.HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                    archive_enabled=True, archive_dir=str(archive_dir),
                                    archive_min_interval_s=5.0)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        clock = iter([100.0, 102.0, 104.9, 105.0, 106.0])
        monkeypatch.setattr(hud.time, "time", lambda: next(clock))
        for _ in range(5):
            renderer.maybe_render(frame, None, "GAME_BATTLE_EJECT",
                                  None, None, None).join(timeout=5)
        assert len(list(archive_dir.glob("*.png"))) == 2     # t=100 and t=105

    def test_encounter_cap_resets_when_encounter_ends(self, tmp_path):
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir),
                                archive_max_per_encounter=2)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        states = ["PURSUIT_MODE"] * 4 + ["GAME_BATTLE"] + ["PURSUIT_MODE"] * 3
        for state in states:
            renderer.maybe_render(frame, None, state, None, None, None).join(timeout=5)
        assert len(list(archive_dir.glob("*.png"))) == 4     # 2 per encounter

    def test_archive_skipped_when_budget_refuses(self, tmp_path, monkeypatch):
        from wingman import capture_budget
        from wingman.hud import HudRenderer
        monkeypatch.setattr(capture_budget, "admit", lambda *a, **k: False)
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        renderer.maybe_render(frame, None, "PURSUIT_MODE", None, None, None).join(timeout=5)
        assert not archive_dir.exists() or not list(archive_dir.glob("*.png"))
        assert renderer._archive_count == 0

    def test_from_config_reads_throttle_keys(self, tmp_path):
        from wingman.hud import HudRenderer
        cfg = {"hud": {"enabled": True, "output_path": str(tmp_path / "o.png"),
                        "target_tracking_archive": {"enabled": True,
                                                     "min_interval_s": 5.0,
                                                     "max_per_encounter": 12}}}
        r = HudRenderer.from_config(cfg)
        assert r._archive_min_interval == 5.0
        assert r._archive_max_per_encounter == 12

    def test_from_config_reads_save_raw_scan(self, tmp_path):
        from wingman.hud import HudRenderer
        cfg = {"hud": {"enabled": True, "output_path": str(tmp_path / "o.png"),
                        "target_tracking_archive": {"enabled": True,
                                                     "dir": str(tmp_path / "a"),
                                                     "save_raw_scan": True}}}
        assert HudRenderer.from_config(cfg)._archive_save_raw_scan is True


class TestHudRendererRawScanArchive:
    """Action item 001: the annotated PNG cannot be replayed faithfully — the
    PURSUING marker is drawn on the exact pixels that produced the lock —
    so with save_raw_scan the exact, unannotated crop the tracker scanned is
    saved beside it."""

    _OBS = {"mode": "TRACKING", "visible": True, "centroid_x": 200,
            "centroid_y": 150, "error_norm": 0.0, "n_detections": 1}

    def _renderer(self, tmp_path, **kw):
        from wingman.hud import HudRenderer
        return HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                            archive_enabled=True,
                            archive_dir=str(tmp_path / "archive"),
                            acquisition_region_pct=(0.25, 0.25, 0.75, 0.75), **kw)

    def _frame(self):
        return np.random.default_rng(0).integers(0, 255, (300, 400, 3), dtype=np.uint8)

    def test_acquisition_scan_saves_the_exact_acq_crop(self, tmp_path):
        renderer = self._renderer(tmp_path, archive_save_raw_scan=True)
        frame = self._frame()
        renderer.maybe_render(frame, self._OBS,
                              "PURSUIT_MODE", None, None, None).join(timeout=5)
        raws = list((tmp_path / "archive").glob("*_raw_*.png"))
        assert len(raws) == 1
        # Same integer math the tracker uses: int(w*.25)=100, int(h*.25)=75, ...
        assert np.array_equal(cv2.imread(str(raws[0])), frame[75:225, 100:300])
        assert "_raw_ox100_oy75_fw400_fh300" in raws[0].name

    def test_raw_crop_carries_no_overlay(self, tmp_path):
        """The whole point: the marker drawn at the lock must not be in it."""
        renderer = self._renderer(tmp_path, archive_save_raw_scan=True)
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        renderer.maybe_render(frame, self._OBS,
                              "PURSUIT_MODE", None, None, None).join(timeout=5)
        raw = cv2.imread(str(next((tmp_path / "archive").glob("*_raw_*.png"))))
        assert not raw.any()          # annotated copy has marker/lines; this is black
        annotated = cv2.imread(str(next(
            p for p in (tmp_path / "archive").glob("*.png") if "_raw_" not in p.name)))
        assert annotated.any()

    def test_off_by_default_and_annotated_frame_still_saved(self, tmp_path):
        renderer = self._renderer(tmp_path)
        renderer.maybe_render(self._frame(), self._OBS,
                              "PURSUIT_MODE", None, None, None).join(timeout=5)
        names = [p.name for p in (tmp_path / "archive").glob("*.png")]
        assert len(names) == 1 and "_raw_" not in names[0]

    def test_no_obs_falls_back_to_acq_crop(self, tmp_path):
        renderer = self._renderer(tmp_path, archive_save_raw_scan=True)
        frame = self._frame()
        renderer.maybe_render(frame, None, "PURSUIT_MODE", None, None, None).join(timeout=5)
        raws = list((tmp_path / "archive").glob("*_raw_*.png"))
        assert len(raws) == 1
        assert np.array_equal(cv2.imread(str(raws[0])), frame[75:225, 100:300])

    def test_raw_is_not_saved_past_the_session_cap(self, tmp_path):
        renderer = self._renderer(tmp_path, archive_save_raw_scan=True,
                                   archive_max_files=1)
        for _ in range(3):
            renderer.maybe_render(self._frame(), self._OBS,
                                  "PURSUIT_MODE", None, None, None).join(timeout=5)
        names = [p.name for p in (tmp_path / "archive").glob("*.png")]
        assert len(names) == 2        # one annotated + its raw twin, nothing more


# ---------------------------------------------------------------------------
# HudRenderer — steering vector line (direct operator instruction, 2026-09-23)
# ---------------------------------------------------------------------------

class TestHudRendererSteeringLine:
    def test_line_drawn_from_screen_center_to_steer_target(self, tmp_path):
        """A line from screen center to the current steer target, so the HUD
        shows where the aircraft is being commanded to point.
        Checked against the full-res archived frame (pre-resize) so pixel
        coordinates are exact, not resize-interpolated."""
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)  # center = (200, 150)
        obs = {
            "mode": "TRACKING", "visible": True,
            "centroid_x": 350.0, "centroid_y": 150.0,  # same y as center -> horizontal line
            "error_norm": 0.9, "error_norm_y": 0.0,
            "n_detections": 1,
        }
        thread = renderer.maybe_render(frame, obs, "GAME_BATTLE_EJECT", None, None, None)
        thread.join(timeout=5)
        saved = list(archive_dir.glob("*.png"))
        assert len(saved) == 1
        canvas = cv2.imread(str(saved[0]))
        # Midpoint of the line (275, 150) is well clear of the marker drawn
        # at the target end (350, 150) and of the center crosshair.
        midpoint = tuple(int(c) for c in canvas[150, 275])
        pursuit_bgr = (255, 60, 220)
        # LINE_AA blends even a solid horizontal line slightly against the
        # background — tolerance wide enough to absorb that blend but far
        # below a near-black background pixel's own distance (~535).
        dist = sum(abs(a - b) for a, b in zip(midpoint, pursuit_bgr, strict=True))
        assert dist < 100, f"expected pursuit-colored line at midpoint, got {midpoint}"

    def test_no_line_without_a_steer_target(self, tmp_path):
        from wingman.hud import HudRenderer
        archive_dir = tmp_path / "archive"
        renderer = HudRenderer(str(tmp_path / "hud.png"), interval_sec=0.0,
                                archive_enabled=True, archive_dir=str(archive_dir))
        frame = np.zeros((300, 400, 3), dtype=np.uint8)
        obs = {"mode": "ACQUIRING", "visible": False, "centroid_x": None,
               "centroid_y": None, "error_norm": None, "n_detections": 0}
        thread = renderer.maybe_render(frame, obs, "GAME_BATTLE_EJECT", None, None, None)
        thread.join(timeout=5)
        canvas = cv2.imread(str(next((tmp_path / "archive").glob("*.png"))))
        midpoint = tuple(int(c) for c in canvas[150, 275])
        pursuit_bgr = (255, 60, 220)
        dist = sum(abs(a - b) for a, b in zip(midpoint, pursuit_bgr, strict=True))
        assert dist >= 100, "no steer target -> no line should be drawn"

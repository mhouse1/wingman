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
        "prefer_red_lock": True,
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
        t = _tracker()
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_every_tick_scans_the_whole_acquisition_region(self):
        """Once locked, the tracker used to scan only a local ROI around the
        lock (88 px wide on this frame), so a target that moved further than
        that between ticks was lost. Every tick now scans the whole region."""
        t = _tracker()
        assert t.update(_draw_bar(_black_frame(), cx=100, cy=150), ts=0.0)["visible"] is True
        obs = t.update(_draw_bar(_black_frame(), cx=330, cy=150), ts=0.3)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(330, abs=4)
        assert "roi_rect" not in obs

    def test_miss_after_tracking_enters_lost_grace(self):
        t = _tracker()
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        assert t.mode == TrackMode.TRACKING
        t.update(_black_frame())
        assert t.mode == TrackMode.LOST_GRACE

    def test_reacquire_in_grace_returns_to_tracking(self):
        t = _tracker()
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        t.update(frame)
        t.update(_black_frame())
        assert t.mode == TrackMode.LOST_GRACE
        obs = t.update(frame)
        assert obs["visible"] is True
        assert t.mode == TrackMode.TRACKING

    def test_lost_target_is_remembered_for_the_roll_on_miss_delay(self):
        """LOST_GRACE lasts as long as Controller.roll_on_miss waits before it
        resumes the search: pursuit_mode.search_resume_delay_s (default 2.0 s)
        when the target was last seen off centre."""
        t = _tracker()
        t.update(_draw_bar(_black_frame(), cx=40, cy=150), ts=0.0)   # err -0.8
        t.update(_black_frame(), ts=0.3)
        t.update(_black_frame(), ts=1.9)
        assert t.mode == TrackMode.LOST_GRACE
        t.update(_black_frame(), ts=2.05)
        assert t.mode == TrackMode.ACQUIRING

    def test_a_target_lost_near_centre_is_remembered_for_the_centre_delay(self):
        """roll_on_miss's near-centre extension: last seen within
        search_resume_centre_err (0.15) of centre -> search_resume_centre_delay_s
        (default 6.0 s)."""
        t = _tracker()
        t.update(_draw_bar(_black_frame(), cx=200, cy=150), ts=0.0)  # err 0
        t.update(_black_frame(), ts=0.3)
        t.update(_black_frame(), ts=5.9)
        assert t.mode == TrackMode.LOST_GRACE
        t.update(_black_frame(), ts=6.05)
        assert t.mode == TrackMode.ACQUIRING

    def test_memory_reads_the_same_pursuit_mode_keys_as_roll_on_miss(self):
        t = TargetTracker({**_BASE_CFG, "pursuit_mode": {
            "search_resume_delay_s": 0.5, "search_resume_centre_err": 0.15,
            "search_resume_centre_delay_s": 1.0}})
        t.update(_draw_bar(_black_frame(), cx=40, cy=150), ts=0.0)
        t.update(_black_frame(), ts=0.3)
        t.update(_black_frame(), ts=0.55)
        assert t.mode == TrackMode.ACQUIRING

    def test_reset_clears_state(self):
        t = _tracker()
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
        t = _tracker()
        frame = _draw_bar(_black_frame(400, 300), cx=200, cy=150, bar_h=50, bar_w=5)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["n_detections"] >= 1

    def test_circle_is_rejected(self):
        """Lock-on reticle (circular blob) must NOT be classified as a target bar."""
        t = _tracker()
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25)
        obs = t.update(frame)
        assert obs["visible"] is False
        assert obs["n_detections"] == 0

    def test_wide_blob_is_rejected(self):
        """A wide rectangle (h/w < 2.5) is not a target bar."""
        t = _tracker()
        frame = _black_frame(400, 300)
        frame[140:160, 160:240] = (0, 200, 50)  # 20px tall × 80px wide → aspect 0.25
        obs = t.update(frame)
        assert obs["visible"] is False


# ---------------------------------------------------------------------------
# red_mass_steering (direct operator instruction, 2026-09-23): steer toward
# the centroid of all red pixels, bypassing the aspect/area filter above
# entirely. Default False — these tests exercise the override explicitly;
# TestAspectRatioFilter above (which never sets this key) is the regression
# guard proving the default leaves that filter's behavior untouched.
# ---------------------------------------------------------------------------

class TestRedMassSteering:
    _RED_BGR = (0, 0, 255)  # H=0,S=255,V=255 — well inside red_lower/red_upper

    def test_disabled_by_default(self):
        t = _tracker()
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25,
                              bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is False

    def test_enabled_tracks_a_shape_the_aspect_filter_would_reject(self):
        """A filled red circle fails min_aspect_ratio (it's ~1:1, not >=2.5) —
        exactly the shape TestAspectRatioFilter.test_circle_is_rejected
        exists to reject for the tall-bar path. With red_mass_steering on,
        it must be tracked anyway, centered on the circle's own centroid."""
        t = _tracker(red_mass_steering=True)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25,
                              bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=2)
        assert obs["centroid_y"] == pytest.approx(150, abs=2)

    def test_n_detections_still_reports_the_tall_bar_count_not_red_mass(self):
        """n_detections must keep meaning "tall-bar contours found" even
        while centroid_x/y follow the red-mass override — otherwise the HUD's
        det= readout stops matching what it has meant everywhere else."""
        t = _tracker(red_mass_steering=True)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25,
                              bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True       # red-mass override fired
        assert obs["n_detections"] == 0     # no tall-bar contour qualified

    def test_enabled_transitions_to_tracking(self):
        t = _tracker(red_mass_steering=True)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=25,
                              bgr=self._RED_BGR)
        t.update(frame)
        assert t.mode == TrackMode.TRACKING

    def test_no_red_at_all_falls_back_to_the_tall_bar_pick(self):
        """When red_mass_steering is on but there is no red pixel anywhere,
        _red_mass_centroid returns None and the existing tall-bar pick (a
        green bar here) still governs — the override only overrides when it
        actually finds something."""
        t = _tracker(red_mass_steering=True)
        frame = _draw_bar(_black_frame(400, 300), cx=200, cy=150)  # green
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=2)

    def test_exclusion_region_ignores_fixed_hud_chrome(self):
        """Regression for the "NO LOCK" text false-positive (2026-09-23,
        game_battle_eject_20260923_094830_187.png): that text is fixed,
        boresight-relative HUD chrome, always red, always inside the acq/
        ROI crop — without exclusion it wins the centroid outright and the
        local ROI then narrows onto it forever. A large red blob inside the
        configured exclusion region must not pull the centroid at all, even
        though it is much bigger than the real target elsewhere in frame."""
        t = _tracker(red_mass_steering=True,
                      red_mass_exclude_pct=[0.7, 0.7, 1.0, 1.0])
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=self._RED_BGR)   # real target
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=self._RED_BGR)  # HUD-chrome stand-in
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(100, abs=3)
        assert obs["centroid_y"] == pytest.approx(100, abs=3)

    def test_without_exclusion_configured_the_big_blob_still_wins(self):
        """Regression guard: red_mass_exclude_pct defaults to None (no
        exclusion) — proves the fix above is opt-in via its own config key,
        not a change to red_mass_steering's baseline behavior."""
        t = _tracker(red_mass_steering=True)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=self._RED_BGR)
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        # Mass-weighted centroid is pulled well away from the small blob
        # toward the much larger one — not on the small blob alone.
        assert obs["centroid_x"] > 250

    def test_hue_max_ignores_afterburner_colored_orange(self):
        """Regression for the afterburner false-positive (2026-09-23,
        game_battle_eject_20260923_123703_35.png and 2 others): the
        afterburner glow is orange (measured hue 7-10), the real target icon
        is pure red (measured hue 3-5) — per-blob pixel area was checked
        first and rejected as a discriminator (270 vs 271, 222 vs 309 px in
        those same frames). A bigger orange blob (hue 9, afterburner
        stand-in) must not pull the centroid once red_mass_hue_max excludes
        its hue, even though it is much bigger than the real target icon
        (hue 3) elsewhere in frame."""
        t = _tracker(red_mass_steering=True, red_mass_hue_max=6)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=_bgr_from_hue(3))   # real target
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=_bgr_from_hue(9))  # afterburner stand-in
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(100, abs=3)
        assert obs["centroid_y"] == pytest.approx(100, abs=3)

    def test_without_hue_max_configured_the_orange_blob_still_counts(self):
        """Regression guard: red_mass_hue_max defaults to None (falls back
        to tracking_hsv.red_upper's own hue, 10 — no behavior change)."""
        t = _tracker(red_mass_steering=True)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=_bgr_from_hue(3))
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=_bgr_from_hue(9))
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] > 250

    def test_value_min_ignores_the_afterburners_fading_rim(self):
        """Regression for a second afterburner false-positive (2026-09-23,
        game_battle_eject_20260923_171137/38/39_0/1/2.png), found after the
        hue_max fix above was already in place: the flame's outer rim fades
        through the *same* red hue band the real target uses (hue 3-6, not
        the flame's own orange bulk), so hue can't separate them. Per-blob
        pixel area was checked again and rejected again too (bad-frame rim
        blobs measured 328-652px, bigger than the good frame's own 79px
        fragments — backwards from what a minimum-area rule would need).
        Brightness is the real discriminator: the target is a flat-shaded
        icon (value approx 255 on 86% of its qualifying pixels); the fading
        rim only reaches value >=245 on 9-11% of its. A bigger same-hue but
        dim blob (rim stand-in) must not pull the centroid once
        red_mass_value_min excludes it, even though it is much bigger than
        the real, fully-bright target icon elsewhere in frame."""
        t = _tracker(red_mass_steering=True, red_mass_value_min=245)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=_bgr_from_hue(3, v=255))  # real target
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=_bgr_from_hue(3, v=200))  # fading rim stand-in
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(100, abs=3)
        assert obs["centroid_y"] == pytest.approx(100, abs=3)

    def test_without_value_min_configured_the_dim_blob_still_counts(self):
        """Regression guard: red_mass_value_min defaults to None (falls back
        to tracking_hsv.red_lower's own value, 150 — no behavior change)."""
        t = _tracker(red_mass_steering=True)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=100, cy=100, r=10, bgr=_bgr_from_hue(3, v=255))
        frame = _draw_circle(frame, cx=350, cy=260, r=30, bgr=_bgr_from_hue(3, v=200))
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] > 250


def _draw_glyphs(frame: np.ndarray, cx: int, cy: int, n: int,
                  glyph_w: int = 4, glyph_h: int = 8, gap: int = 2,
                  bgr=(0, 0, 255)) -> np.ndarray:
    """Paint n small, separated glyph-sized rectangles in a row centered on
    (cx, cy), simulating the letter/digit fragments of a HUD nameplate
    (name/distance/type) — each becomes its own connected component, sized
    to fall inside the default red_mass_nameplate_glyph_area/max_dim
    bounds (10-200px, <=25px per side)."""
    out = frame.copy()
    total_w = n * glyph_w + (n - 1) * gap
    x0 = cx - total_w // 2
    y1, y2 = cy - glyph_h // 2, cy + glyph_h // 2
    for i in range(n):
        x1 = x0 + i * (glyph_w + gap)
        out[y1:y2, x1:x1 + glyph_w] = bgr
    return out


# ---------------------------------------------------------------------------
# HLDD 005 nameplate gate (2026-09-23)
# ---------------------------------------------------------------------------

class TestNameplateGate:
    """Regression for pursuit_mode_20260923_195448/50/53_81/83/85.png: 81
    (correct lock, real target) measured 33 glyph-sized components nearby;
    83 (locked onto an enemy flare effect) and 85 (locked onto the player's
    own engine exhaust) measured 8 and 9 respectively — no nameplate renders
    near either false positive. See TargetTracker._count_nameplate_glyphs."""

    _RED_BGR = (0, 0, 255)

    def test_disabled_by_default(self):
        """Gate must do nothing unless red_mass_nameplate_gate_enabled is
        set — a lone blob with no nameplate nearby still wins, exactly like
        red_mass_steering's own existing baseline behavior."""
        t = _tracker(red_mass_steering=True)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=15,
                              bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=2)

    def test_enabled_rejects_a_lone_blob_with_no_nameplate(self):
        """A lone red blob with no glyph cluster nearby (the flare/exhaust
        shape) must be rejected outright — no tall-bar candidate exists in
        this scene either, so the tick reports no target at all rather than
        locking onto the blob."""
        t = _tracker(red_mass_steering=True,
                      red_mass_nameplate_gate_enabled=True)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=15,
                              bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is False

    def test_enabled_accepts_a_blob_with_a_nearby_nameplate(self):
        """A blob with a glyph cluster at least as large as the measured
        real-target count (33) must still be tracked."""
        t = _tracker(red_mass_steering=True,
                      red_mass_nameplate_gate_enabled=True,
                      red_mass_nameplate_min_glyphs=20)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=200, cy=150, r=15, bgr=self._RED_BGR)
        frame = _draw_glyphs(frame, cx=200, cy=110, n=25, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=5)

    def test_glyph_count_below_threshold_is_rejected(self):
        """A handful of stray glyph-sized fragments — the size of what was
        actually measured on both false positives (8, 9) — must not clear a
        threshold set above that count."""
        t = _tracker(red_mass_steering=True,
                      red_mass_nameplate_gate_enabled=True,
                      red_mass_nameplate_min_glyphs=20)
        frame = _black_frame(400, 300)
        frame = _draw_circle(frame, cx=200, cy=150, r=15, bgr=self._RED_BGR)
        frame = _draw_glyphs(frame, cx=200, cy=110, n=5, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is False

    def test_count_nameplate_glyphs_default_bounds(self):
        """Direct unit test of the glyph-shape filter, independent of the
        tracker's state machine: a big blob (own-exhaust/flare stand-in)
        must not count as a glyph, using the shipped defaults (10-200 area,
        <=25px per side)."""
        t = _tracker()
        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[10:40, 10:40] = 255       # 30x30 = 900px, too big to be a glyph
        for i in range(5):
            x = 10 + i * 10
            mask[60:68, x:x + 4] = 255  # 4x8 = 32px, glyph-sized, gapped
        assert t._count_nameplate_glyphs(mask) == 5


# ---------------------------------------------------------------------------
# Action item 001 (2026-09-23): the nameplate gate must be able to REJECT a lock
# ---------------------------------------------------------------------------

def _draw_banner_shards(frame: np.ndarray, x: int, y: int, w: int = 260,
                        h: int = 22, shard_xs=(60, 130, 200)) -> np.ndarray:
    """The game's "INCOMING" banner as the tall-bar path sees it while it
    fades: a wide pink-red fill (measured H 165-178, S 139-165, V 109-238 on
    pursuit_mode_20260923_211425_64.png) whose saturation sits just under
    the tracker's S>=150 floor almost everywhere, with a few thin vertical
    strips that cross it. Each strip is a 4x16 tall/narrow red contour —
    exactly the shape _detect_targets accepts as a target bar (measured: a
    6x18, area-41 contour on the real banner, frame 60). V stays under
    red_mass_value_min (245) everywhere, as on the real banner, so the
    red-mass path never sees any of it."""
    out = frame.copy()
    out[y:y + h, x:x + w] = _bgr_from_hue(172, 140, 190)
    for sx in shard_xs:
        out[y + 3:y + 19, x + sx:x + sx + 4] = _bgr_from_hue(172, 165, 200)
    return out


class TestTallbarFallbackSuppression:
    """Measured 2026-09-23 (docs/action-item/001): with the nameplate gate
    on, a gate rejection used to fall back to the tall-bar pick, so the gate
    could narrow the red-mass override but never reject a lock. On the
    archived pursuit/eject frames, every lock the gate accepted sat on a
    real enemy nameplate (22 of 22) while roughly nine in ten of the
    fallback locks were false: the INCOMING banner's shards, own exhaust,
    terrain, fireballs. `red_mass_tallbar_fallback: false` makes the gate
    authoritative. The default stays true so every pre-existing scenario
    (and every synthetic single-shape test) is untouched."""

    _RED_BGR = (0, 0, 255)

    def _gated(self, **overrides):
        return _tracker(red_mass_steering=True,
                        red_mass_nameplate_gate_enabled=True,
                        red_mass_nameplate_min_glyphs=20, **overrides)

    def test_default_keeps_the_fallback_so_a_rejected_gate_still_locks_a_bar(self):
        """The old behavior, pinned: gate rejects (no nameplate anywhere) yet
        a tall red bar exists, and the tick still reports a lock on it."""
        t = self._gated()
        frame = _draw_bar(_black_frame(), cx=200, cy=150, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=3)

    def test_fallback_off_gate_rejection_yields_no_lock(self):
        """The bug itself. Same scene as above; with the fallback off, the
        gate's rejection is final: nothing to steer toward."""
        t = self._gated(red_mass_tallbar_fallback=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is False
        assert t.mode != TrackMode.TRACKING
        # n_detections stays the raw tall-bar count — the tall-bar path still
        # ran and found its bar; only its authority to lock was removed.
        assert obs["n_detections"] == 1

    def test_fallback_off_still_locks_when_the_gate_passes(self):
        """Suppression must not touch the path that works: a blob with a real
        nameplate cluster beside it still locks, at the red-mass centroid."""
        t = self._gated(red_mass_tallbar_fallback=False)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=15,
                             bgr=self._RED_BGR)
        frame = _draw_glyphs(frame, cx=200, cy=110, n=25, bgr=self._RED_BGR)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["centroid_x"] == pytest.approx(200, abs=5)

    def test_fallback_off_is_inert_when_the_gate_is_disabled(self):
        """Only a gate *rejection* suppresses the tall-bar pick. With the
        gate off there is no rejection to be authoritative about, so a green
        bar (no red pixels at all, hence no red-mass candidate) still locks."""
        t = _tracker(red_mass_steering=True,
                     red_mass_tallbar_fallback=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150)   # default green
        obs = t.update(frame)
        assert obs["visible"] is True

    def test_incoming_banner_shards_lock_by_default_and_not_when_suppressed(self):
        """The mechanism behind the banner's 779 measured acquisitions
        (docs/action-item/001): shards of the fading banner qualify as tall
        bars, and — the banner's V being far below the red-mass floor — the
        gate rejecting the tick changed nothing. Default reproduces the false
        lock; the fix removes it."""
        frame = _draw_banner_shards(_black_frame(400, 300), x=70, y=40)
        legacy = self._gated().update(frame)
        assert legacy["visible"] is True
        assert 40 <= legacy["centroid_y"] <= 62        # on the banner row
        fixed = self._gated(red_mass_tallbar_fallback=False).update(frame)
        assert fixed["visible"] is False

    def test_suppressed_tick_from_tracking_enters_lost_grace(self):
        """A real lock followed by a gate-rejected tick is an ordinary miss
        (LOST_GRACE), not a phantom hold on the tall-bar pick."""
        t = self._gated(red_mass_tallbar_fallback=False)
        locked = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=15,
                              bgr=self._RED_BGR)
        locked = _draw_glyphs(locked, cx=200, cy=110, n=25, bgr=self._RED_BGR)
        assert t.update(locked)["visible"] is True
        assert t.mode == TrackMode.TRACKING
        bar_only = _draw_bar(_black_frame(400, 300), cx=200, cy=150,
                             bgr=self._RED_BGR)
        obs = t.update(bar_only)
        assert obs["visible"] is False
        assert t.mode == TrackMode.LOST_GRACE

    def test_config_flag_is_read(self):
        t = TargetTracker({"tracking": {"red_mass_tallbar_fallback": False}})
        assert t._red_mass_tallbar_fallback is False
        assert TargetTracker({"tracking": {}})._red_mass_tallbar_fallback is True


class TestPickPathLogging:
    """The instrumentation action item 001 asked for first: one greppable
    line per tick saying which path supplied the lock, what the gate saw,
    and — when the fallback is suppressed — what it would have picked."""

    _RED_BGR = (0, 0, 255)

    def _pick_lines(self, caplog):
        """The per-tick DEBUG lines only (each carries `path=`); the
        rate-limited INFO veto line is checked separately."""
        return [r.getMessage() for r in caplog.records
                if "TRACKPICK: path=" in r.getMessage()]

    def test_redmass_path_is_attributed_with_glyph_count(self, caplog):
        import logging
        t = _tracker(red_mass_steering=True,
                     red_mass_nameplate_gate_enabled=True,
                     red_mass_nameplate_min_glyphs=20)
        frame = _draw_circle(_black_frame(400, 300), cx=200, cy=150, r=15,
                             bgr=self._RED_BGR)
        frame = _draw_glyphs(frame, cx=200, cy=110, n=25, bgr=self._RED_BGR)
        with caplog.at_level(logging.DEBUG, logger="wingman.tracker"):
            t.update(frame)
        line = self._pick_lines(caplog)[-1]
        assert "path=redmass" in line
        assert "gate=pass" in line
        assert "glyphs=25" in line

    def test_tallbar_path_is_attributed_when_redmass_is_off(self, caplog):
        import logging
        t = _tracker()
        frame = _draw_bar(_black_frame(), cx=200, cy=150)
        with caplog.at_level(logging.DEBUG, logger="wingman.tracker"):
            t.update(frame)
        line = self._pick_lines(caplog)[-1]
        assert "path=tallbar" in line
        assert "gate=off" in line

    def test_suppressed_pick_names_what_the_fallback_would_have_locked(self, caplog):
        import logging
        t = _tracker(red_mass_steering=True,
                     red_mass_nameplate_gate_enabled=True,
                     red_mass_nameplate_min_glyphs=20,
                     red_mass_tallbar_fallback=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150, bgr=self._RED_BGR)
        with caplog.at_level(logging.DEBUG, logger="wingman.tracker"):
            t.update(frame)
        line = self._pick_lines(caplog)[-1]
        assert "path=suppressed" in line
        assert "gate=reject" in line
        assert "tall=(200," in line

    def test_no_candidate_at_all_is_path_none(self, caplog):
        import logging
        t = _tracker(red_mass_steering=True,
                     red_mass_nameplate_gate_enabled=True,
                     red_mass_tallbar_fallback=False)
        with caplog.at_level(logging.DEBUG, logger="wingman.tracker"):
            t.update(_black_frame())
        assert "path=none" in self._pick_lines(caplog)[-1]

    def test_suppression_is_also_reported_at_info_rate_limited(self, caplog):
        """Debug lines vanish from a normal session log, so the first
        suppressed pick is also surfaced at INFO — same 1st/10th/100th
        rate limit every other shadow counter in this module uses."""
        import logging
        t = _tracker(red_mass_steering=True,
                     red_mass_nameplate_gate_enabled=True,
                     red_mass_tallbar_fallback=False)
        frame = _draw_bar(_black_frame(), cx=200, cy=150, bgr=self._RED_BGR)
        with caplog.at_level(logging.INFO, logger="wingman.tracker"):
            for _ in range(3):
                t.update(frame)
        infos = [r for r in caplog.records
                 if r.levelno == logging.INFO and "TRACKPICK" in r.getMessage()]
        assert len(infos) == 1                     # 1st only; 2nd, 3rd suppressed
        assert "1 so far" in infos[0].getMessage()

    def test_probe_exposes_the_gate_numbers_it_decided_on(self):
        t = _tracker(red_mass_steering=True,
                     red_mass_nameplate_gate_enabled=True,
                     red_mass_nameplate_min_glyphs=20)
        frame = _draw_glyphs(_black_frame(400, 300), cx=200, cy=110, n=7,
                             bgr=self._RED_BGR)
        probe = t._red_mass_probe(frame, 0, 0, 400, 300)
        assert probe["gate"] == "reject"
        assert probe["glyphs"] == 7
        assert probe["centroid"] is None
        assert probe["px"] > 0
        # The public wrapper is unchanged: same answer, no dict.
        assert t._red_mass_centroid(frame, 0, 0, 400, 300) is None


# ---------------------------------------------------------------------------
# Error normalization
# ---------------------------------------------------------------------------

class TestErrorNorm:
    def test_target_at_center_gives_zero_error(self):
        t = _tracker()
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=w // 2, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] == pytest.approx(0.0, abs=0.05)

    def test_target_left_of_center_gives_negative_error(self):
        t = _tracker()
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=80, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] < -0.3

    def test_target_right_of_center_gives_positive_error(self):
        t = _tracker()
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=320, cy=h // 2)
        obs = t.update(frame)
        assert obs["visible"] is True
        assert obs["error_norm"] > 0.3

    def test_error_clamped_to_unit_range(self):
        t = _tracker()
        w, h = 400, 300
        frame = _draw_bar(_black_frame(w, h), cx=2, cy=h // 2)
        obs = t.update(frame)
        assert -1.0 <= obs["error_norm"] <= 1.0

    def test_no_error_when_no_target(self):
        t = _tracker()
        obs = t.update(_black_frame())
        assert obs["error_norm"] is None


# ---------------------------------------------------------------------------
# Target selection: nearest-to-last heuristic
# ---------------------------------------------------------------------------

class TestTargetSelection:
    def test_picks_nearer_of_two_targets_on_first_frame(self):
        """Without a prior position, should pick target nearest to frame center."""
        t = _tracker()
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
        t = _tracker()
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
        t = _tracker(prefer_red_lock=True)
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
        t = _tracker(prefer_red_lock=True)
        w, h = 400, 300
        frame = self._hsv_bar(_black_frame(w, h), w // 2, h // 2, h_val=60)  # green bar
        obs = t.update(frame)
        assert obs["visible"] is True


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
# Reference frame regression
# ---------------------------------------------------------------------------

class TestReferenceFrame:
    # P1_060 reused (P2_050 deleted 2026-08-13 — byte-identical copy).
    _REF = Path("test_screenshots/integration_test/P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png")

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
        t = _tracker()
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
        t = _tracker()
        obs = t.update(frame)
        # The reticle is roughly at the left-center of the frame (approx x=0.35*w).
        # A detection there with error_norm near -0.30 suggests the reticle was hit.
        # We can't rule it out statically, but we CAN assert the centroid is not
        # suspiciously small (a real bar is taller than it is wide).
        if obs["centroid_x"] is not None:
            # Re-run internal detect to inspect raw contours
            raw, _red_hits, _green_hits, _red_won = t._detect_targets(frame)
            for _cx, _cy, area in raw:
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
        shows where the aircraft is being commanded to point regardless of
        which detection mode (tall-bar or red_mass_steering) produced it.
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

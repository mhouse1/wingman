"""Action item 001, Cycle 5 (2026-09-24): the local ROI follows a nameplate the
crop edge cut.

Live evidence (07:34-08:56 pursuit and dive logs): of 182 dropped locks, 125
had red pixels present and a nameplate-gate rejection, and the four drop ticks
whose raw crop was archived and replayed all showed a real nameplate cut by the
crop edge. A rejection never moved the ROI, so the next scan clipped the same
label. `TargetTracker._follow_clipped_nameplate` moves the ROI toward the
clipped red mass without touching lock decisions, steering or the gate.
"""

import cv2
import numpy as np

from wingman.tracker import TargetTracker, TrackMode

_RED = (0, 0, 255)
_W, _H = 1000, 600          # local ROI at scale 0.22 is 220 x 132
_GLYPHS = 30                # 30 glyphs, 178 px wide, clears the gate (20)

_TRACKING_CFG = {
    "enabled": True,
    "acquisition_region_pct": [0.0, 0.0, 1.0, 1.0],
    "lost_timeout_sec": 0.70,
    "prefer_red_lock": True,
    "red_mass_steering": True,
    "red_mass_nameplate_gate_enabled": True,
    "red_mass_nameplate_min_glyphs": 20,
    "red_mass_tallbar_fallback": False,
    "local_roi_enabled": True,
    "local_roi_scale": 0.22,
    "local_roi_min_px": [40, 25],
    "local_roi_expand_factor": 1.25,
    "local_roi_max_scale": 0.45,
    "local_roi_reacquire_cycles": 3,
    "local_roi_follow_on_clip": True,
    "local_roi_follow_min_px": 50,
}
_HSV_CFG = {
    "red_lower": [0, 150, 150], "red_upper": [10, 255, 255],
    "green_lower": [45, 150, 150], "green_upper": [75, 255, 255],
    "min_contour_area": 12, "min_aspect_ratio": 2.5,
}


def _tracker(**overrides) -> TargetTracker:
    return TargetTracker({"tracking": {**_TRACKING_CFG, **overrides}, "tracking_hsv": _HSV_CFG})


def _frame() -> np.ndarray:
    return np.zeros((_H, _W, 3), dtype=np.uint8)


def _target(frame: np.ndarray, cx: int, n_glyphs: int = _GLYPHS) -> np.ndarray:
    """An aircraft blob at (cx, 300) with an `n_glyphs` nameplate row above it
    (glyph-sized rectangles, the shape the gate counts), centred on cx."""
    out = frame.copy()
    cv2.circle(out, (cx, 300), 10, _RED, -1)
    x0 = cx - (n_glyphs * 6 - 2) // 2
    for i in range(n_glyphs):
        x1 = x0 + i * 6
        out[258:266, x1:x1 + 4] = _RED
    return out


def _locked_tracker(**overrides) -> TargetTracker:
    """Tracker that locked a fully visible target at x=400 at ts=0.0."""
    t = _tracker(**overrides)
    obs = t.update(_target(_frame(), 400), ts=0.0)
    assert obs["visible"] is True and obs["mode"] == TrackMode.TRACKING.name
    return t


# The lock above centres the ROI on x=400. Moving the scene 90 px right leaves
# the nameplate's right end outside the 220 px ROI (x 290..509), which counts
# 18 glyphs and so fails the gate at 20.
_SHIFTED = 490


class TestFollowClippedNameplate:
    def test_the_scene_really_is_clipped_without_the_feature(self):
        """Control: with the feature off the same drift keeps missing, so the
        tests below prove the follow, not an easy fixture."""
        t = _locked_tracker(local_roi_follow_on_clip=False)
        shifted = _target(_frame(), _SHIFTED)
        assert t.update(shifted, ts=0.3)["visible"] is False
        assert t.update(shifted, ts=0.6)["visible"] is False

    def test_roi_moves_toward_the_cut_label_and_the_next_scan_relocks(self):
        t = _locked_tracker()
        before = t._roi_rect
        shifted = _target(_frame(), _SHIFTED)

        assert t.update(shifted, ts=0.3)["visible"] is False   # cut label rejected
        after = t._roi_rect
        assert after[0] > before[0], "ROI should move right toward the cut label"
        assert t._roi_follow_count == 1

        obs = t.update(shifted, ts=0.6)                        # same scene, moved window
        assert obs["visible"] is True
        assert obs["mode"] == TrackMode.TRACKING.name

    def test_lock_decision_and_steering_are_unchanged_on_the_rejected_tick(self):
        t = _locked_tracker()
        first = t.update(_target(_frame(), 400), ts=0.1)       # still locked, x=400
        obs = t.update(_target(_frame(), _SHIFTED), ts=0.4)
        assert obs["visible"] is False
        assert obs["centroid_x"] == first["centroid_x"]        # last lock, not the mass
        assert obs["error_norm"] == first["error_norm"]

    def test_disabled_by_default(self):
        cfg = {**_TRACKING_CFG}
        cfg.pop("local_roi_follow_on_clip")
        t = TargetTracker({"tracking": cfg, "tracking_hsv": _HSV_CFG})
        t.update(_target(_frame(), 400), ts=0.0)
        before = t._roi_rect
        t.update(_target(_frame(), _SHIFTED), ts=0.3)
        assert t._roi_rect == before
        assert t._roi_follow_count == 0

    def test_no_follow_when_the_red_does_not_touch_the_crop_edge(self):
        """An absent nameplate (5 glyphs, all well inside the ROI) is a real
        rejection, not a cut label: the ROI must stay put."""
        t = _locked_tracker()
        before = t._roi_rect
        assert t.update(_target(_frame(), 400, n_glyphs=5), ts=0.3)["visible"] is False
        assert t._roi_rect == before
        assert t._roi_follow_count == 0

    def test_no_follow_below_the_minimum_red_pixel_count(self):
        """A speck touching the crop edge is debris, not a label."""
        t = _locked_tracker()
        before = t._roi_rect
        speck = _frame()
        rx, ry, rw, rh = before
        speck[ry + rh // 2:ry + rh // 2 + 3, rx + rw - 3:rx + rw] = _RED   # 9 px
        assert t.update(speck, ts=0.3)["visible"] is False
        assert t._roi_rect == before
        assert t._roi_follow_count == 0

    def test_no_follow_on_the_wide_acquisition_scan(self):
        """There is no ROI to move before the first lock: a label cut by the
        frame edge in the acquisition scan must not create one."""
        t = _tracker()
        obs = t.update(_target(_frame(), _W - 20), ts=0.0)
        assert obs["visible"] is False
        assert t._roi_rect is None
        assert t._roi_follow_count == 0

    def test_relock_clears_the_follow_hint(self):
        t = _locked_tracker()
        shifted = _target(_frame(), _SHIFTED)
        t.update(shifted, ts=0.3)
        assert t._roi_centre_hint is not None
        t.update(shifted, ts=0.6)
        assert t._roi_centre_hint is None

    def test_reset_clears_the_follow_hint(self):
        t = _locked_tracker()
        t.update(_target(_frame(), _SHIFTED), ts=0.3)
        t.reset()
        assert t._roi_centre_hint is None

    def test_grace_timeout_still_ends_the_follow(self):
        """The follow rides the existing LOST_GRACE clock (`lost_timeout_sec`):
        once it expires the ROI is dropped and the hint with it."""
        t = _locked_tracker()
        t.update(_target(_frame(), _SHIFTED), ts=0.3)
        assert t._roi_centre_hint is not None
        t.update(_frame(), ts=0.9)                             # empty frame, past 0.70 s
        assert t.mode == TrackMode.ACQUIRING
        assert t._roi_rect is None
        assert t._roi_centre_hint is None

    def test_expansion_recentres_on_the_followed_position_not_the_stale_lock(self):
        t = _locked_tracker(lost_timeout_sec=5.0, local_roi_reacquire_cycles=2)
        t.update(_target(_frame(), _SHIFTED), ts=0.3)          # follow, miss 1
        hint_x = t._roi_centre_hint[0]
        t.update(_frame(), ts=0.6)                             # miss 2 -> expansion
        rx, _ry, rw, _rh = t._roi_rect
        assert abs((rx + rw / 2.0) - hint_x) <= 2.0
        assert abs((rx + rw / 2.0) - 400.0) > 30.0


class TestProbeEdgeReporting:
    """`_red_mass_probe` reports where the red is, gate or no gate."""

    def _probe(self, frame, **overrides):
        t = _tracker(**overrides)
        return t._red_mass_probe(frame, 0, 0, _W, _H)

    def test_interior_mass_reports_no_clipped_edges(self):
        pr = self._probe(_target(_frame(), 400))
        assert pr["clipped_edges"] == ()
        assert pr["gate"] == "pass"
        assert pr["centroid"] == pr["mass_centroid"]

    def test_mass_on_the_right_border_reports_the_right_edge(self):
        frame = _frame()
        frame[290:310, _W - 30:_W] = _RED
        assert "right" in self._probe(frame, red_mass_nameplate_gate_enabled=False)["clipped_edges"]

    def test_rejected_probe_keeps_the_mass_centroid_but_not_the_lock_centroid(self):
        pr = self._probe(_target(_frame(), 400, n_glyphs=5))
        assert pr["gate"] == "reject"
        assert pr["centroid"] is None
        assert pr["mass_centroid"] is not None

    def test_no_red_reports_nothing(self):
        pr = self._probe(_frame())
        assert pr["mass_centroid"] is None
        assert pr["clipped_edges"] == ()
        assert pr["px"] == 0


class TestBlobLogging:
    """Action item 001, Cycle 6: `TRACKPICK` gains `blob=(x,y,aAREA,WxH)`, the
    largest red component (absolute centre), so a gate-rejected tick still says
    where the red is. Logging only: it never changes a pick or a gate decision,
    and it is computed only while DEBUG logging is on."""

    _LOGGER = "wingman.tracker"

    @staticmethod
    def _icon(frame: np.ndarray, x: int, y: int, size: int = 30) -> np.ndarray:
        out = frame.copy()
        out[y:y + size, x:x + size] = _RED
        return out

    def test_off_unless_debug_logging_is_on(self):
        pr = _tracker()._red_mass_probe(self._icon(_frame(), 300, 200), 0, 0, _W, _H)
        assert pr["blob"] is None

    def test_reports_absolute_centre_area_and_size(self, caplog):
        crop = self._icon(np.zeros((132, 220, 3), np.uint8), 40, 30)   # 30x30 at crop (40,30)
        with caplog.at_level("DEBUG", logger=self._LOGGER):
            pr = _tracker()._red_mass_probe(crop, 500, 300, _W, _H)
        x, y, area, w, h = pr["blob"]
        assert (w, h, area) == (30, 30, 900)
        # the block's centre is (54.5, 44.5) in the crop, so (554.5, 344.5) in the frame
        assert abs(x - 554.5) <= 0.5 and abs(y - 344.5) <= 0.5

    def test_picks_the_largest_of_several_components(self, caplog):
        frame = self._icon(self._icon(_frame(), 100, 100, size=12), 600, 300, size=34)
        with caplog.at_level("DEBUG", logger=self._LOGGER):
            pr = _tracker()._red_mass_probe(frame, 0, 0, _W, _H)
        assert pr["blob"][2] == 34 * 34
        assert abs(pr["blob"][0] - 616.5) <= 1 and abs(pr["blob"][1] - 316.5) <= 1

    def test_no_red_gives_no_blob(self, caplog):
        with caplog.at_level("DEBUG", logger=self._LOGGER):
            assert _tracker()._red_mass_probe(_frame(), 0, 0, _W, _H)["blob"] is None

    def test_trackpick_line_carries_the_blob_on_a_rejected_tick(self, caplog):
        t = _tracker()
        with caplog.at_level("DEBUG", logger=self._LOGGER):
            obs = t.update(self._icon(_frame(), 300, 200), ts=0.0)   # icon, no nameplate
        assert obs["visible"] is False
        line = next(r.getMessage() for r in caplog.records if "TRACKPICK:" in r.getMessage())
        assert "gate=reject" in line
        assert "blob=(314,214,a900,30x30)" in line    # centre 314.5 rounds to 314

    def test_trackpick_blob_is_dash_without_red(self, caplog):
        t = _tracker()
        with caplog.at_level("DEBUG", logger=self._LOGGER):
            t.update(_frame(), ts=0.0)
        line = next(r.getMessage() for r in caplog.records if "TRACKPICK:" in r.getMessage())
        assert "blob=- clu=" in line

"""`TargetTracker._red_mass_probe`'s logging-only outputs: where the red mask
touches the scanned crop's edge (`clipped_edges`, TRACKPICK's `edges=`) and the
largest red component (TRACKPICK's `blob=`, action item 001 Cycle 6).

Moved here from test_roi_follow.py when the local ROI and its clipped-nameplate
follow were removed (2026-09-24); these probe outputs stayed.
"""

import cv2
import numpy as np

from wingman.tracker import TargetTracker

_RED = (0, 0, 255)
_W, _H = 1000, 600
_GLYPHS = 30                # 30 glyphs, 178 px wide, clears the gate (20)

_TRACKING_CFG = {
    "enabled": True,
    "acquisition_region_pct": [0.0, 0.0, 1.0, 1.0],
    "red_mass_nameplate_min_glyphs": 20,
}
_HSV_CFG = {"red_lower": [0, 150, 150], "red_upper": [10, 255, 255]}


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


class TestProbeEdgeReporting:
    """`_red_mass_probe` reports where the red is, gate or no gate."""

    def _probe(self, frame, **overrides):
        t = _tracker(**overrides)
        return t._red_mass_probe(frame, 0, 0, _W, _H)

    def test_interior_mass_reports_no_clipped_edges(self):
        pr = self._probe(_target(_frame(), 400))
        assert pr["clipped_edges"] == ()
        assert pr["gate"] == "pass"
        assert pr["centroid"] is not None

    def test_mass_on_the_right_border_reports_the_right_edge(self):
        frame = _frame()
        frame[290:310, _W - 30:_W] = _RED
        assert "right" in self._probe(frame)["clipped_edges"]

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

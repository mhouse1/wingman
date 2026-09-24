"""Action item 001, Cycle 12 (2026-09-24): widened acquisition region, HUD zone
exclusion, and steering on one nameplate cluster.

Trigger: pursuit_mode_20260924_164821_48.png, tracker ACQUIRING with two complete enemy
nameplates on screen below the acquisition box. Measured over 300 archived frames: 41% of
the nameplates shown lay outside the old box; under the old rule (mean of every red
pixel) a wider box moved the steering point of already-locked frames a median 134 px even
with the HUD masked, because other nameplates entered the mean.
"""

import cv2
import numpy as np
import pytest

from wingman.tracker import TargetTracker, TrackMode

_RED = (0, 0, 255)
_W, _H = 1920, 1200

_HUD_ZONES = [
    [0.0, 0.0, 1.0, 0.0917],        # scoreboard and rosters
    [0.828, 0.0, 1.0, 0.275],       # minimap
    [0.75, 0.883, 1.0, 1.0],        # weapons panel
    [0.0, 0.9, 0.172, 1.0],         # squad logo
]

_BASE = {
    "enabled": True,
    "acquisition_region_pct": [0.2, 0.18, 0.8, 0.68],
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
}
_HSV = {
    "red_lower": [0, 150, 150], "red_upper": [10, 255, 255],
    "green_lower": [45, 150, 150], "green_upper": [75, 255, 255],
    "min_contour_area": 12, "min_aspect_ratio": 2.5,
}


def _tracker(**overrides) -> TargetTracker:
    return TargetTracker({"tracking": {**_BASE, **overrides}, "tracking_hsv": _HSV})


def _wide(**extra):
    return _tracker(acquisition_region_pct=[0.0, 0.09, 1.0, 0.95],
                    red_mass_exclude_zones_pct=_HUD_ZONES,
                    red_mass_cluster_select=True, **extra)


def _frame():
    return np.zeros((_H, _W, 3), dtype=np.uint8)


def _nameplate(frame, cx, cy, n_glyphs=28, with_marker=True):
    """A nameplate block centred on (cx, cy): rows of glyph-sized rectangles (about
    200 x 90 px) and an underline bar, with the target marker (a solid blob) 185 px
    above, as the game draws it."""
    out = frame
    per_row = max(1, n_glyphs // 3)
    rows = [per_row, per_row, n_glyphs - 2 * per_row]
    for r, count in enumerate(rows):
        y = cy - 30 + r * 30
        x0 = cx - (count * 8) // 2
        for i in range(count):
            out[y:y + 12, x0 + i * 8:x0 + i * 8 + 5] = _RED
    out[cy + 48:cy + 52, cx - 40:cx + 40] = _RED
    if with_marker:
        cv2.circle(out, (cx, cy - 185), 14, _RED, -1)
    return out


def _probe(t, frame, ref=None):
    return t._red_mass_probe(frame, 0, 0, _W, _H, ref=ref)


# --- the region and the zones -------------------------------------------------

def test_a_nameplate_below_the_old_box_is_invisible_to_the_old_region_and_found_by_the_wide_one():
    frame = _nameplate(_frame(), 1020, 960)               # y 930-1012, box ends at y 816
    assert _tracker().update(frame, ts=0.0)["visible"] is False
    obs = _wide().update(frame, ts=0.0)
    assert obs["visible"] is True
    assert obs["centroid_y"] > 816


def test_red_hud_chrome_in_an_excluded_zone_is_ignored():
    frame = _frame()
    frame[0:90, 1180:1400] = _RED                          # the red team's score box
    cv2.circle(frame, (1760, 160), 120, _RED, 6)           # a red ring on the minimap
    assert _wide().update(frame, ts=0.0)["visible"] is False
    assert _probe(_wide(), frame)["px"] == 0


def test_glyph_like_hud_text_in_a_zone_does_not_count_toward_the_gate():
    frame = _nameplate(_frame(), 960, 600, n_glyphs=12)    # too few to pass alone
    top = _nameplate(_frame(), 1500, 60, n_glyphs=30, with_marker=False)
    frame = np.maximum(frame, top)                         # 30 glyphs in the scoreboard strip
    assert _wide().update(frame, ts=0.0)["visible"] is False


def test_zones_are_off_by_default():
    frame = _frame()
    frame[20:80, 1180:1400] = _RED
    assert _probe(_tracker(), frame)["px"] > 0


# --- cluster selection ----------------------------------------------------------

def test_two_nameplates_steer_on_one_of_them_not_between_them():
    """The 16:48:21 situation. The old rule aimed at the midpoint of the two."""
    frame = _nameplate(_nameplate(_frame(), 960, 800), 1500, 950)
    old = _probe(_tracker(red_mass_nameplate_min_glyphs=10), frame)["centroid"]
    new = _probe(_wide(), frame)
    assert new["clusters"] == 2 and new["gate"] == "pass"
    x, y = new["centroid"]
    assert (abs(x - 960) < 60) or (abs(x - 1500) < 60), "must sit on a nameplate"
    assert 700 < old[0] < 1300, "the old whole-crop mean lands between the two"


def test_with_no_previous_lock_the_nameplate_nearest_the_screen_centre_wins():
    frame = _nameplate(_nameplate(_frame(), 900, 620), 1600, 900)
    x, _ = _probe(_wide(), frame)["centroid"]
    assert abs(x - 900) < 60


def test_the_previous_lock_wins_over_the_screen_centre():
    frame = _nameplate(_nameplate(_frame(), 900, 620), 1600, 900)
    x, _ = _probe(_wide(), frame, ref=(1600, 850))["centroid"]
    assert abs(x - 1600) < 60


def test_one_nameplate_in_a_local_roi_sized_crop_gives_the_same_point_as_the_old_mean():
    """The common case must not move: a crop the pixel window covers entirely."""
    frame = _nameplate(_frame(), 960, 560)
    roi = frame[400:664, 800:1222]                         # 422 x 264
    legacy = _tracker()._red_mass_probe(roi, 800, 400, _W, _H)["centroid"]
    clustered = _wide()._red_mass_probe(roi, 800, 400, _W, _H)["centroid"]
    assert clustered == pytest.approx(legacy, abs=1.0)


def test_the_marker_above_the_label_is_included_in_the_steering_point():
    frame = _nameplate(_frame(), 960, 700)                 # marker at y 515
    _, y = _probe(_wide(), frame)["centroid"]
    assert 515 < y < 700, "between the marker and the label, like the whole-crop mean"


def test_the_gate_counts_per_cluster_not_across_the_whole_crop():
    """Two half-labels far apart (12 glyphs each) pass a whole-crop count of 24 but
    are not a nameplate."""
    frame = _nameplate(_nameplate(_frame(), 700, 500, 12, False), 1500, 800, 12, False)
    assert _probe(_tracker(), frame)["gate"] == "pass"     # old rule: 24 >= 20
    p = _probe(_wide(), frame)
    assert p["gate"] == "reject" and p["glyphs"] == 12


def test_a_rejected_tick_reports_the_best_cluster_count():
    frame = _nameplate(_frame(), 960, 600, 15, False)
    p = _probe(_wide(), frame)
    assert (p["gate"], p["glyphs"], p["clusters"]) == ("reject", 15, 0)


def test_cluster_mode_is_off_by_default():
    frame = _nameplate(_nameplate(_frame(), 700, 500, 12, False), 1500, 800, 12, False)
    assert _probe(_tracker(), frame)["clusters"] is None


def test_a_lock_stays_on_its_nameplate_when_a_second_one_appears():
    """Tracking continuity: once locked, the nearest to the previous lock keeps the lock."""
    t = _wide()
    first = _nameplate(_frame(), 960, 600)
    obs = t.update(first, ts=0.0)
    assert obs["visible"] is True and obs["mode"] == TrackMode.TRACKING.name
    x0 = obs["centroid_x"]
    second = _nameplate(_nameplate(_frame(), 960, 600), 1500, 900)
    obs = t.update(second, ts=0.3)
    assert obs["visible"] is True and abs(obs["centroid_x"] - x0) < 80

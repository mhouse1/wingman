"""Action item 001, Cycle 12 (2026-09-24): widened acquisition region, HUD zone
exclusion, and steering on one nameplate cluster.

Trigger: pursuit_mode_20260924_164821_48.png, tracker ACQUIRING with two complete enemy
nameplates on screen below the acquisition box. Measured over 300 archived frames: 41% of
the nameplates shown lay outside the old box; under the old rule (mean of every red
pixel) a wider box moved the steering point of already-locked frames a median 134 px even
with the HUD masked, because other nameplates entered the mean.

2026-09-24: the steering point is the chosen nameplate's marker (the red component above
the label), not a pixel mean around the label, and a lock is kept on a looser glyph test
near the previous lock's cluster while the strict gate decides acquisition only.
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
    "prefer_red_lock": True,
    "red_mass_steering": True,
    "red_mass_nameplate_gate_enabled": True,
    "red_mass_nameplate_min_glyphs": 20,
    "red_mass_tallbar_fallback": False,
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
    assert obs["centroid_x"] == pytest.approx(1020, abs=3)
    assert obs["centroid_y"] == pytest.approx(960 - 185, abs=3)   # its marker


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


def test_the_steering_point_is_the_marker_not_the_label():
    frame = _nameplate(_frame(), 960, 700)                 # marker at (960, 515)
    p = _probe(_wide(), frame)
    assert p["aim"] == "marker"
    assert p["centroid"] == pytest.approx((960, 515), abs=1.0)


def test_without_a_marker_the_label_is_moved_up_by_the_offset():
    frame = _nameplate(_frame(), 960, 700, with_marker=False)
    p = _probe(_wide(), frame)
    cx, cy = p["cluster"]
    assert p["aim"] == "offset"
    assert p["centroid"] == pytest.approx((cx, cy - 225), abs=0.5)


def test_a_red_shape_outside_the_measured_band_is_not_taken_for_the_marker():
    frame = _nameplate(_frame(), 960, 700, with_marker=False)
    cv2.circle(frame, (960, 600), 14, _RED, -1)            # about 105 px above the label
    assert _probe(_wide(), frame)["aim"] == "offset"


def test_the_largest_shape_in_the_band_is_the_marker():
    frame = _nameplate(_frame(), 960, 700, with_marker=False)
    cv2.circle(frame, (900, 500), 8, _RED, -1)             # smaller, but bigger than a glyph
    cv2.circle(frame, (1010, 520), 16, _RED, -1)
    assert _probe(_wide(), frame)["centroid"] == pytest.approx((1010, 520), abs=1.0)


def test_a_stationary_target_keeps_the_same_steering_point_every_tick():
    """The old point blended label and marker in the wide scan and settled on the label
    once the local ROI cut the marker off, so a target that never moved read
    error_norm_y +0.21, +0.32, +0.36 over its first three ticks."""
    t = _wide()
    frame = _nameplate(_frame(), 960, 700)
    points = [(o["centroid_x"], o["centroid_y"])
              for o in (t.update(frame, ts=0.33 * i) for i in range(4))]
    assert all(pt == pytest.approx(points[0], abs=0.5) for pt in points)


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


# --- acquire strictly, keep loosely --------------------------------------------------

def _locked(cx=960, cy=700):
    t = _wide()
    obs = t.update(_nameplate(_frame(), cx, cy), ts=0.0)
    assert obs["visible"] is True and obs["mode"] == TrackMode.TRACKING.name
    return t


def test_a_label_below_the_gate_is_kept_near_the_lock():
    """10 glyphs fail the gate (20) but pass the keep test next to the lock."""
    t = _locked()
    obs = t.update(_nameplate(_frame(), 990, 720, n_glyphs=10), ts=0.33)
    assert obs["visible"] is True and obs["mode"] == TrackMode.TRACKING.name
    assert obs["centroid_x"] == pytest.approx(990, abs=3)


def test_a_label_below_the_gate_is_never_acquired():
    obs = _wide().update(_nameplate(_frame(), 960, 700, n_glyphs=10), ts=0.0)
    assert obs["visible"] is False


def test_a_label_below_the_gate_far_from_the_lock_is_not_kept():
    t = _locked()
    obs = t.update(_nameplate(_frame(), 960 + 400, 700, n_glyphs=10), ts=0.33)
    assert obs["visible"] is False


def test_the_keep_test_holds_across_a_missed_tick_while_the_target_is_remembered():
    t = _locked()
    t.update(_frame(), ts=0.33)
    assert t.mode == TrackMode.LOST_GRACE
    obs = t.update(_nameplate(_frame(), 980, 710, n_glyphs=10), ts=1.0)
    assert obs["visible"] is True and obs["mode"] == TrackMode.TRACKING.name


def test_once_the_target_is_forgotten_the_keep_test_no_longer_applies():
    t = _locked(cx=400)                                    # off centre: remembered 2.0 s
    t.update(_frame(), ts=0.3)
    t.update(_frame(), ts=2.1)
    assert t.mode == TrackMode.ACQUIRING
    assert t.update(_nameplate(_frame(), 410, 700, n_glyphs=10), ts=2.4)["visible"] is False


def test_a_strict_label_elsewhere_is_acquired_when_the_lock_has_nothing_to_keep():
    t = _locked(cx=400)
    obs = t.update(_nameplate(_frame(), 1500, 700), ts=0.33)
    assert obs["visible"] is True
    assert obs["centroid_x"] == pytest.approx(1500, abs=3)


def test_trackpick_names_a_keep_and_where_the_steering_point_came_from(caplog):
    t = _locked()
    with caplog.at_level("DEBUG", logger="wingman.tracker"):
        t.update(_nameplate(_frame(), 990, 720, n_glyphs=10), ts=0.33)
        t.update(_nameplate(_frame(), 990, 720, with_marker=False), ts=0.66)
    lines = [r.getMessage() for r in caplog.records if "TRACKPICK:" in r.getMessage()]
    assert "path=keep" in lines[0] and "gate=keep" in lines[0] and "aim=marker" in lines[0]
    assert "path=redmass" in lines[1] and "gate=pass" in lines[1] and "aim=offset" in lines[1]

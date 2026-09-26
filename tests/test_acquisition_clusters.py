"""Action item 001, Cycle 12 (2026-09-24): widened acquisition region, HUD zone
exclusion, and steering on one nameplate cluster.

Trigger: pursuit_mode_20260924_164821_48.png, tracker ACQUIRING with two complete enemy
nameplates on screen below the acquisition box. Measured over 300 archived frames: 41% of
the nameplates shown lay outside the old box; under the old rule (mean of every red
pixel) a wider box moved the steering point of already-locked frames a median 134 px even
with the HUD masked, because other nameplates entered the mean.

2026-09-24: the steering point is the aircraft, drawn by the game below its nameplate (its
marker centre measured 90 to 110 px below the label's glyph centre, mean 100.5, on 8 frames),
not a pixel mean around the label, and a lock is kept on a looser glyph test near the
previous lock's cluster while the strict gate decides acquisition only.
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
    "red_mass_nameplate_min_glyphs": 20,
}
_HSV = {"red_lower": [0, 150, 150], "red_upper": [10, 255, 255]}


def _tracker(**overrides) -> TargetTracker:
    return TargetTracker({"tracking": {**_BASE, **overrides}, "tracking_hsv": _HSV})


def _wide(**extra):
    return _tracker(acquisition_region_pct=[0.0, 0.09, 1.0, 0.95],
                    red_mass_exclude_zones_pct=_HUD_ZONES, **extra)


def _frame():
    return np.zeros((_H, _W, 3), dtype=np.uint8)


def _nameplate(frame, cx, cy, n_glyphs=28, with_marker=True):
    """A nameplate block centred on (cx, cy): rows of glyph-sized rectangles (about
    200 x 90 px) and an underline bar, with the target marker (a solid blob) 100 px
    below the label's centre, as the game draws it."""
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
        cv2.circle(out, (cx, cy + 100), 14, _RED, -1)
    return out


def _probe(t, frame, ref=None):
    return t._red_mass_probe(frame, 0, 0, _W, _H, ref=ref)


# --- the region and the zones -------------------------------------------------

def test_a_nameplate_below_the_old_box_is_invisible_to_the_old_region_and_found_by_the_wide_one():
    # x 1300: clear of the NO LOCK exclusion (x 883-1037, y 900-948), a default now.
    frame = _nameplate(_frame(), 1300, 960)               # y 930-1012, box ends at y 816
    assert _tracker().update(frame, ts=0.0)["visible"] is False
    obs = _wide().update(frame, ts=0.0)
    assert obs["visible"] is True
    assert obs["centroid_x"] == pytest.approx(1300, abs=3)
    assert obs["centroid_y"] == pytest.approx(960 + 100, abs=12)  # the aircraft, below the label


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


def test_zones_are_on_by_default_and_an_empty_list_turns_them_off():
    """CR-018-04: the shipped zones are the code default."""
    frame = _frame()
    frame[20:80, 1180:1400] = _RED
    assert _probe(_tracker(), frame)["px"] == 0
    assert _probe(_tracker(red_mass_exclude_zones_pct=[]), frame)["px"] > 0


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


def test_the_steering_point_is_the_aircraft_100_px_below_the_label():
    frame = _nameplate(_frame(), 960, 700)                 # the marker blob is drawn at y 800
    p = _probe(_wide(), frame)
    cx, cy = p["cluster"]
    assert abs(cy - 700) < 12, "the cluster centre is the drawn label"
    assert p["centroid"] == pytest.approx((cx, cy + 100), abs=0.5)


def test_the_offset_comes_from_the_config():
    p = _probe(_wide(red_mass_aim_offset_px=60), _nameplate(_frame(), 960, 700))
    cx, cy = p["cluster"]
    assert p["centroid"] == pytest.approx((cx, cy + 60), abs=0.5)


def test_the_default_offset_is_100_px_below():
    p = _probe(_wide(), _nameplate(_frame(), 960, 700, with_marker=False))
    cx, cy = p["cluster"]
    assert p["centroid"] == pytest.approx((cx, cy + 100), abs=0.5)


def test_a_red_shape_above_or_beside_the_label_does_not_move_the_steering_point():
    """The rule this replaced took the largest red shape 162-287 px above the label for the
    marker. No frame has one there, and a shape there must not pull the point."""
    frame = _nameplate(_frame(), 960, 700, with_marker=False)
    base = _probe(_wide(), frame)["centroid"]
    cv2.circle(frame, (960, 480), 16, _RED, -1)            # 220 px above the label
    cv2.circle(frame, (1080, 520), 14, _RED, -1)
    assert _probe(_wide(), frame)["centroid"] == pytest.approx(base, abs=0.5)


def test_a_marker_below_the_label_does_not_move_the_steering_point_either():
    with_marker = _probe(_wide(), _nameplate(_frame(), 960, 700))["centroid"]
    without = _probe(_wide(), _nameplate(_frame(), 960, 700, with_marker=False))["centroid"]
    assert with_marker == pytest.approx(without, abs=0.5)


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
    p = _probe(_wide(), frame)
    assert p["gate"] == "reject" and p["glyphs"] == 12


def test_a_rejected_tick_reports_the_best_cluster_count():
    frame = _nameplate(_frame(), 960, 600, 15, False)
    p = _probe(_wide(), frame)
    assert (p["gate"], p["glyphs"], p["clusters"]) == ("reject", 15, 0)


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


def test_trackpick_names_a_keep_and_the_strict_pass(caplog):
    t = _locked()
    with caplog.at_level("DEBUG", logger="wingman.tracker"):
        t.update(_nameplate(_frame(), 990, 720, n_glyphs=10), ts=0.33)
        t.update(_nameplate(_frame(), 990, 720, with_marker=False), ts=0.66)
    lines = [r.getMessage() for r in caplog.records if "TRACKPICK:" in r.getMessage()]
    assert "path=keep" in lines[0] and "gate=keep" in lines[0]
    assert "path=redmass" in lines[1] and "gate=pass" in lines[1]

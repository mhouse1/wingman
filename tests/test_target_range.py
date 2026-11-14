"""The gun's range gate (operator, 2026-11-13: "machine gun activation should
only be allowed when the target distance is less than 2km away").

The fixtures in tests/fixtures/gun_range/ are a curated, enumerated corpus:
the operator's own frame (an F-106 at 1.3 km, centred, which must open the
gun) and 400 x 260 px windows centred on a nameplate's glyph cluster, each
range checked by eye. Do not glob the directory into a new test without
checking what was added.
"""

import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

import wingman.controller as controller_module
import wingman.target_range as tr
from wingman.controller import Controller, FIRE_MACHINE_GUN
from wingman.controller_config import ControllerConfig
from wingman.target_range import (
    RangeReading, TargetRangeReader, parse_range_km, range_row_crops,
)
from wingman.tracker import TargetTracker

_FIX = Path(__file__).parent / "fixtures" / "gun_range"
_FULL = _FIX / "f106_1p3km_full.png"
# Window file -> range on the nameplate, read by eye.
_WINDOWS = {
    "window_0p95km.png": 0.95, "window_1p0km.png": 1.0, "window_3p1km.png": 3.1,
    "window_3p2km.png": 3.2, "window_4p4km.png": 4.4, "window_4p7km.png": 4.7,
    "window_5p2km.png": 5.2, "window_5p3km.png": 5.3, "window_8p9km.png": 8.9,
    "window_10p4km.png": 10.4, "window_14p0km.png": 14.0,
}
_WINDOW_CLUSTER = (200, 130)


# -- parsing -----------------------------------------------------------------

@pytest.mark.parametrize("text,km", [
    ("1.3km", 1.3), ("0.95km", 0.95), ("10.4km", 10.4), ("1.3km7", 1.3),
    ("1.3 km", 1.3),
])
def test_parse_reads_the_game_format(text, km):
    assert parse_range_km(text) == km


@pytest.mark.parametrize("text", [
    "13km",        # the operator's 1.3 km with the point dropped: not 13, not 1.3
    "04kkkkkkk", "0.1977", "[Axel] Ref", "", None, "4kk4749", "1.3",
])
def test_parse_refuses_anything_else(text):
    assert parse_range_km(text) is None


# -- finding the range row ---------------------------------------------------

def _tracked(frame):
    cfg = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
    obs = TargetTracker(cfg).update(frame)
    return obs


def test_tracker_reports_the_nameplate_cluster_on_the_operators_frame():
    obs = _tracked(cv2.imread(str(_FULL)))
    assert obs["visible"] and obs["cluster"] is not None
    cx, cy = obs["cluster"]
    # The F-106's label: name 445-465, range 476-494, type 509-529.
    assert 950 < cx < 1060 and 470 < cy < 500


def test_the_first_crop_is_the_range_row_on_the_operators_frame():
    frame = cv2.imread(str(_FULL))
    crops = range_row_crops(frame, _tracked(frame)["cluster"])
    assert crops
    # 18 rows of glyphs plus a 4 px margin each side.
    assert 20 <= crops[0].shape[0] <= 30


# -- the jet icon left of the range (rev 6) -------------------------------------

# Range rows captured live 2026-11-13 04:32-04:39 (CRUCIBLE), each with a red
# jet icon left of the text that OCR read as a leading digit. File -> the range
# printed on it, read by eye; the OCR said 44.5, 45.4, 46.5 and 49.2.
_ICON_ROWS = {
    "rowcrop_icon_4p5km.png": 4.5, "rowcrop_icon_5p4km.png": 5.4,
    "rowcrop_icon_6p5km.png": 6.5, "rowcrop_icon_9p2km.png": 9.2,
}


def _row_mask(crop):
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return (cv2.inRange(hsv, tr._RED_LO, tr._RED_HI)
            | cv2.inRange(hsv, tr._RED_WRAP_LO, tr._RED_WRAP_HI))


@pytest.mark.parametrize("name", sorted(_ICON_ROWS))
def test_the_text_span_leaves_the_jet_icon_out(name):
    crop = cv2.imread(str(_FIX / name))
    cols = np.nonzero((_row_mask(crop) > 0).sum(axis=0))[0]
    a, b = tr.text_span(_row_mask(crop))
    assert a > cols.min() and b == cols.max()


def test_the_text_span_keeps_every_curated_range_whole():
    for name in _WINDOWS:
        crop = range_row_crops(cv2.imread(str(_FIX / name)), _WINDOW_CLUSTER)[0]
        cols = np.nonzero((_row_mask(crop) > 0).sum(axis=0))[0]
        assert tr.text_span(_row_mask(crop)) == (int(cols.min()), int(cols.max())), name


# -- real OCR on the curated corpus -------------------------------------------

@pytest.fixture(scope="module")
def ocr():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("name,km", sorted(_WINDOWS.items()))
def test_real_ocr_reads_each_range(ocr, name, km):
    frame = cv2.imread(str(_FIX / name))
    assert TargetRangeReader().read(frame, _WINDOW_CLUSTER, reader=ocr).km == km


@pytest.mark.slow
@pytest.mark.parametrize("name,km", sorted(_ICON_ROWS.items()))
def test_real_ocr_reads_the_range_without_the_icon(ocr, name, km):
    crop = cv2.imread(str(_FIX / name))
    a, b = tr.text_span(_row_mask(crop))
    text_crop = crop[:, max(0, a - 6):b + 7]
    res = ocr.recognize(tr._ocr_input(text_crop), detail=1, allowlist=tr._ALLOWLIST)
    assert parse_range_km(res[0][1]) == km, res[0][1]


@pytest.mark.slow
def test_real_ocr_reads_the_operators_target_at_1p3km(ocr):
    frame = cv2.imread(str(_FULL))
    reading = TargetRangeReader().read(frame, _tracked(frame)["cluster"], reader=ocr)
    assert reading.km == 1.3, reading.text


# -- the gate ----------------------------------------------------------------

def _make_ctrl(monkeypatch):
    monkeypatch.setattr(controller_module, "keyboard_module", None)
    ctrl = Controller(
        (0, 0, 1920, 1200), exit_event=threading.Event(),
        config=ControllerConfig(simulate_os_input=True, disable_hotkeys=True),
    )
    ctrl._pursuit_gun_on_centre = True
    ctrl._gun_max_range_km = 2.0
    ctrl._gun_range_max_age_s = 1.5
    ctrl._target_range = TargetRangeReader()
    keys = []
    monkeypatch.setattr(ctrl, "_press_tracking_key", lambda k, a: keys.append(("down", k)))
    monkeypatch.setattr(ctrl, "_release_tracking_key", lambda k, a: keys.append(("up", k)))
    return ctrl, keys


_CLUSTER = (1004.0, 483.0)


def _reading(ctrl, km, age_s=0.0, cluster=_CLUSTER):
    ctrl._target_range._latest = RangeReading(
        km=km, text="" if km is None else "%gkm" % km, cluster=cluster,
        ts=time.monotonic() - age_s, cost_ms=20.0)
    ctrl._gun_cluster = _CLUSTER


def test_the_gun_opens_on_a_centred_target_under_2km(monkeypatch):
    """The operator's example: F-106 centred at 1.3 km."""
    ctrl, keys = _make_ctrl(monkeypatch)
    _reading(ctrl, 1.3)
    ctrl._pursuit_gun_tick(True, 0.046, -0.029)
    assert keys == [("down", FIRE_MACHINE_GUN)]


@pytest.mark.parametrize("km,age,cluster", [
    (3.1, 0.0, _CLUSTER),            # centred but out of range
    (2.0, 0.0, _CLUSTER),            # "less than 2km": 2.0 is not
    (None, 0.0, _CLUSTER),           # no range read
    (1.3, 3.0, _CLUSTER),            # a stale reading
    (1.3, 0.0, (400.0, 300.0)),      # a reading of another nameplate
])
def test_the_gun_stays_off_without_a_fresh_reading_under_2km(monkeypatch, km, age, cluster):
    ctrl, keys = _make_ctrl(monkeypatch)
    _reading(ctrl, km, age, cluster)
    ctrl._pursuit_gun_tick(True, 0.0, 0.0)
    assert keys == []


def test_the_gun_lets_go_when_the_target_opens_past_2km(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    _reading(ctrl, 1.3)
    ctrl._pursuit_gun_tick(True, 0.0, 0.0)
    _reading(ctrl, 2.4)
    ctrl._pursuit_gun_tick(True, 0.0, 0.0)
    assert keys == [("down", FIRE_MACHINE_GUN), ("up", FIRE_MACHINE_GUN)]


def test_range_gate_off_keeps_the_centre_rule(monkeypatch):
    ctrl, keys = _make_ctrl(monkeypatch)
    ctrl._target_range = None
    ctrl._pursuit_gun_tick(True, 0.0, 0.0)
    assert keys == [("down", FIRE_MACHINE_GUN)]


def test_a_miss_does_not_replace_a_good_reading():
    """Live 2026-11-13 03:04:15: a 1.8 km read, then one unreadable picture
    ("770"), let go of the gun 0.5 s into the burst."""
    # One text per picture: read() tries a second row after a miss.
    current = {"text": "1.8km"}

    class Fake:
        def recognize(self, img, detail=1, allowlist=None):
            return [([], current["text"], 0.9)]
    rd = TargetRangeReader(reader_factory=Fake)
    frame = cv2.imread(str(_FULL))
    cl = (1003.95, 482.88)
    for n, text in enumerate(["1.8km", "770"]):
        current["text"] = text
        rd.submit(frame, cl)
        deadline = time.time() + 3.0
        while (rd.reads + rd.misses) <= n and time.time() < deadline:
            time.sleep(0.01)
    rd.stop()
    assert (rd.reads, rd.misses) == (1, 1)
    assert rd.latest(cl, 5.0).km == 1.8


def _r(km, ts, cluster=(929.0, 503.0)):
    return RangeReading(km=km, text="", cluster=cluster, ts=ts, cost_ms=0.0)


def test_an_impossible_jump_is_not_kept():
    """Live 2026-11-13 03:24:11, as logged: 1.2 km, then "81.2km" and "21.2km"
    (a junk digit in front of 1.2), then 1.2 km. Rev 2 let go of the gun on 81.2."""
    rd = TargetRangeReader()
    seq = [(1.2, 11.186), (81.2, 11.341), (21.2, 11.717), (1.2, 12.210)]
    verdicts = [rd.note(_r(km, ts)) for km, ts in seq]
    assert verdicts == ["kept", "jump", "jump", "kept"]
    assert rd.latest((929.0, 503.0), 1.5, now=12.3).km == 1.2


def test_a_misread_drop_does_not_open_the_gun():
    """Live 2026-11-13 03:26:00, as logged: 4.8, 4.7, "1.7km4", 4.5, 4.5. Rev 3
    opened the gun on the 1.7 at about 4.6 km."""
    rd = TargetRangeReader()
    kept_after = []
    verdicts = []
    for km, ts in [(4.8, 0.527), (4.7, 0.702), (1.7, 0.884), (4.5, 1.006), (4.5, 1.162)]:
        verdicts.append(rd.note(_r(km, ts)))
        kept_after.append(rd.latest((929.0, 503.0), 1.5, now=ts).km)
    assert verdicts == ["kept", "kept", "jump", "kept", "kept"]
    assert min(kept_after) > 2.0     # the gate never saw a range under 2 km


def test_a_junk_first_reading_does_not_lock_out_the_real_ones():
    rd = TargetRangeReader()
    seq = [(81.2, 0.0), (1.2, 0.15), (1.2, 0.3)]
    assert [rd.note(_r(km, ts)) for km, ts in seq] == ["kept", "jump", "kept"]
    assert rd.latest((929.0, 503.0), 1.5, now=0.4).km == 1.2


def test_an_out_of_range_reading_under_10km_always_replaces_an_in_range_one():
    """Live 03:01:39: a 3.0 km read 1 s after a kept 1.4. Holding it back would
    keep the gun allowed on a target that may be out of range."""
    rd = TargetRangeReader()
    assert [rd.note(_r(km, ts)) for km, ts in [(1.4, 0.0), (3.0, 0.3)]] == ["kept", "kept"]
    assert rd.latest((929.0, 503.0), 1.5, now=0.4).km == 3.0


def test_a_systematic_junk_digit_is_dropped():
    """Live 04:10:31-34, as logged: 6.3, 6.2, "76.0km", "75.7km", 5.0 from
    successive pictures. Rev 4 kept 75.7 (it agreed with 76.0) and held back
    the real 5.0."""
    rd = TargetRangeReader()
    seq = [("06.3km", 590.411), ("06.2km", 590.562), ("76.0km", 590.870),
           ("75.7km", 591.584), ("5.0km", 592.970)]
    verdicts = []
    for text, ts in seq:
        r = RangeReading(km=parse_range_km(text), text=text, cluster=(949.0, 506.0),
                         ts=ts, cost_ms=0.0)
        verdicts.append(rd.note(r))
    assert verdicts == ["kept", "kept", "strip", "strip", "kept"]
    assert rd.latest((949.0, 506.0), 1.5, now=593.0).km == 5.0


def test_a_far_reading_that_strips_to_nothing_consistent_is_held_back():
    rd = TargetRangeReader()
    for text, ts in [("1.2km", 0.0), ("81.2km", 0.15)]:
        rd.note(RangeReading(km=parse_range_km(text), text=text,
                             cluster=(929.0, 503.0), ts=ts, cost_ms=0.0))
    # "81.2" strips to "1.2", which fits: kept as 1.2.
    assert rd.latest((929.0, 503.0), 1.5, now=0.2).km == 1.2
    assert rd.note(RangeReading(km=44.8, text="44.8km", cluster=(929.0, 503.0),
                                ts=0.3, cost_ms=0.0)) == "jump"   # 4.8 does not fit 1.2


def test_rereads_of_one_picture_do_not_agree():
    """Live 03:47:57: 8.1 km kept, then "31.2km" three times from one repeated
    picture (same picture time). Rev 3 kept the 31.2."""
    rd = TargetRangeReader()
    seq = [(8.1, 56.233), (31.2, 57.232), (31.2, 57.232), (31.2, 57.232)]
    assert [rd.note(_r(km, ts)) for km, ts in seq] == ["kept", "jump", "jump", "jump"]


def test_a_repeated_picture_is_not_submitted_twice():
    calls = []

    class Fake:
        def recognize(self, img, detail=1, allowlist=None):
            calls.append(1)
            return [([], "8.1km", 0.9)]
    rd = TargetRangeReader(reader_factory=Fake)
    frame = cv2.imread(str(_FULL))
    rd.submit(frame, (1003.95, 482.88), 5.0)
    deadline = time.time() + 3.0
    while rd.reads < 1 and time.time() < deadline:
        time.sleep(0.01)
    rd.submit(frame, (1003.95, 482.88), 5.0)
    time.sleep(0.2)
    rd.stop()
    assert (rd.reads, rd.repeats) == (1, 1)


def test_two_agreeing_far_readings_replace_the_kept_one():
    rd = TargetRangeReader()
    seq = [(4.0, 0.0), (12.0, 0.2), (12.1, 0.4)]
    assert [rd.note(_r(km, ts)) for km, ts in seq] == ["kept", "jump", "kept"]


def test_a_closing_target_and_a_new_nameplate_are_kept():
    rd = TargetRangeReader()
    closing = [rd.note(_r(3.1 - 0.1 * i, 0.15 * i)) for i in range(10)]
    assert closing == ["kept"] * 10
    # Another nameplate far across the screen is a new target, not a jump.
    assert rd.note(_r(8.9, 1.6, cluster=(300.0, 300.0))) == "kept"


def test_worker_reads_a_submitted_picture_and_stops():
    class Fake:
        def recognize(self, img, detail=1, allowlist=None):
            return [([], "1.3km", 0.9)]
    rd = TargetRangeReader(reader_factory=Fake)
    frame = cv2.imread(str(_FULL))
    rd.submit(frame, (1003.95, 482.88))
    deadline = time.time() + 3.0
    while rd.latest((1003.95, 482.88), 5.0) is None and time.time() < deadline:
        time.sleep(0.01)
    assert rd.latest((1003.95, 482.88), 5.0).km == 1.3
    rd.stop()
    assert not rd._thread.is_alive()

"""Selection geometry and saving in tests/screenshot.py (`make screenshot`)."""

import pathlib
import sys

import cv2
import numpy as np
import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "tests"))

import screenshot  # noqa: E402  — needs the sys.path entry above

_SCREEN = {"left": 0, "top": 0, "width": 1920, "height": 1080}
# Second monitor to the left of the primary: the virtual desktop starts at x=-1920.
_DUAL = {"left": -1920, "top": 0, "width": 3840, "height": 1080}


def test_drag_maps_to_desktop_region():
    assert screenshot._selection_to_region(10, 20, 110, 70, _SCREEN) == \
        {"left": 10, "top": 20, "width": 100, "height": 50}


def test_drag_corners_in_any_order():
    assert screenshot._selection_to_region(110, 70, 10, 20, _SCREEN) == \
        {"left": 10, "top": 20, "width": 100, "height": 50}


def test_overlay_origin_offsets_negative_desktop():
    # Overlay x=100 on a desktop starting at -1920 is desktop x=-1820.
    assert screenshot._selection_to_region(100, 0, 300, 100, _DUAL) == \
        {"left": -1820, "top": 0, "width": 200, "height": 100}


def test_drag_clamped_to_screen():
    assert screenshot._selection_to_region(-50, -50, 5000, 5000, _SCREEN) == _SCREEN


def test_click_without_drag_is_not_a_selection():
    assert screenshot._selection_to_region(50, 50, 51, 52, _SCREEN) is None


def test_borders_frame_region_from_outside():
    region = {"left": 100, "top": 200, "width": 300, "height": 50}
    strips = screenshot._border_rects(region, t=2)
    assert strips == [(98, 198, 304, 2), (98, 250, 304, 2), (98, 200, 2, 50), (400, 200, 2, 50)]
    for x, y, w, h in strips:  # no strip overlaps the captured area
        overlaps = (x < 400 and x + w > 100) and (y < 250 and y + h > 200)
        assert not overlaps


def test_geometry_keeps_negative_positions_absolute():
    assert screenshot._geometry(-1920, 0, 10, 20) == "10x20+-1920+0"


class _FakeSct:
    def grab(self, region):
        return np.zeros((region["height"], region["width"], 4), dtype=np.uint8)


def test_save_writes_region_as_bgr_png(tmp_path, monkeypatch):
    monkeypatch.setattr(screenshot, "_OUTPUT_DIR", tmp_path)
    path = screenshot._save(_FakeSct(), {"left": 0, "top": 0, "width": 100, "height": 50})
    assert path is not None and path.parent == tmp_path
    assert path.name.startswith("screenshot_") and path.suffix == ".png"
    assert cv2.imread(str(path)).shape == (50, 100, 3)


def test_region_maps_into_stream_at_origin():
    region = {"left": 1624, "top": 478, "width": 1524, "height": 751}
    assert screenshot._region_in_stream(region, (0, 0), (1.0, 1.0), (3840, 1600)) == \
        (1624, 478, 1524, 751)


def test_region_offset_and_scaled_into_stream():
    # Stream shows the right-hand monitor at desktop x=1920, at 2x its desktop size.
    region = {"left": 2020, "top": 10, "width": 100, "height": 50}
    assert screenshot._region_in_stream(region, (1920, 0), (2.0, 2.0), (3840, 2160)) == \
        (200, 20, 200, 100)


def test_region_off_the_streamed_monitor_is_rejected():
    region = {"left": 100, "top": 0, "width": 100, "height": 50}
    with pytest.raises(ValueError):
        screenshot._region_in_stream(region, (1920, 0), (1.0, 1.0), (1920, 1080))


def test_stream_frame_placed_on_desktop_at_its_monitor():
    # Stream shows the right-hand monitor of a desktop that starts at x=-1920.
    frame = np.full((1080, 1920, 3), 200, dtype=np.uint8)
    desktop = screenshot._place_on_desktop(frame, (0, 0), (1.0, 1.0), _DUAL)
    assert desktop.shape == (1080, 3840, 3)
    assert desktop[:, :1920].max() == 0 and desktop[:, 1920:].min() == 200


def test_scaled_stream_frame_brought_back_to_desktop_pixels():
    frame = np.full((2160, 3840, 3), 200, dtype=np.uint8)  # 2x the 1920x1080 desktop
    desktop = screenshot._place_on_desktop(frame, (0, 0), (2.0, 2.0), _SCREEN)
    assert desktop.shape == (1080, 1920, 3) and desktop.min() == 200

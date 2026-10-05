"""Position from the game's full map (Design 017, "The full map").

The reader must say nothing unless it is sure. A position that is wrong sends
a survey to the wrong part of the arena; a map taken for closed when it is
open leaves the forward view covered.
"""

import math
import pathlib

import cv2
import numpy as np
import pytest
import yaml

from wingman.config_schema import schema_default
from wingman.full_map import FullMapFix, FullMapReader, fmt_fix

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_REAL = _ROOT / "docs" / "hldd" / "017-terrain-map-from-flight-footage" / (
    "full-map-with-the-m-key-20261004-031914.jpg")

_SKY = (235, 190, 140)          # BGR: a pale blue scene behind the map
_RING = (70, 55, 45)            # the dark compass ring
_DISC = (60, 75, 95)            # brown terrain inside it
_ORANGE = cv2.cvtColor(np.uint8([[[20, 220, 240]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()
_GREEN = cv2.cvtColor(np.uint8([[[60, 200, 180]]]), cv2.COLOR_HSV2BGR)[0, 0].tolist()


def _frame(size=(960, 600), icon=(-0.33, -0.69), cone=134.0, n=True, ring=True,
           extra_white=None, marker=None):
    """A picture with the full map open: ring, disc, N, own icon and view cone.

    `icon` and `extra_white` are (east, north) in radii; `cone` is a heading or
    None; `marker` is a small green friendly marker at (east, north).
    """
    width, height = size
    img = np.full((height, width, 3), _SKY, np.uint8)
    cx, cy, radius = (width - 1) / 2.0, (height - 1) / 2.0, 0.3944 * height

    def at(east, north):
        return int(round(cx + east * radius)), int(round(cy - north * radius))

    if ring:
        cv2.circle(img, (int(cx), int(cy)), int(1.10 * radius), _RING, -1)
        cv2.circle(img, (int(cx), int(cy)), int(radius), _DISC, -1)
    if n:
        side = max(4, int(round(math.sqrt(0.004) * radius)))        # the N's area, as a square
        x, y = at(0.0, 1.052)
        cv2.rectangle(img, (x - side // 2, y - side // 2), (x + side // 2, y + side // 2), _ORANGE, -1)
    dot = max(2, int(round(math.sqrt(0.0005 / math.pi) * radius)))
    if cone is not None and icon is not None:
        east = icon[0] + 0.2 * math.sin(math.radians(cone))
        north = icon[1] + 0.2 * math.cos(math.radians(cone))
        cv2.circle(img, at(east, north), int(0.08 * radius), _GREEN, -1)
    if marker is not None:
        cv2.circle(img, at(*marker), max(2, int(0.02 * radius)), _GREEN, -1)
    for spot in (icon, extra_white):
        if spot is not None:
            cv2.circle(img, at(*spot), dot, (255, 255, 255), -1)
    return img


@pytest.mark.parametrize("size", [(960, 600), (1920, 1200)])
def test_it_reads_where_the_aircraft_is_and_which_way_it_points(size):
    fix = FullMapReader().read(_frame(size=size, icon=(-0.33, -0.69), cone=134.0))
    assert fix is not None
    assert fix.east == pytest.approx(-0.33, abs=0.01)
    assert fix.north == pytest.approx(-0.69, abs=0.01)
    assert fix.radius == pytest.approx(math.hypot(0.33, 0.69), abs=0.01)
    assert fix.heading == pytest.approx(134.0, abs=3.0)


@pytest.mark.parametrize("icon, cone", [((0.0, 0.0), 0.0), ((0.6, 0.5), 270.0), ((-0.7, 0.2), 45.0)])
def test_east_is_right_and_north_is_up(icon, cone):
    fix = FullMapReader().read(_frame(icon=icon, cone=cone))
    assert (fix.east, fix.north) == (pytest.approx(icon[0], abs=0.01), pytest.approx(icon[1], abs=0.01))
    assert abs((fix.heading - cone + 180.0) % 360.0 - 180.0) <= 3.0


def test_a_picture_without_the_map_is_not_a_map():
    reader = FullMapReader()
    scene = np.full((600, 960, 3), _SKY, np.uint8)
    cv2.circle(scene, (300, 200), 3, (255, 255, 255), -1)           # a white speck in the sky
    assert not reader.shown(scene) and not reader.covering(scene) and reader.read(scene) is None
    assert not reader.shown(None) and reader.read(None) is None


def test_a_map_that_is_still_drawing_is_not_read():
    """Measured 2026-10-04 16:43: 0.29 s after the key the disc is on screen,
    the N is not drawn yet and the own icon is still moving to its place."""
    reader = FullMapReader()
    arriving = _frame(n=False)
    assert reader.covering(arriving), "the ring is there: something is over the view"
    assert not reader.shown(arriving) and reader.read(arriving) is None


def test_orange_rock_at_the_top_of_the_picture_is_not_the_map():
    """Canyon rock is orange too. The N only counts when it is alone there."""
    reader = FullMapReader()
    rock = np.full((600, 960, 3), _SKY, np.uint8)
    rock[:160] = _ORANGE                                 # a mesa across the top of the picture
    assert not reader.shown(rock) and reader.read(rock) is None
    spur = _frame(ring=False, icon=None, cone=None)      # an N-sized patch ...
    cv2.circle(spur, (480 + 30, 51), 14, _ORANGE, -1)     # ... with more orange beside it
    assert not reader.shown(spur)


def test_the_map_over_bright_cloud_is_still_the_map():
    """Thirteenth flight, 2026-10-04 18:01:11: just after a spawn, over white
    cloud, the see-through ring was not dark enough for the check of the time,
    and an open map went unrecognised and unclosed for ten seconds, twice."""
    reader = FullMapReader()
    bright = _frame(icon=(-0.05, -0.48), cone=44.0)
    cx, cy, radius = 479.5, 299.5, 0.3944 * 600
    yy, xx = np.mgrid[:600, :960]
    r = np.hypot(xx - cx, yy - cy)
    bright[(r > radius) & (r <= 1.10 * radius) & (bright == _RING).all(axis=2)] = (190, 185, 180)
    assert not reader.covering(bright), "the ring is too pale to call dark"
    assert reader.shown(bright)
    fix = reader.read(bright)
    assert fix is not None and fix.east == pytest.approx(-0.05, abs=0.01)


def test_two_white_marks_of_the_icons_size_give_no_position():
    """A wrong position is worse than none."""
    assert FullMapReader().read(_frame(extra_white=(0.4, 0.3))) is None


def test_without_a_view_cone_there_is_a_position_and_no_heading():
    fix = FullMapReader().read(_frame(cone=None))
    assert fix is not None and fix.heading is None
    assert fix.east == pytest.approx(-0.33, abs=0.01)


def test_a_friendly_marker_beside_the_icon_is_not_the_cone():
    """Friendly markers are green too, and far smaller than the cone."""
    fix = FullMapReader().read(_frame(cone=None, marker=(-0.25, -0.69)))
    assert fix is not None and fix.heading is None


def test_switched_off_it_reads_nothing():
    reader = FullMapReader({"enabled": False})
    assert reader.enabled is False


def test_the_real_picture_from_the_design():
    """The operator's screenshot of 2026-10-04 03:19, measured by hand in
    Design 017: 0.064 radii west, 0.732 south, heading 129."""
    if not _REAL.exists():
        pytest.skip("the design's picture is not in this checkout")
    frame = cv2.imread(str(_REAL))
    reader = FullMapReader()
    assert reader.shown(frame)
    fix = reader.read(frame)
    assert fix.east == pytest.approx(-0.064, abs=0.01)
    assert fix.north == pytest.approx(-0.732, abs=0.01)
    assert fix.heading == pytest.approx(129.0, abs=4.0)


def test_the_log_form():
    assert fmt_fix(None) == "n/a"
    assert fmt_fix(FullMapFix(east=-0.33, north=-0.685, heading=134.2)) == (
        "east=-0.330 north=-0.685 r=0.76 cone=134")
    assert fmt_fix(FullMapFix(east=0.5, north=0.0)) == "east=+0.500 north=+0.000 r=0.50 cone=n/a"


def test_the_shipped_config_declares_every_setting_at_its_schema_default():
    with open(_ROOT / "wingman" / "config.yaml", encoding="utf-8") as f:
        shipped = yaml.safe_load(f)["full_map"]
    reader = FullMapReader(shipped)
    assert reader.enabled is schema_default("full_map.enabled")
    for key, value in shipped.items():
        assert value == schema_default(f"full_map.{key}"), key

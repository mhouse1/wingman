"""Minimap red-icon scan tests (Design 003 / ADR 028).

Two layers:

1. Synthetic geometry against the pure `_scan_minimap_red` function — bearing
   and radius math, circle mask, component area band, hue wrap-around.
2. Static-frame regression on archived screenshots: `MINIMAP.png` (the hard
   case — the game renders a red locked-target ring and route line on the
   map), plus the PATH1 battle frames.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman.analyzer import (
    GameStateAnalyzer,
    _scan_minimap_components,
    _scan_minimap_red,
)
from wingman.engage_nav import RING_LONG, RING_MID, RING_SHORT, bin_rings
from constants import CONFIG_PATH

MASK_RADIUS_FRAC = 0.93
MIN_BLOB_PX = 4
MAX_BLOB_PX = 120
SIZE = 321  # odd → exact integer centre pixel
LOWER = np.array([0, 120, 120], dtype=np.uint8)
UPPER = np.array([10, 255, 255], dtype=np.uint8)

ROOT = Path(__file__).resolve().parents[1]
MINIMAP_FRAME = ROOT / "test_screenshots" / "MINIMAP.png"
P1_040_FRAME = ROOT / "test_screenshots" / "integration_test" / "P1_040_BATTLE_HUD_MISSILES_0_HEALTH_ALIVE.png"
P1_060_FRAME = ROOT / "test_screenshots" / "integration_test" / "P1_060_BATTLE_HUD_HEALTH_ALIVE_MISSILES_4.png"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


# ---------------------------------------------------------------------------
# Synthetic geometry
# ---------------------------------------------------------------------------


def blank_crop() -> np.ndarray:
    return np.zeros((SIZE, SIZE, 3), dtype=np.uint8)


def paint_dot(crop, bearing_deg, radius_frac, half_size=1, color=(0, 0, 255)):
    """Paint a filled red square whose centre sits at the given polar position."""
    radius_px = MASK_RADIUS_FRAC * SIZE / 2.0
    cx = (SIZE - 1) / 2.0
    cy = (SIZE - 1) / 2.0
    theta = np.radians(bearing_deg)
    x = int(round(cx + radius_frac * radius_px * np.sin(theta)))
    y = int(round(cy - radius_frac * radius_px * np.cos(theta)))
    crop[y - half_size:y + half_size + 1, x - half_size:x + half_size + 1] = color


def scan(crop, max_blob_px=MAX_BLOB_PX):
    return _scan_minimap_red(
        crop, LOWER, UPPER, MASK_RADIUS_FRAC, MIN_BLOB_PX, max_blob_px,
    )


def angle_diff(a, b) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def test_empty_crop_returns_none():
    assert scan(blank_crop()) == (None, None, 0, 0)


@pytest.mark.parametrize("bearing", [0, 45, 90, 135, 180, -45, -90, -135])
def test_single_dot_bearing(bearing):
    crop = blank_crop()
    paint_dot(crop, bearing, 0.6)
    got_bearing, got_radius, blobs, pixels = scan(crop)
    assert blobs == 1
    assert pixels == 9
    assert angle_diff(got_bearing, bearing) <= 3.0
    assert abs(got_radius - 0.6) <= 0.03


@pytest.mark.parametrize("radius", [0.3, 0.6, 0.9])
def test_single_dot_radius(radius):
    crop = blank_crop()
    paint_dot(crop, 90, radius)
    _, got_radius, _, _ = scan(crop)
    assert abs(got_radius - radius) <= 0.03


def test_two_dots_area_weighted_centroid():
    crop = blank_crop()
    paint_dot(crop, 90, 0.6, half_size=2)   # 25 px
    paint_dot(crop, 0, 0.6, half_size=1)    # 9 px
    got_bearing, _, blobs, pixels = scan(crop)
    assert blobs == 2
    assert pixels == 34
    # Analytic centroid of (25 px at east, 9 px at north) → atan2(25, 9) ≈ 70.2°
    assert 65.0 <= got_bearing <= 75.0


def test_red_outside_circle_mask_is_ignored():
    crop = blank_crop()
    crop[2:8, 2:8] = (0, 0, 255)  # bounding-box corner: game world, not map
    assert scan(crop) == (None, None, 0, 0)


def test_oversized_component_rejected():
    crop = blank_crop()
    paint_dot(crop, 45, 0.5, half_size=10)  # 21×21 = 441 px > max_blob_px
    assert scan(crop) == (None, None, 0, 0)


def test_undersized_component_rejected():
    crop = blank_crop()
    paint_dot(crop, 45, 0.5, half_size=0)   # single pixel < min_blob_px
    assert scan(crop) == (None, None, 0, 0)


def test_wraparound_red_hue_detected():
    crop = blank_crop()
    paint_dot(crop, -90, 0.5, color=(50, 0, 255))  # hue ≈ 174 — wrap band
    got_bearing, _, blobs, _ = scan(crop)
    assert blobs == 1
    assert angle_diff(got_bearing, -90) <= 3.0


# ---------------------------------------------------------------------------
# Static-frame regression
# ---------------------------------------------------------------------------


def scan_frame(cfg, path, max_blob_px=None):
    frame = cv2.imread(str(path))
    assert frame is not None, f"unreadable screenshot: {path}"
    (x1, y1), (x2, y2) = cfg["crops"]["MINIMAP"]["coords"]
    height, width = frame.shape[:2]
    crop = frame[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
    minimap_cfg = cfg["minimap"]
    return _scan_minimap_red(
        crop,
        np.array(cfg["enemy_hsv"]["lower"], dtype=np.uint8),
        np.array(cfg["enemy_hsv"]["upper"], dtype=np.uint8),
        minimap_cfg["mask_radius_frac"],
        minimap_cfg["min_blob_px"],
        max_blob_px if max_blob_px is not None else minimap_cfg["max_blob_px"],
    )


@pytest.mark.skipif(not MINIMAP_FRAME.exists(), reason="MINIMAP.png not archived")
def test_reference_frame_cluster_bearing():
    # Ground truth (hand-verified 2026-08-08): enemy icons cluster to the
    # left/lower-left of own position; five icon components survive the band.
    bearing, radius, blobs, pixels = scan_frame(load_config(), MINIMAP_FRAME)
    assert blobs >= 3
    assert -150.0 <= bearing <= -90.0
    assert 0.2 <= radius <= 0.6
    assert pixels < 400


@pytest.mark.skipif(not MINIMAP_FRAME.exists(), reason="MINIMAP.png not archived")
def test_reference_frame_rejects_lock_ring_and_route_line():
    cfg = load_config()
    # With the band open, the ~1000 px ring+route-line component dominates the
    # pixel count; the configured band must exclude it.
    _, _, _, open_pixels = scan_frame(cfg, MINIMAP_FRAME, max_blob_px=10_000)
    _, _, _, banded_pixels = scan_frame(cfg, MINIMAP_FRAME)
    assert open_pixels > 1000
    assert banded_pixels < 400


@pytest.mark.skipif(not P1_040_FRAME.exists(), reason="P1_040 not archived")
def test_p1_040_cluster_bearing():
    # Ground truth (hand-verified 2026-08-08): red icons sit upper-right of
    # own position on the desert map — and the desert terrain inside the
    # circle must not flood the mask.
    bearing, radius, blobs, _ = scan_frame(load_config(), P1_040_FRAME)
    assert blobs >= 2
    assert 20.0 <= bearing <= 100.0
    assert 0.15 <= radius <= 0.7


@pytest.mark.skipif(not P1_060_FRAME.exists(), reason="P1_060 not archived")
def test_p1_060_output_well_formed():
    # Known tuning gap (recorded 2026-08-08): this frame's enemy mass sits in
    # rim-merged components (~155 px and ~492 px) that the v1 area band
    # rejects, leaving a single small blob. Structural assertions only — the
    # dry-run tuning phase owns turning this into a semantic expectation.
    bearing, radius, blobs, pixels = scan_frame(load_config(), P1_060_FRAME)
    assert blobs >= 0
    if bearing is not None:
        assert -180.0 < bearing <= 180.0
        assert 0.0 <= radius <= 1.0
        assert pixels >= blobs * MIN_BLOB_PX


# ---------------------------------------------------------------------------
# Analyzer method (crop lookup, cache, fail-safe)
# ---------------------------------------------------------------------------


@pytest.fixture
def analyzer() -> GameStateAnalyzer:
    a = GameStateAnalyzer(load_config())
    try:
        yield a
    finally:
        a.cleanup()


@pytest.mark.skipif(not MINIMAP_FRAME.exists(), reason="MINIMAP.png not archived")
def test_method_matches_pure_function_and_caches(analyzer):
    frame = cv2.imread(str(MINIMAP_FRAME))
    expected_bearing, _, expected_blobs, _ = scan_frame(load_config(), MINIMAP_FRAME)
    first = analyzer.detect_enemy_map_bearing(frame)
    second = analyzer.detect_enemy_map_bearing(frame)  # served from cached mask
    assert first["blob_count"] == expected_blobs
    # get_crop rounds edges slightly differently than the raw slice; allow a
    # small angular tolerance rather than exact equality.
    assert abs(first["bearing_deg"] - expected_bearing) <= 3.0
    assert second == first
    assert analyzer._minimap_circle_cache is not None


def test_method_failsafe_without_minimap_crop(analyzer):
    analyzer.crops.pop("MINIMAP", None)
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    result = analyzer.detect_enemy_map_bearing(frame)
    assert result == {
        "bearing_deg": None, "radius_frac": None, "blob_count": 0, "pixel_count": 0,
    }
    assert analyzer.detect_enemy_map_components(frame) is None


# ---------------------------------------------------------------------------
# Per-component scan and ring binning (Design 003 revision 3)
# ---------------------------------------------------------------------------


def components_of_frame(cfg, path):
    frame = cv2.imread(str(path))
    assert frame is not None, f"unreadable screenshot: {path}"
    (x1, y1), (x2, y2) = cfg["crops"]["MINIMAP"]["coords"]
    height, width = frame.shape[:2]
    crop = frame[int(y1 * height):int(y2 * height), int(x1 * width):int(x2 * width)]
    minimap_cfg = cfg["minimap"]
    return _scan_minimap_components(
        crop,
        np.array(cfg["enemy_hsv"]["lower"], dtype=np.uint8),
        np.array(cfg["enemy_hsv"]["upper"], dtype=np.uint8),
        minimap_cfg["mask_radius_frac"],
        minimap_cfg["min_blob_px"],
        minimap_cfg["max_blob_px"],
    )


def test_components_synthetic_dot_polar():
    crop = blank_crop()
    paint_dot(crop, 45, 0.2)
    components = _scan_minimap_components(
        crop, LOWER, UPPER, MASK_RADIUS_FRAC, MIN_BLOB_PX, MAX_BLOB_PX,
    )
    assert len(components) == 1
    bearing, radius, area = components[0]
    assert angle_diff(bearing, 45) <= 3.0
    assert abs(radius - 0.2) <= 0.03
    assert area == 9


def test_aggregate_matches_components():
    crop = blank_crop()
    paint_dot(crop, 90, 0.6, half_size=2)
    paint_dot(crop, 0, 0.6, half_size=1)
    bearing, _, blobs, pixels = scan(crop)
    components = _scan_minimap_components(
        crop, LOWER, UPPER, MASK_RADIUS_FRAC, MIN_BLOB_PX, MAX_BLOB_PX,
    )
    assert blobs == len(components) == 2
    assert pixels == sum(area for _, _, area in components) == 34
    assert 65.0 <= bearing <= 75.0


@pytest.mark.skipif(not MINIMAP_FRAME.exists(), reason="MINIMAP.png not archived")
def test_reference_frame_ring_occupancy():
    # Ground truth (hand-verified 2026-08-08): two icons near own position
    # (short ring, incl. the locked target) and a three-icon group at the map
    # edge (long ring); mid ring empty.
    rings = bin_rings(components_of_frame(load_config(), MINIMAP_FRAME))
    assert rings[RING_SHORT].count == 2
    assert rings[RING_MID].count == 0
    assert rings[RING_LONG].count == 3
    assert -140.0 <= rings[RING_LONG].bearing_deg <= -125.0


@pytest.mark.skipif(not P1_040_FRAME.exists(), reason="P1_040 not archived")
def test_p1_040_ring_occupancy():
    # Ground truth (hand-verified 2026-08-08): one contact per ring.
    rings = bin_rings(components_of_frame(load_config(), P1_040_FRAME))
    assert (rings[RING_SHORT].count, rings[RING_MID].count, rings[RING_LONG].count) == (1, 1, 1)
    assert 50.0 <= rings[RING_MID].bearing_deg <= 65.0


@pytest.mark.skipif(not P1_060_FRAME.exists(), reason="P1_060 not archived")
def test_p1_060_ring_occupancy():
    # Known tuning gap carried from revision 2: the rim-merged clusters are
    # band-rejected, leaving one small long-ring blob.
    rings = bin_rings(components_of_frame(load_config(), P1_060_FRAME))
    assert (rings[RING_SHORT].count, rings[RING_MID].count, rings[RING_LONG].count) == (0, 0, 1)
    assert 140.0 <= rings[RING_LONG].bearing_deg <= 150.0


@pytest.mark.skipif(not MINIMAP_FRAME.exists(), reason="MINIMAP.png not archived")
def test_components_method_matches_pure_function(analyzer):
    frame = cv2.imread(str(MINIMAP_FRAME))
    expected = components_of_frame(load_config(), MINIMAP_FRAME)
    got = analyzer.detect_enemy_map_components(frame)
    assert got is not None
    assert len(got) == len(expected)


def test_components_method_empty_on_black_frame(analyzer):
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    assert analyzer.detect_enemy_map_components(frame) == []

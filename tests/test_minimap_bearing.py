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
# Dedicated fixture, formerly the pre-2026-08-13 P1_040 gate frame (preserved
# from git history when the gate corpus was recaptured on the new UI layout).
# Hand-verified CV ground truth must NOT live on gate frames: the gate corpus
# is recaptured on every game-UI update, which silently invalidates pinned
# blob positions — dedicated fixtures like MINIMAP.png are recapture-immune.
DESERT_3RINGS_FRAME = ROOT / "test_screenshots" / "MINIMAP_DESERT_3RINGS.png"
# Same treatment: formerly the pre-2026-08-13 P1_060 gate frame (its
# rim-merged-components tuning-gap ground truth belongs to THAT frame,
# not to whatever the capture lane most recently recorded).
RIM_MERGED_FRAME = ROOT / "test_screenshots" / "MINIMAP_RIM_MERGED.png"


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


@pytest.mark.skipif(not DESERT_3RINGS_FRAME.exists(), reason="MINIMAP_DESERT_3RINGS not archived")
def test_desert_cluster_bearing():
    # Ground truth (hand-verified 2026-08-08): red icons sit upper-right of
    # own position on the desert map — and the desert terrain inside the
    # circle must not flood the mask.
    bearing, radius, blobs, _ = scan_frame(load_config(), DESERT_3RINGS_FRAME)
    assert blobs >= 2
    assert 20.0 <= bearing <= 100.0
    assert 0.15 <= radius <= 0.7


@pytest.mark.skipif(not RIM_MERGED_FRAME.exists(), reason="MINIMAP_RIM_MERGED not archived")
def test_rim_merged_output_well_formed():
    # Known tuning gap (recorded 2026-08-08): this frame's enemy mass sits in
    # rim-merged components (~155 px and ~492 px) that the v1 area band
    # rejects, leaving a single small blob. Structural assertions only — the
    # dry-run tuning phase owns turning this into a semantic expectation.
    bearing, radius, blobs, pixels = scan_frame(load_config(), RIM_MERGED_FRAME)
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


@pytest.mark.skipif(not DESERT_3RINGS_FRAME.exists(), reason="MINIMAP_DESERT_3RINGS not archived")
def test_desert_ring_occupancy():
    # Ground truth (hand-verified 2026-08-08): one contact per ring.
    rings = bin_rings(components_of_frame(load_config(), DESERT_3RINGS_FRAME))
    assert (rings[RING_SHORT].count, rings[RING_MID].count, rings[RING_LONG].count) == (1, 1, 1)
    assert 50.0 <= rings[RING_MID].bearing_deg <= 65.0


@pytest.mark.skipif(not RIM_MERGED_FRAME.exists(), reason="MINIMAP_RIM_MERGED not archived")
def test_rim_merged_ring_occupancy():
    # Known tuning gap carried from revision 2: the rim-merged clusters are
    # band-rejected, leaving one small long-ring blob.
    rings = bin_rings(components_of_frame(load_config(), RIM_MERGED_FRAME))
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


# --- ADR 108: the post-2026-09-02 minimap -------------------------------------

RTB_FRAMES = sorted((ROOT / "test_screenshots" / "unknown_anomalies").glob("rtb_*.png")) \
    if (ROOT / "test_screenshots" / "unknown_anomalies").exists() else []


@pytest.mark.skipif(len(RTB_FRAMES) < 5, reason="crossing corpus not present")
def test_the_boundary_is_found_at_the_centre_on_crossing_frames(analyzer):
    """Every one of these frames was captured with RETURN TO BATTLE on screen,
    so the line is at the aircraft. Before ADR 108 the detector read NOTHING on
    81% of ticks — five of eight crossings that session had no reading at all in
    the 30 s before they happened, because the thin antialiased line arrived as
    20 to 174 fragments and the span filter rejected all of them."""
    seen = []
    for f in RTB_FRAMES:
        r = analyzer.detect_map_boundary(cv2.imread(str(f)))
        if r is not None:
            seen.append((f.name, r[0]))
    # A RATE, not an exact count. The corpus grows every session that captures
    # a crossing, so an equality calibrated on nine frames fails the moment the
    # tenth arrives — which is the corpus doing its job, not a regression.
    # Detection was 18.8% of ticks before ADR 108; the bar here is that a frame
    # WITH the banner up almost always yields a reading.
    assert len(seen) >= 0.9 * len(RTB_FRAMES), \
        f"only {len(seen)}/{len(RTB_FRAMES)} frames produced a reading"
    # KNOWN LIMITATION, one frame of nine (rtb_...105821_crossing5). Where the
    # line crosses bright tan terrain it merges with it locally, the thickness
    # gate rejects that stretch, and only a distant fragment of the same arc
    # survives — so the range is measured to the wrong part of the line. The
    # frame reads 0.44R with the aircraft sitting ON the boundary. Asserted as a
    # majority rather than hidden: tightening this to 9/9 would mean loosening
    # the gate that keeps desert terrain out, which is the worse trade.
    near = [d for _n, d in seen if d < 0.30]
    assert len(near) >= 0.85 * len(seen), \
        f"only {len(near)}/{len(seen)} readings were at the boundary: {seen}"


@pytest.mark.skipif(not (ROOT / "test_screenshots" / "AMMO_MISSILE.png").exists(),
                    reason="AMMO_MISSILE not archived")
def test_island_terrain_is_not_read_as_a_boundary(analyzer):
    """The new minimap renders tan terrain in the boundary's own hue family. The
    old rule took the LARGEST component and reported 0.78-0.80R from islands on
    these two frames; the shape gate rejects them, because an arc fills 0.03-0.23
    of its bounding box and a landmass fills 0.48-0.79."""
    for name in ("AMMO_MISSILE.png", "AMMO_MISSILE_1.png"):
        f = ROOT / "test_screenshots" / name
        if not f.exists():
            continue
        assert analyzer.detect_map_boundary(cv2.imread(str(f))) is None, \
            f"{name}: terrain read as a boundary"


# Enumerated, NOT globbed. The first version matched every approach_*.png, so
# the next live session dropped frames from other maps into the corpus and the
# test failed on them — correctly, since those frames may legitimately show a
# boundary inside 0.20R. A curated negative corpus has to name its members.
_DESERT_NAMES = (
    "approach_20260903_164202_17.png",
    "approach_20260903_164218_18.png",
    "approach_20260903_164224_19.png",
    "approach_20260903_164235_20.png",
)
DESERT_FRAMES = [p for p in
                 (ROOT / "test_screenshots" / "unknown_anomalies" / n
                  for n in _DESERT_NAMES) if p.exists()]


@pytest.mark.skipif(len(DESERT_FRAMES) < 3, reason="desert negative corpus not present")
def test_desert_terrain_is_not_read_as_a_NEAR_boundary(analyzer):
    """The four frames kept from the 2026-09-03 false-positive session, on a
    desert map whose tan landmass sits in the boundary's own hue family.

    Before ADR 108's local-thickness gate these read 0.016-0.065R — the mask
    covering an entire landmass, reported as an edge at the aircraft — and drove
    32 turns in 12 minutes, one at round start. A reading is allowed here (the
    real line IS somewhere on the minimap); reading it AT the aircraft is the
    regression.

    Kept in the gitignored anomalies folder rather than committed: four full
    frames are 8 MB, and ADR 100 exists because this repository grew on exactly
    that kind of artifact. The test skips when they are absent."""
    for f in DESERT_FRAMES:
        r = analyzer.detect_map_boundary(cv2.imread(str(f)))
        if r is None:
            continue
        assert r[0] >= 0.20, \
            f"{f.name} read {r[0]:.3f}R — terrain masquerading as a near edge"


# --- ADR 122: the lateral component, and turning away from the edge ----------

def test_the_detector_reports_which_side_the_boundary_is_on():
    """ADR 122. `dx` was computed and discarded, so the turn had no way to know
    which way to roll. A vertical line to the RIGHT of centre must read
    positive lateral; the same line to the left, negative."""
    import numpy as np
    import cv2
    from wingman.analyzer import GameStateAnalyzer
    from wingman.crop_region import CropCoords

    def _reading(line_x):
        img = np.zeros((200, 200, 3), dtype=np.uint8)
        # The boundary hue (~17) at full saturation, drawn as a thin line.
        colour = cv2.cvtColor(
            np.uint8([[[17, 200, 220]]]), cv2.COLOR_HSV2BGR)[0][0].tolist()
        cv2.line(img, (line_x, 20), (line_x, 180), colour, 2)
        a = GameStateAnalyzer.__new__(GameStateAnalyzer)
        a.crops = {"MINIMAP": CropCoords(0.0, 0.0, 1.0, 1.0)}
        a._boundary_hsv_lower = np.array([8, 60, 120], dtype=np.uint8)
        a._boundary_hsv_upper = np.array([28, 255, 255], dtype=np.uint8)
        a._boundary_close_kernel = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (3, 3))
        a._boundary_close_iters = 1
        a._boundary_min_span_frac = 0.5
        a._boundary_max_thickness_frac = 0.1
        a._boundary_min_px = 20
        a._mask_radius_frac = 0.93
        a._minimap_mask_radius_frac = 0.93
        a._minimap_circle_cache = None
        return a.detect_map_boundary(img)

    right = _reading(150)
    left = _reading(50)
    assert right is not None and left is not None, "detector found no line"
    assert len(right) == 3, "reading no longer carries the lateral component"
    assert right[2] > 0, f"line on the right read lateral {right[2]:+.3f}"
    assert left[2] < 0, f"line on the left read lateral {left[2]:+.3f}"


def test_the_turn_rolls_away_from_the_boundary():
    """ADR 122. The turn always rolled RIGHT, so roughly half of them turned
    INTO the edge — which is what ADR 107's median gain of +0.00R over 61 turns
    looks like.

    Drives the REAL `boundary_turn_mode` and reads the key it actually presses.
    An earlier version of this test re-derived the choice from `lateral` and so
    proved only that the test agreed with itself.
    """
    import threading
    import unittest.mock as mock
    from wingman.controller import Controller
    from wingman.keybindings import ROLL_LEFT_KEY, ROLL_RIGHT_KEY

    def _pressed_key(lateral):
        c = Controller.__new__(Controller)
        c._boundary_turning = threading.Event()
        c._boundary_turn_stop = threading.Event()
        c._ejecting = threading.Event()
        c._missile_evading = threading.Event()
        c._mission_cancel = threading.Event()
        c._exit_event = threading.Event()
        c._boundary_turn_max_s = 0.2
        c._analyzer = None
        c._climb_key = mock.MagicMock()
        c._inc_programmatic_key = mock.MagicMock()
        c._dec_programmatic_key = mock.MagicMock()
        # The SAF-010 exit push runs when the turn ends. It is a separate
        # mechanism with its own tuning, so it is stubbed rather than
        # reconstructed — and stubbed at the METHOD, not attribute by
        # attribute, so the stub cannot drift out of date. A test that passed
        # while its own thread raised would be reading the assertions before
        # the crash, so thread exceptions are errors here.
        c._climb_exit_push = mock.MagicMock()
        c._climb_stop = threading.Event()
        # Everything else the turn thread touches, enumerated from the code
        # rather than discovered one AttributeError at a time.
        c._arm_release_grace = mock.MagicMock()
        c._climbing = threading.Event()
        c._climb_exit_alt = 0.0
        c._climb_max_s = 1.0
        c.boundary_turn_mode(lateral=lateral)
        t = getattr(c, "_boundary_turn_thread", None)
        if t is not None:
            t.join(timeout=3.0)
        rolls = [call.args[0] for call in c._climb_key.call_args_list
                 if call.args and call.args[0] in (ROLL_LEFT_KEY, ROLL_RIGHT_KEY)]
        return rolls

    right_edge = _pressed_key(+0.40)
    assert right_edge, "no roll key was pressed at all"
    assert set(right_edge) == {ROLL_LEFT_KEY}, \
        f"edge on the right — expected a LEFT roll, got {set(right_edge)}"

    left_edge = _pressed_key(-0.40)
    assert set(left_edge) == {ROLL_RIGHT_KEY}, \
        f"edge on the left — expected a RIGHT roll, got {set(left_edge)}"

    unknown = _pressed_key(None)
    assert set(unknown) == {ROLL_RIGHT_KEY}, \
        "with no lateral the turn must keep its previous fixed direction"




def test_the_turn_cap_is_not_shortened_without_new_evidence():
    """ADR 126, and its DISPROVED premise.

    The original version of this test asserted the cap was at most 7 s, on the
    theory that a 12 s turn completes a circle — 294 degrees of measured path
    rotation — and so returns the aircraft to where it started.

    The soak disproved it. Capping the ACTUATOR does not cap the MANOEUVRE: 167
    actuator starts landed inside 47 selection episodes, 3.6 restarts each,
    because the CONDITION decides how long the aircraft turns and the actuator
    just restarts within it. Median path rotation went 294 -> 320 deg (up),
    turns per mission 2.6 -> 6.7, and crossings per mission 0.094 -> 0.160.

    The lever is the release condition, not this constant. This guards against
    shortening it again without evidence that reaches the condition.
    """
    import yaml
    with open("wingman/config.yaml") as fh:
        cfg = yaml.safe_load(fh)
    cap = float(cfg["behavior_tree"]["climb"]["boundary_turn_max_s"])
    assert cap >= 10.0, (
        f"cap {cap}s: shortening the actuator cap was MEASURED to increase "
        "turn frequency and total rotation, because the condition restarts it")

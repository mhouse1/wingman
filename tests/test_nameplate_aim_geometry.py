"""The steering point must land on the aircraft, which the game draws BELOW its nameplate.

Why this test exists. Commit 3cb467d (2026-09-24) aimed at a marker it expected 162-287 px
ABOVE the label, else 225 px above it, and its synthetic tests drew the marker above the label,
so the suite agreed with the code while both disagreed with the game: on real frames that point
was about 325 px above the aircraft. A fake built from the code's own assumption proves only
that the code is self-consistent, so these are REAL frames.

Each fixture is a crop of an archived frame from the 16:47 to 16:49 pursuit (targets at 0.36 to
6.8 km). `marker` is the centre of the game's diamond bracket around the aircraft in absolute
frame coordinates, read by eye from a 2.5x zoom with a ruler (+-3 px). Measured across 8 frames
the marker centre sits 90 to 110 px below the label's glyph centre (mean 100.5); these four are
the ones whose crops reproduce the full-frame decision exactly.
"""

import pathlib

import cv2
import pytest
import yaml

from wingman.tracker import TargetTracker

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_FIXTURES = _ROOT / "tests" / "fixtures"
_W, _H = 1920, 1200
_TOLERANCE_PX = 15

# file, crop origin (ox, oy), marker centre (x, y), range in km (for the failure message)
_CASES = [
    ("aim_164703_43.png", 46, 459, (239, 671), 6.8),
    ("aim_164844_52.png", 351, 843, (545, 1054), 6.7),
    ("aim_164900_55.png", 702, 205, (900, 421), 1.7),
    ("aim_164906_56.png", 842, 418, (1035, 638), 0.36),
]


@pytest.fixture(scope="module")
def shipped_cfg():
    return yaml.safe_load((_ROOT / "wingman" / "config.yaml").read_text(encoding="utf-8"))


def _aim(cfg, name, ox, oy, **tracking):
    crop = cv2.imread(str(_FIXTURES / name))
    assert crop is not None, f"missing fixture {name}"
    cfg = {**cfg, "tracking": {**cfg["tracking"], **tracking}}
    p = TargetTracker(cfg)._red_mass_probe(crop, ox, oy, _W, _H, ref=None)
    return p, (p["centroid"][0] + ox, p["centroid"][1] + oy) if p["centroid"] else None


@pytest.mark.parametrize("name,ox,oy,marker,km", _CASES)
def test_the_steering_point_lands_on_the_aircraft_marker(shipped_cfg, name, ox, oy, marker, km):
    p, aim = _aim(shipped_cfg, name, ox, oy)
    assert p["gate"] == "pass", f"{name}: no nameplate found at {km} km"
    assert abs(aim[0] - marker[0]) <= _TOLERANCE_PX, (name, aim, marker)
    assert abs(aim[1] - marker[1]) <= _TOLERANCE_PX, (name, aim, marker)


@pytest.mark.parametrize("name,ox,oy,marker,km", _CASES)
def test_the_marker_is_below_the_label_on_every_real_frame(shipped_cfg, name, ox, oy, marker, km):
    p, _aim_xy = _aim(shipped_cfg, name, ox, oy)
    cluster_y = p["cluster"][1]
    assert 85 <= marker[1] - cluster_y <= 115, (name, cluster_y, marker)


@pytest.mark.parametrize("name,ox,oy,marker,km", _CASES)
def test_these_frames_would_have_caught_the_rule_that_aimed_above_the_label(
        shipped_cfg, name, ox, oy, marker, km):
    """Discrimination: the 225 px up rule misses every one of them by far more than the tolerance."""
    _p, aim = _aim(shipped_cfg, name, ox, oy, red_mass_aim_offset_px=-225)
    assert abs(aim[1] - marker[1]) > 5 * _TOLERANCE_PX

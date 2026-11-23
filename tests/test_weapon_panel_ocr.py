"""FR-012: the weapon panel's count, flares and weapon name read off the HUD the
game draws since its 2026-11-18 update, through the crops in config.yaml.

Real OCR on the bottom-right corner (x 1400-1920, y 1000-1200) of five 'v'
screenshots of 2026-11-19, pasted back into a 1920 x 1200 frame so the crops'
fractions apply unchanged. A crop edit that clips a three-digit count (114 read
as 1 or 14) or moves WEAPON_NAME off the label fails here. The fixtures live in
tests/fixtures/, untracked like the rest of the corpus (ADR 100), so the test
skips on a machine without them.
"""

from pathlib import Path

import numpy as np
import pytest
import yaml

from wingman.analyzer import _process_health_region, _process_weapon_name_region
from wingman.crop_region import get_crop, load_crops
from wingman.weapon_name import classify

_FIX = Path(__file__).parent / "fixtures" / "weapon_panel"
_CFG = yaml.safe_load((Path(__file__).parent.parent / "wingman" / "config.yaml").read_text())
_CROPS = load_crops(_CFG["crops"])
_CLASSES = _CFG["weapon"]["classes"]

# fixture -> (count, flares, class or None, a word the name must contain)
_PANELS = {
    "rocket_pod_114.png": (114, 5, "rocket", "ROCKET"),
    "rocket_pod_72.png": (72, 5, "rocket", "ROCKET"),
    "meteor_0_flares_4.png": (0, 4, None, "MET"),
    "meteor_0_flares_2.png": (0, 2, None, "MET"),
    "aim4_sar_0_flares_0.png": (0, 0, None, "AIM4"),
}

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not _FIX.is_dir(), reason="weapon panel fixtures are on veda only"),
]


def _frame(name):
    import cv2
    panel = cv2.imread(str(_FIX / name))
    assert panel is not None and panel.shape[:2] == (200, 520), name
    frame = np.zeros((1200, 1920, 3), np.uint8)
    frame[1000:1200, 1400:1920] = panel
    return frame


def _crop(frame, label):
    return get_crop(frame, *_CROPS[label][:4])


@pytest.mark.parametrize("name", sorted(_PANELS))
def test_the_count_reads_whole(name):
    assert _process_health_region(_crop(_frame(name), "AMMO_MISSILE"),
                                  "ammo_missiles")[0] == _PANELS[name][0]


@pytest.mark.parametrize("name", sorted(_PANELS))
def test_the_flares_read(name):
    assert _process_health_region(_crop(_frame(name), "AMMO_FLARES"),
                                  "ammo_flares")[0] == _PANELS[name][1]


@pytest.mark.parametrize("name", sorted(_PANELS))
def test_the_weapon_name_reads_into_its_class(name):
    text = _process_weapon_name_region(_crop(_frame(name), "WEAPON_NAME"))
    _, _, cls, word = _PANELS[name]
    compact = "".join(c for c in text.upper() if c.isalnum())
    assert word in compact, text
    assert classify(text, _CLASSES)[0] == cls, text

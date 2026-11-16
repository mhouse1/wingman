"""ADR 180 D1: the selected weapon is read from its name on the weapon panel and
sorted into a class by `weapon.classes`.

The fixtures in tests/fixtures/weapon_name/ are the WEAPON_NAME crop of operator
`v` screenshots (tests/test-output/), checked by eye, enumerated here:
2026-11-15 14:08:36 and 14:08:47 (the rocket pod, the second with the panel
faded at 0/38), 14:08:29 and 07:03:34 (AIM-9M), 04:48:49 (PL-8), and 2026-11-13
11:28:10 (R-77 Adder, over bright ground).
"""

import logging
from pathlib import Path

import cv2
import pytest
import yaml

import wingman.analyzer as analyzer_module
from wingman.analyzer import GameStateAnalyzer
from wingman.config_schema import validate_config
from wingman.crop_region import load_crops
from wingman.weapon_name import FIRE_MODES, WeaponNameReader, classify

_FIX = Path(__file__).parent / "fixtures" / "weapon_name"
_CFG = yaml.safe_load((Path(__file__).parents[1] / "wingman" / "config.yaml").read_text())
_CLASSES = _CFG["weapon"]["classes"]


# -- sorting a name into a class ----------------------------------------------------

@pytest.mark.parametrize("name,cls,fire", [
    ("LAU-3 Hydra Rocket Pod", "rocket", "hold_on_lead"),
    # EasyOCR's reads of that label on 2026-11-15, at 1x and 2x.
    ("LAU- 3 Hydro Rocket Pod", "rocket", "hold_on_lead"),
    ("LAU 3 Hydra RRocket Pod", "rocket", "hold_on_lead"),
    ("AIM-9M Sidewinder", "heat_seeking", "tap"),
    ("AIM-9M Sidewindel", "heat_seeking", "tap"),
    ("AIM-gM Sidewinder", "heat_seeking", "tap"),
    ("AIM-9M", "heat_seeking", "tap"),
    ("PL-8", "heat_seeking", "tap"),
    ("R-77 Adder", "radar", "tap"),
    ("RiZZ Adder", "radar", "tap"),
    # Not a weapon this table knows: tapped, as every weapon was before.
    ("AGM-65 Maverick", None, "tap"),
    ("R-17 Adtel", None, "tap"),
])
def test_the_shipped_classes_sort_the_names_seen(name, cls, fire):
    assert classify(name, _CLASSES) == (cls, fire)


def test_a_new_kind_of_weapon_is_a_class_in_the_table():
    """The design's point: a guided missile needs a class and a word, no code."""
    classes = dict(_CLASSES, guided={"fire": "tap", "names": ["Maverick", "AGM-65"]})
    assert classify("AGM-65 Maverick", classes) == ("guided", "tap")


def test_classes_are_tried_in_order():
    classes = {"first": {"fire": "tap", "names": ["Pod"]},
               "second": {"fire": "hold_on_lead", "names": ["Rocket"]}}
    assert classify("LAU-3 Hydra Rocket Pod", classes) == ("first", "tap")


def test_the_shipped_config_is_valid_and_its_modes_are_known():
    assert validate_config(_CFG) == []
    assert {spec["fire"] for spec in _CLASSES.values()} <= set(FIRE_MODES)
    assert _CLASSES["rocket"]["fire"] == "hold_on_lead"
    assert "WEAPON_NAME" in load_crops(_CFG["crops"])


def test_an_unknown_fire_mode_is_refused():
    cfg = dict(_CFG, weapon=dict(_CFG["weapon"], classes={
        "rocket": {"fire": "spray", "names": ["Rocket"]}}))
    problems = validate_config(cfg)
    assert len(problems) == 1 and "weapon.classes.rocket.fire" in problems[0]


# -- the reader ---------------------------------------------------------------------

def test_the_reader_holds_the_newest_name_until_it_is_stale(caplog):
    t = [100.0]
    reader = WeaponNameReader(_CLASSES, stale_after_s=6.0, clock=lambda: t[0])
    assert reader.selected() is None
    with caplog.at_level(logging.INFO, logger="wingman.weapon_name"):
        reader.note_text("AIM-9M Sidewinder")
        reader.note_text("AIM-9M Sidewindel")          # the same class: no new line
        assert reader.selected().cls == "heat_seeking"
        reader.note_text("LAU-3 Hydra Rocket Pod")
    weapon = reader.selected()
    assert (weapon.cls, weapon.fire) == ("rocket", "hold_on_lead")
    assert caplog.text.count("Weapon selected:") == 2
    assert "'LAU-3 Hydra Rocket Pod' -> rocket (hold_on_lead)" in caplog.text
    t[0] += 6.5
    assert reader.selected() is None


def test_blank_text_changes_nothing():
    reader = WeaponNameReader(_CLASSES)
    reader.note_text("LAU-3 Hydra Rocket Pod")
    for blank in ("", None, " ", "-"):
        assert reader.note_text(blank) is None
    assert reader.selected().cls == "rocket"


def test_one_unreadable_name_does_not_displace_a_known_weapon(caplog):
    reader = WeaponNameReader(_CLASSES)
    reader.note_text("LAU-3 Hydra Rocket Pod")
    reader.note_text("RZZ lidiel")
    assert reader.selected().cls == "rocket"
    with caplog.at_level(logging.INFO, logger="wingman.weapon_name"):
        reader.note_text("AGM-65 Maverick")             # the second in a row
        reader.note_text("AGM-65 Maverick")
    weapon = reader.selected()
    assert weapon.cls is None and weapon.fire == "tap"
    assert caplog.text.count("is in no weapon class") == 1     # once a name


# -- the analyzer -------------------------------------------------------------------

class _Now:
    """An executor that runs the job at once."""

    def submit(self, fn, *args):
        from concurrent.futures import Future
        fut = Future()
        fut.set_result(fn(*args))
        return fut


def _analyzer(monkeypatch, text):
    an = GameStateAnalyzer.__new__(GameStateAnalyzer)
    an.crops = load_crops(_CFG["crops"])
    an._weapon_name = WeaponNameReader(_CLASSES)
    an._weapon_name_future = None
    monkeypatch.setattr(analyzer_module, "_process_weapon_name_region", lambda crop: text)
    return an


def test_the_analyzer_reads_the_name_off_the_panel(monkeypatch):
    import numpy as np
    an = _analyzer(monkeypatch, "LAU-3 Hydra Rocket Pod")
    assert an.selected_weapon() is None
    an._submit_weapon_name(_Now(), np.zeros((1200, 1920, 3), np.uint8))
    assert an.selected_weapon().fire == "hold_on_lead"
    assert an._weapon_name_future is None       # ready for the next read


def test_the_analyzer_reads_one_name_at_a_time(monkeypatch):
    import numpy as np
    an = _analyzer(monkeypatch, "AIM-9M Sidewinder")
    an._weapon_name_future = object()           # a read still in flight
    an._submit_weapon_name(_Now(), np.zeros((1200, 1920, 3), np.uint8))
    assert an.selected_weapon() is None


def test_with_the_reader_off_there_is_no_weapon(monkeypatch):
    import numpy as np
    an = _analyzer(monkeypatch, "LAU-3 Hydra Rocket Pod")
    an._weapon_name = None                      # weapon.enabled: false
    an._submit_weapon_name(_Now(), np.zeros((1200, 1920, 3), np.uint8))
    assert an.selected_weapon() is None


# -- real OCR on the operator's frames ----------------------------------------------

_LABELS = {
    "name_lau3_rockets.png": "rocket",
    "name_lau3_rockets_faded.png": "rocket",
    "name_aim9m.png": "heat_seeking",
    "name_aim9m_canyon.png": "heat_seeking",
    "name_pl8.png": "heat_seeking",
    "name_r77_adder.png": "radar",
}


@pytest.fixture(scope="module")
def ocr():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("name,cls", sorted(_LABELS.items()))
def test_real_ocr_sorts_the_label(monkeypatch, ocr, name, cls):
    path = _FIX / name
    if not path.exists():
        pytest.skip(f"fixture {name} not on this machine (tests/fixtures is untracked)")
    monkeypatch.setattr(analyzer_module, "_get_thread_ocr_reader", lambda: ocr)
    text = analyzer_module._process_weapon_name_region(cv2.imread(str(path)))
    assert classify(text, _CLASSES)[0] == cls, text

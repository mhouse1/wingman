"""ADR 167: each round's jet, read off the lobby as PLAY is clicked."""

import logging
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman import jet_name
from wingman.config_schema import schema_default
from wingman.crop_region import CropCoords
from wingman.jet_name import JetRead, name_band, parse_name, read_jet
from wingman.main import _note_jet

MAIN_SRC = Path("wingman/main.py").read_text(encoding="utf-8")

_WHOLE = CropCoords(0.0, 0.0, 1.0, 1.0)


def _bgr(hue, saturation=200, value=230):
    pixel = np.uint8([[[hue, saturation, value]]])
    return tuple(int(c) for c in cv2.cvtColor(pixel, cv2.COLOR_HSV2BGR)[0, 0])


def _band(*blocks, height=40, width=1920):
    """A sky-blue band with blocks of (x, width, colour) on it."""
    frame = np.full((height, width, 3), _bgr(105, 120, 220), np.uint8)
    for x, block_width, colour in blocks:
        frame[8:32, x:x + block_width] = colour
    return frame


GOLD = _bgr(23)


# -- the name ----------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("J-10 FIREBIRD", "J-10 FIREBIRD"),
    ("Su-35 SUPER FLANKER", "SU-35 SUPER FLANKER"),   # OCR's case cannot split a jet in two
    ("MiG-21  FISHBED", "MIG-21 FISHBED"),
    ("TORNADO ADV", "TORNADO ADV"),
    ("JAGUAR GR3", "JAGUAR GR3"),
    ("F/A-18F SUPER HORNET", "F/A-18F SUPER HORNET"),   # 2026-10-31: what mobius1 flies
    ("FIA-18F SUPER HORNET", "F/A-18F SUPER HORNET"),   # how OCR reads it, every time
    ("F1A-18F SUPER HORNET", "F/A-18F SUPER HORNET"),
    ("FLA-18F SUPER HORNET", "F/A-18F SUPER HORNET"),
    ("FIAT G91", "FIAT G91"),                           # only the F/A- form is touched
    ("M.III", "M.III"),
    ("", None), ("Y", None), ("10", None), ("638", None), ("-", None),
    ("- FLANKER", None), ("J-10 -", None), ("F/", None), ("/A-18F", None),
    ("Su-27. FLANKER", None), ("F//A-18F", None)])
def test_a_name_is_words_with_inner_hyphens(text, expected):
    assert parse_name(text) == expected


# -- cutting the label at the trophy icon ------------------------------------------

def test_the_band_ends_at_the_trophy_icon():
    frame = _band((300, 18, GOLD), (420, 18, GOLD))     # the trophy, and the league badge
    assert name_band(frame, _WHOLE).shape[1] == 300


def test_a_stray_gold_speck_does_not_end_the_band():
    frame = _band((120, 3, GOLD), (300, 18, GOLD))
    assert name_band(frame, _WHOLE).shape[1] == 300


def test_an_orange_class_icon_does_not_end_the_band():
    """The J-10's icon is orange and sits left of the name."""
    frame = _band((20, 40, _bgr(10)), (300, 18, GOLD))
    assert name_band(frame, _WHOLE).shape[1] == 300


def test_no_trophy_icon_means_no_band():
    assert name_band(_band(), _WHOLE) is None


# -- one frame's read, on a stand-in reader ----------------------------------------

class _Reader:
    def __init__(self, boxes):
        self.boxes = boxes
        self.calls = []

    def readtext(self, image, **options):
        self.calls.append((image, options))
        return self.boxes


def _box(x, text):
    return ([[x, 0], [x + 10, 0], [x + 10, 10], [x, 10]], text, 0.9)


def test_the_words_are_read_left_to_right_at_double_size():
    reader = _Reader([_box(200, "FLANKER"), _box(0, "Su-27")])
    read = read_jet(reader, _band((300, 18, GOLD)), _WHOLE)
    assert read.name == "SU-27 FLANKER" and read.text == "Su-27 FLANKER"
    image, options = reader.calls[0]
    assert image.shape[1] == 600, "the band alone, doubled"
    assert "/" in options["allowlist"] and "." in options["allowlist"], "F/A-18F, M.III"
    assert "'" not in options["allowlist"]


def test_a_frame_without_the_icon_costs_no_ocr():
    reader = _Reader([_box(0, "ANYTHING")])
    assert read_jet(reader, _band(), _WHOLE) == JetRead(None, "")
    assert reader.calls == []


# -- booking it --------------------------------------------------------------------

class _Saved:
    def __init__(self):
        self.calls = []

    def __call__(self, frame, name):
        self.calls.append((frame, name))
        return True


def test_a_name_is_booked_and_its_label_kept():
    booked, saved = [], _Saved()
    _note_jet(JetRead("J-10 FIREBIRD", "J-10 FIREBIRD", "the label"), "the frame",
              logging.getLogger("t"), booked.append, saved, stamp="s")
    assert booked == ["J-10 FIREBIRD"]
    assert saved.calls == [("the label", "s_J-10-FIREBIRD")]


@pytest.mark.parametrize("read, why", [
    (None, "no OCR"),
    (JetRead(None, ""), "no trophy icon on the label"),
    (JetRead(None, "Y"), "OCR read 'Y'")])
def test_an_unread_lobby_books_nothing_says_why_and_keeps_the_frame(read, why, caplog):
    booked, saved = [], _Saved()
    with caplog.at_level(logging.WARNING):
        _note_jet(read, "the frame", logging.getLogger("t"), booked.append, saved, stamp="s")
    assert booked == []
    assert why in caplog.text and "JET NAME: not read" in caplog.text
    assert saved.calls == [("the frame", "s_unread")]


def test_a_jet_with_a_slash_is_saved_as_a_file_not_a_directory():
    """F/A-18F, 2026-10-31: `save_frame` never raises, so this failed silently."""
    saved = _Saved()
    _note_jet(JetRead("F/A-18F SUPER HORNET", "x", "the label"), "the frame",
              logging.getLogger("t"), lambda name: None, saved, stamp="s")
    assert saved.calls == [("the label", "s_F-A-18F-SUPER-HORNET")]


def test_with_capture_off_nothing_is_kept():
    _note_jet(JetRead("J-10 FIREBIRD", "x", "the label"), "the frame", logging.getLogger("t"),
              lambda name: None, False, stamp="s")
    _note_jet(None, "the frame", logging.getLogger("t"), lambda name: None, False, stamp="s")


# -- the wiring --------------------------------------------------------------------

def test_the_read_leaves_the_click_alone_and_clears_the_last_round_first():
    """An unread lobby must not book its round under the previous round's jet, and
    the PLAY click must not wait for OCR."""
    body = MAIN_SRC[MAIN_SRC.index("def _on_play_click_read_jet("):]
    body = body[:body.index("analyzer.subscribe(")]
    assert body.index("stats_tracker.record_jet(None)") < body.index("threading.Thread(")
    assert "daemon=True" in body and "run_ocr" not in body


def test_the_read_is_live_only_and_listens_to_the_play_click():
    block = MAIN_SRC[MAIN_SRC.index("# ADR 167: each round's jet"):]
    assert block.startswith("# ADR 167: each round's jet, off the frame PLAY or READY")
    assert "if not replay_mode and not capture_mode and jet_name.CROP in analyzer.crops:" in block
    assert ("analyzer.subscribe(GameEvent.LOBBY_PLAY_CLICK, _on_play_click_read_jet, "
            'name="jet_name")') in block


def test_the_crop_is_calibrated_and_capture_is_declared():
    cfg = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert jet_name.CROP in cfg["crops"]
    assert schema_default("jet_name.capture") is False
    assert cfg["jet_name"]["capture"] is True


# -- real OCR on saved lobby frames, 2026-09 to 2026-10 ----------------------------

_CORPUS = Path(__file__).parent / "fixtures" / "jet_name"

_JETS = ("SU-27 FLANKER", "SU-30 STRIKE FLANKER", "SU-33 SEA FLANKER", "SU-34 FULLBACK",
         "SU-35 SUPER FLANKER", "SU-47 BERKUT", "MIG-21 FISHBED", "J-10 FIREBIRD",
         "TORNADO ADV", "JAGUAR GR3", "F/A-18F SUPER HORNET")


def _config_crop():
    crops = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))["crops"]
    return CropCoords(*crops[jet_name.CROP]["coords"][0], *crops[jet_name.CROP]["coords"][1])


def _load(name):
    path = _CORPUS / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    return cv2.imread(str(path))


@pytest.fixture(scope="module")
def reader():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("jet", _JETS)
def test_real_ocr_reads_each_jet(reader, jet):
    """SU-35 SUPER FLANKER and JAGUAR GR3 are the two that OCR ran into the trophy
    count before the cut ("Su-35 SUPER FLANKER '/683", "JAGUAR GR3 Y 0 @"). The
    F/A-18F frame is live, from the operator's client on 2026-10-31."""
    assert read_jet(reader, _load(f"{jet.replace('/', '-')}.png"), _config_crop()).name == jet


@pytest.mark.slow
def test_real_frames_off_the_lobby_read_nothing(reader):
    assert read_jet(reader, _load("end_of_round.jpg"), _config_crop()).name is None

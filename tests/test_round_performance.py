"""The round's score and K/D/A, read off the end-of-round PERFORMANCE panel.

The real-OCR corpus (tests/fixtures/round_performance/, AI-005-2) is frames of
2026-10-26 and 2026-10-29 at 1920x1200: the panel at each of the three heights
seen (four medals, five, none), and the two pages that must not read, the
podium before the panel and the scoreboard after it.
"""

from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from constants import CONFIG_PATH
from wingman.crop_region import CropCoords
from wingman.round_performance import (
    KDA_CROP, MAX_READS, READ_INTERVAL_S, SCORE_CROP, PanelRead, RoundPerformance,
    frame_tag, parse_kda, parse_score, read_panel, read_round, save_frame, unread_reason)

_CORPUS = Path(__file__).parent / "fixtures" / "round_performance"


@pytest.mark.parametrize("text, expected", [
    ("1/2/0", (1, 2, 0)), ("1 / 2 / 0", (1, 2, 0)), ("12/10/5", (12, 10, 5)),
    ("0/0/0", (0, 0, 0)), ("O/3/1", (0, 3, 1)),
    ("11210", None),        # a slash read as a 1
    ("63/4/040", None),     # the scoreboard's columns, 2026-10-29 03:18
    ("100/1/1", None),
    ("1/2", None), ("1/2/0/4", None), ("//2/0", None), ("", None)])
def test_the_kda_is_three_numbers_between_slashes(text, expected):
    assert parse_kda(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("163", 163), ("1,234", 1234), ("1.234", 1234), (" 163 ", 163), ("0", 0), ("1O0", 100),
    ("05,.,.", None),       # the scoreboard, 2026-10-29 03:18
    ("", None), ("1/2", None),
    # OCR's boxes joined left to right with a separator between them (2026-10-30).
    ("1,,255", 1255), ("1,255", 1255), ("12,345", 12345),
    ("1,3,255", None),      # a stray box between the groups
    ("1,63", None),         # a three-digit score split in two
    ("1,", None), (",255", None),
    ("13210", None),        # a comma read as a digit, 2026-10-31 15:37
    ("1210", None)])        # the panel prints the comma
def test_the_score_is_a_number(text, expected):
    assert parse_score(text) == expected


# -- one frame ---------------------------------------------------------------------

def _box(x, y, text):
    return [[x, y - 20], [x + 90, y - 20], [x + 90, y + 20], [x, y + 20]], text, 0.9


class _Reader:
    """An EasyOCR stand-in. The KDA band's read answers `boxes`, each (text,
    middle row in the frame); the score's read, the one with an allowlist,
    answers `score`, each text or (text, left edge), placed left to right in
    the order given unless its edge says otherwise. The row doubled answers
    `doubled` the same way."""

    def __init__(self, boxes, score=(), doubled=()):
        self.boxes = boxes
        self.score = self._placed(score)
        self.doubled = self._placed(doubled)
        self.calls = []

    @staticmethod
    def _placed(score):
        return [s if isinstance(s, tuple) else (s, 100 * i) for i, s in enumerate(score)]

    def readtext(self, img, detail=0, paragraph=False, workers=0, allowlist=None):
        self.calls.append((detail, allowlist, img.shape[0]))
        if allowlist is None:
            return [_box(0, y, text) for text, y in self.boxes]
        score = self.doubled if img.shape[0] > 120 else self.score
        return [_box(x, 40, text) for text, x in score]


_SCORE = CropCoords(0.0, 0.0, 0.5, 1.0)
_KDA = CropCoords(0.5, 0.0, 1.0, 1.0)
_FRAME = np.zeros((1200, 1920, 3), np.uint8)

# The no-medals panel's KDA column as OCR read it on 2026-10-29 03:09.
_PANEL_BOXES = [("M A N C E", 511), ("1/5/0", 613), ("KD A", 675)]


def test_the_kda_is_found_by_the_label_under_it_and_the_score_on_its_row():
    reader = _Reader(_PANEL_BOXES, ["100"])
    assert read_panel(reader, _FRAME, _SCORE, _KDA) == PanelRead(
        RoundPerformance(100, 1, 5, 0), "1/5/0", "100")
    (band, _), (row, allowlist) = [(c[0], c[1]) for c in reader.calls]
    assert (band, row, allowlist) == (1, 1, "0123456789,.")
    assert 70 <= reader.calls[1][2] <= 90, "the KDA's row at its own size; not the band"


def test_the_scoreboards_kdas_have_their_label_above_and_do_not_read():
    """2026-10-29 03:18: the scoreboard's KDA column, with its header above."""
    reader = _Reader([("KD A", 298), ("7/6/3", 379), ("3/4/0", 493), ("2/3/0", 606)], ["400"])
    panel = read_panel(reader, _FRAME, _SCORE, _KDA)
    assert panel == PanelRead(None, "KD A | 7/6/3 | 3/4/0 | 2/3/0")
    assert len(reader.calls) == 1, "no KDA, so no score read"


def test_a_kda_with_no_label_does_not_read():
    assert read_panel(_Reader([("1/5/0", 613)], ["100"]), _FRAME, _SCORE, _KDA).reading is None


def test_a_kda_without_a_score_is_no_reading():
    assert read_panel(_Reader(_PANEL_BOXES, []), _FRAME, _SCORE, _KDA) == PanelRead(
        None, "1/5/0", "")


def test_a_split_score_reads_left_to_right():
    """2026-10-30 09:10, acct3: OCR listed the pieces of "1,255" out of order and
    the round was booked at 25513. The 03:48 frame of a 1,295 lists "295" first."""
    reader = _Reader(_PANEL_BOXES, [("295", 277), ("1,", 191)])
    assert read_panel(reader, _FRAME, _SCORE, _KDA) == PanelRead(
        RoundPerformance(1295, 1, 5, 0), "1/5/0", "1,,295")


def test_a_lone_zero_is_read_doubled():
    """2026-10-29 06:53: OCR finds no text in a lone "0" at the row's own size."""
    reader = _Reader(_PANEL_BOXES, [], doubled=["0"])
    assert read_panel(reader, _FRAME, _SCORE, _KDA) == PanelRead(
        RoundPerformance(0, 1, 5, 0), "1/5/0", "0")


def test_a_score_read_at_its_own_size_is_not_read_doubled():
    """2026-10-31 15:37, acct2: doubled, a 1,210 split at the comma, its "1," read
    as a number, and the round was booked at 13210."""
    reader = _Reader(_PANEL_BOXES, ["1,210"], doubled=[("13", 151), ("210", 296)])
    assert read_panel(reader, _FRAME, _SCORE, _KDA) == PanelRead(
        RoundPerformance(1210, 1, 5, 0), "1/5/0", "1,210")
    assert len(reader.calls) == 2, "no doubled read"


def test_a_stray_box_between_the_groups_is_no_reading():
    reader = _Reader(_PANEL_BOXES, [("255", 282), ("1", 177), ("3", 240)])
    assert read_panel(reader, _FRAME, _SCORE, _KDA).reading is None


# -- the round: two frames alike ---------------------------------------------------

def _panel(reading):
    """A frame's read as read_panel gives it: no KDA when there is no reading."""
    return PanelRead(None, "") if reading is None else PanelRead(reading, "k", "s")


def _round(readings, frames=None):
    panels = [_panel(r) for r in readings]
    sleeps = []
    frames = iter(frames if frames is not None else range(100))
    (reading, reads) = read_round(lambda _frame: panels.pop(0) if panels else None,
                                  lambda: next(frames, None), sleeps.append)
    return (reading, len(reads)), sleeps


_A = RoundPerformance(163, 1, 2, 0)


def test_two_frames_alike_are_the_round():
    (reading, reads), sleeps = _round([_A, _A])
    assert (reading, reads) == (_A, 2)
    assert sleeps == [READ_INTERVAL_S]


def test_a_panel_still_drawing_is_read_again():
    """Nothing, then a score still counting up, then the settled figures."""
    counting = RoundPerformance(120, 1, 2, 0)
    (reading, reads), _ = _round([None, counting, _A, _A])
    assert (reading, reads) == (_A, 4)


def test_no_two_alike_is_no_reading():
    (reading, reads), sleeps = _round([_A, None, _A, None, _A])
    assert (reading, reads) == (None, MAX_READS)
    assert len(sleeps) == MAX_READS - 1


def test_no_frame_stops_the_reading():
    (reading, reads), _ = _round([_A, _A], frames=[])
    assert (reading, reads) == (None, 0)


def test_the_frames_read_come_back_with_their_reads():
    panels = [PanelRead(None, "11210"), PanelRead(None, "")] + [None] * (MAX_READS - 2)
    frames = [f"f{k}" for k in range(1, MAX_READS + 1)]
    queue = list(panels)
    reading, reads = read_round(lambda _f: queue.pop(0), iter(frames).__next__, lambda _s: None)
    assert reading is None
    assert reads == list(zip(frames, panels, strict=True))


# -- AI-005-1: why a round was not read --------------------------------------------

def test_an_unread_round_with_no_kda_says_so_with_the_text():
    """2026-10-27/28: every unread panel failed here, on every frame."""
    assert unread_reason([PanelRead(None, ""), PanelRead(None, "11210"), None]) == (
        "no KDA on any of 3 frames: kda ''; kda '11210'; no OCR")


def test_an_unread_round_whose_frames_differed_says_so():
    reasons = unread_reason([PanelRead(RoundPerformance(120, 1, 2, 0), "1/2/0", "120"),
                             PanelRead(None, "1/2/0", "")])
    assert reasons == ("no two of 2 frames read alike: "
                       "kda '1/2/0' score '120'; kda '1/2/0' score ''")


def test_no_frame_at_all():
    assert unread_reason([]) == "no frame to read"


@pytest.mark.parametrize("panel, tag", [
    (PanelRead(RoundPerformance(163, 1, 2, 0), "1/2/0", "163"), "read-163-1-2-0"),
    (PanelRead(None, "1/2/0", ""), "noscore"),
    (PanelRead(None, "112"), "nokda"),
    (None, "noocr")])
def test_a_saved_frame_is_named_for_its_read(panel, tag):
    assert frame_tag(panel) == tag


def test_a_frame_is_saved_as_a_jpeg(tmp_path):
    frame = np.full((12, 16, 3), 200, np.uint8)
    assert save_frame(tmp_path / "rp", frame, "20261029_101500_p1_f1_nokda")
    saved = cv2.imread(str(tmp_path / "rp" / "20261029_101500_p1_f1_nokda.jpg"))
    assert saved is not None and saved.shape == frame.shape


def test_a_failed_save_does_not_raise(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    assert save_frame(blocker / "rp", np.zeros((4, 4, 3), np.uint8), "x") is False


# -- real OCR on the corpus (AI-005-2) --------------------------------------------

def _config_crops():
    crops = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))["crops"]

    def coords(name):
        (x1, y1), (x2, y2) = crops[name]["coords"]
        return CropCoords(x1, y1, x2, y2)
    return coords(SCORE_CROP), coords(KDA_CROP)


# Enumerated, not globbed: each frame and what it must read.
_CASES = [
    ("panel_4_medals_163_1-2-0.jpg", RoundPerformance(163, 1, 2, 0)),
    ("panel_5_medals_800_5-4-3.jpg", RoundPerformance(800, 5, 4, 3)),
    ("panel_no_medals_100_1-5-0.jpg", RoundPerformance(100, 1, 5, 0)),
    ("panel_no_medals_100_1-5-0_aircraft.jpg", RoundPerformance(100, 1, 5, 0)),
    ("panel_no_medals_50_0-3-1.jpg", RoundPerformance(50, 0, 3, 1)),
    ("panel_no_medals_200_1-4-2.jpg", RoundPerformance(200, 1, 4, 2)),
    # 2026-10-29 06:53: a lone "0", which OCR missed until the score row was doubled.
    ("panel_zero_score_0_0-3-0.jpg", RoundPerformance(0, 0, 3, 0)),
    # 2026-10-30: OCR split these at the comma and listed the pieces out of order;
    # the 09:10 one was booked at 25513.
    ("panel_split_score_1295_8-5-3.jpg", RoundPerformance(1295, 8, 5, 3)),
    ("panel_split_score_1255_6-2-2.jpg", RoundPerformance(1255, 6, 2, 2)),
    # 2026-10-31 15:37: doubled, the comma of this 1,210 read as a digit, booked at 13210.
    ("panel_comma_score_1210_7-4-2.jpg", RoundPerformance(1210, 7, 4, 2)),
    ("podium.jpg", None),
    ("scoreboard_a.jpg", None),
    ("scoreboard_b.jpg", None),
]


def _load(name, size=(1200, 1920)):
    path = _CORPUS / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    frame = cv2.imread(str(path))
    h, w = size
    return frame if w == frame.shape[1] else cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)


@pytest.fixture(scope="module")
def reader():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("name, expected", _CASES)
def test_the_corpus_reads_as_labelled(reader, name, expected):
    score_crop, kda_crop = _config_crops()
    assert read_panel(reader, _load(name), score_crop, kda_crop).reading == expected


@pytest.mark.slow
@pytest.mark.parametrize("name, expected", [_CASES[0], _CASES[2]])
def test_a_half_size_frame_reads_too(reader, name, expected):
    score_crop, kda_crop = _config_crops()
    assert read_panel(reader, _load(name, (600, 960)), score_crop, kda_crop).reading == expected


@pytest.mark.slow
def test_an_empty_screen_reads_nothing(reader):
    score_crop, kda_crop = _config_crops()
    frame = np.full((1200, 1920, 3), 180, np.uint8)
    assert read_panel(reader, frame, score_crop, kda_crop).reading is None

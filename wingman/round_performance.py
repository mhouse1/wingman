"""The round's score and kills/deaths/assists, read from the PERFORMANCE panel.

The first click on the end-of-round "Click to Continue..." prompt brings up the
PERFORMANCE panel on the right of the screen: the round's SCORE on the left of
it and its KDA on the right, "163" and "1/2/0" (1 kill, 2 deaths, 0 assists) in
the 2026-10-26 18:45:10 screenshot. The game keeps neither figure in its
PlayerPrefs (wingman/game_stats.py reads the three it does keep), so they are
read off the screen. The session summary prints the totals, to compare long
runs (operator, 2026-10-26).

The panel is drawn centred, and its height follows the rows of medals the round
earned, so its figures sit at a different height from round to round (AI-005-2:
about y 330, 448 and 585 of 1200 px with five medals, four and none). So the
KDA crop is a tall band of the KDA column, and the KDA is the text box in it
that reads as three numbers between slashes with the panel's own "KDA" label
just below it. That label is the check that this is the PERFORMANCE panel: the
scoreboard a few pages later has KDAs in the same column, with its "KDA" header
above them. The score is then read on the KDA's row of the score column. A
frame without that KDA costs one OCR read. A round's reading counts once two
frames in a row read the same, so a panel still fading in is not booked.

`read_panel` runs on the analyzer's OCR pool with that thread's reader
(`GameStateAnalyzer.run_ocr`), as the kill card does: a reader of its own would
cost about 300 MB (Performance 008).

Action Item 005 (AI-005-1): a third of the panels go unread, and every one of
them failed at the KDA on all four frames. So each frame's read keeps the text
OCR returned, the log says which case an unread round was, and `save_frame`
keeps the frames as a corpus to measure the reader against.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import NamedTuple

import cv2

from . import capture_budget

logger = logging.getLogger(__name__)

# The crops in wingman/config.yaml `crops:`.
SCORE_CROP = "PERF_SCORE"
KDA_CROP = "PERF_KDA"

# Where `round_performance.capture` saves the frames (AI-005-1).
CAPTURE_DIR = "test_screenshots/round_performance"

# Frames read for one round, this far apart. The page before the panel (MVP,
# 2ND, 3RD) can still be up when the reader starts: of 46 rounds from
# 2026-10-29 06:01 to 10:59, the panel was first read on frame 1 in 32, frame 2
# in 10, frame 3 in 3 and frame 4 in 1, and a reading takes two frames alike, so
# 4 frames lost that last round. A round with no panel at all costs every frame.
MAX_READS = 6
READ_INTERVAL_S = 0.3

_SCORE_ALLOWLIST = "0123456789,."
# A round's counts run to two digits, without a leading zero: the scoreboard's
# columns read as '63/4/040' on 2026-10-29 03:18, which the old pattern took.
_KDA_RE = re.compile(r"(0|[1-9]\d?)/(0|[1-9]\d?)/(0|[1-9]\d?)")
# A score is up to three digits, or thousands groups with a separator between
# each: OCR splits "1,255" at the comma, and a group of other than three digits
# means a box was misread or lost. The panel prints the comma, and all 28 saved
# scores of 1,000 or more read with it (2026-10-31), so a longer run of digits
# is a comma read as a digit.
_SCORE_RE = re.compile(r"0|[1-9]\d{0,2}(?:,\d{3})*")
# The panel's "KDA" label sits this far below the KDA figures, as a share of
# the frame's height: 60 to 62 px of 1200 at all three heights measured.
_LABEL_BELOW = (0.03, 0.075)
# The score is read this far above and below the KDA's middle: the figures, not
# the SCORE label under them.
_ROW_HALF = 0.033


class RoundPerformance(NamedTuple):
    score: int
    kills: int
    deaths: int
    assists: int


class PanelRead(NamedTuple):
    """One frame's read of the panel: its figures, or None, and the OCR text."""

    reading: "RoundPerformance | None"
    kda_text: str
    score_text: "str | None" = None     # None: not read, because the KDA did not parse


def _digits(text: str) -> str:
    """OCR text with the spaces dropped and the letter O read as the zero it is
    in the panel's figures."""
    return "".join(text.split()).replace("O", "0").replace("o", "0")


def parse_kda(text: str) -> "tuple[int, int, int] | None":
    """(kills, deaths, assists) from a text box; None unless it is exactly
    three counts between slashes. A slash read as a digit fails here rather
    than booking a wrong count."""
    m = _KDA_RE.fullmatch(_digits(text))
    return None if m is None else (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def parse_score(text: str) -> "int | None":
    """The score from the score crop's text, thousands separators dropped; None
    unless its digit groups are those of a number."""
    grouped = re.sub(r",+", ",", _digits(text).replace(".", ","))
    return int(grouped.replace(",", "")) if _SCORE_RE.fullmatch(grouped) else None


def _region(frame, crop, y1: float, y2: float):
    """(the crop's columns between heights y1 and y2, grey and scaled to the
    1920 px width it was measured at, the scale, its top row in the frame)."""
    h, w = frame.shape[:2]
    top = max(0, int(y1 * h))
    region = frame[top:max(top, int(y2 * h)), int(crop.x1 * w):int(crop.x2 * w)]
    if region.size == 0:
        return None, 1.0, top
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    scale = max(1.0, 1920.0 / max(1, w))
    if scale > 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray, scale, top


def _find_kda(reader, frame, kda_crop) -> "tuple[str, tuple | None, float | None]":
    """(the text read, (kills, deaths, assists) or None, the KDA's middle as a
    share of the frame's height). The KDA is a box that parses as one with a
    "KDA" label box below it at the panel's spacing."""
    h = frame.shape[0]
    gray, scale, top = _region(frame, kda_crop, kda_crop.y1, kda_crop.y2)
    if gray is None:
        return "", None, None
    boxes = [(str(text), top + (box[0][1] + box[2][1]) / 2 / scale)
             for box, text, _conf in reader.readtext(gray, detail=1, paragraph=False, workers=0)]
    labels = [y for text, y in boxes
              if "".join(c for c in text.upper() if c.isalpha()).startswith("KD")]
    for text, y in boxes:
        kda = parse_kda(text)
        if kda is not None and any(_LABEL_BELOW[0] * h <= label - y <= _LABEL_BELOW[1] * h
                                   for label in labels):
            return text, kda, y / h
    return " | ".join(text for text, _y in boxes), None, None


def _score_text(reader, gray) -> str:
    # Left to right, a separator between boxes: OCR splits "1,255" at the comma
    # and lists the pieces in no set order. Joined as listed, a 1,255 was booked
    # as 25513 (2026-10-30 09:10, acct3), and the 03:48 frame of a 1,295 reads 2951.
    boxes = reader.readtext(gray, detail=1, paragraph=False, workers=0,
                            allowlist=_SCORE_ALLOWLIST)
    return ",".join(str(text) for _x, text in
                    sorted((min(p[0] for p in box), text) for box, text, _conf in boxes))


def _read_score(reader, frame, score_crop, row: float) -> str:
    gray, _scale, _top = _region(frame, score_crop, row - _ROW_HALF, row + _ROW_HALF)
    if gray is None:
        return ""
    # Doubled only when the row at its own size gives nothing, which is a lone
    # "0": all 36 saved. Doubling splits a score at its comma, 10 of the 28 saved
    # scores of 1,000 or more, and can read the comma as a digit: a 1,210 was
    # booked as 13210 (2026-10-31 15:37, acct2). None of the 383 saved panels
    # split at its own size, and all read right.
    return (_score_text(reader, gray)
            or _score_text(reader, cv2.resize(gray, None, fx=2, fy=2,
                                              interpolation=cv2.INTER_CUBIC)))


def read_panel(reader, frame, score_crop, kda_crop) -> PanelRead:
    """One frame's read: the panel's figures, or None when it does not show
    them, with the text OCR returned for each crop it read."""
    kda_text, kda, row = _find_kda(reader, frame, kda_crop)
    if kda is None:
        return PanelRead(None, kda_text)
    score_text = _read_score(reader, frame, score_crop, row)
    score = parse_score(score_text)
    return PanelRead(None if score is None else RoundPerformance(score, *kda),
                     kda_text, score_text)


def read_round(read_fn, grab_frame, sleep_fn=time.sleep) -> "tuple[RoundPerformance | None, list]":
    """(the round's figures, [(frame, its PanelRead)] for each frame read). The
    figures are the first two frames in a row that read alike, or None when
    none do in `MAX_READS` frames.

    `read_fn(frame)` returns `read_panel`'s answer for one frame, or None when
    OCR could not run; `grab_frame()` a fresh frame, or None when there is none."""
    previous = None
    reads = []
    while len(reads) < MAX_READS:
        if reads:
            sleep_fn(READ_INTERVAL_S)
        frame = grab_frame()
        if frame is None:
            break
        panel = read_fn(frame)
        reads.append((frame, panel))
        reading = None if panel is None else panel.reading
        if reading is not None and reading == previous:
            return reading, reads
        previous = reading
    return None, reads


def describe_read(panel: "PanelRead | None") -> str:
    """One frame's read for the log: what OCR returned for each crop it read."""
    if panel is None:
        return "no OCR"
    text = f"kda {panel.kda_text!r}"
    return text if panel.score_text is None else f"{text} score {panel.score_text!r}"


def unread_reason(panels: list) -> str:
    """Why a round's panel was not read, for its `not read` line: no frame
    showed a KDA, or the frames that did never read alike twice in a row."""
    if not panels:
        return "no frame to read"
    kda_seen = any(p is not None and p.score_text is not None for p in panels)
    head = (f"no two of {len(panels)} frames read alike" if kda_seen
            else f"no KDA on any of {len(panels)} frames")
    return head + ": " + "; ".join(describe_read(p) for p in panels)


def frame_tag(panel: "PanelRead | None") -> str:
    """A saved frame's name ends in what the reader made of it."""
    if panel is None:
        return "noocr"
    if panel.reading is not None:
        r = panel.reading
        return f"read-{r.score}-{r.kills}-{r.deaths}-{r.assists}"
    return "noscore" if panel.score_text is not None else "nokda"


def save_frame(directory, frame, name: str) -> bool:
    """Save one frame for the reader's corpus (AI-005-1), a JPEG at quality 95,
    within the capture budget. False when it was not written. Never raises."""
    try:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        if not capture_budget.admit(directory, "Round performance capture"):
            return False
        return bool(cv2.imwrite(str(directory / f"{name}.jpg"), frame,
                                [cv2.IMWRITE_JPEG_QUALITY, 95]))
    except Exception as e:
        logger.warning("Round performance capture: write failed: %s: %s", type(e).__name__, e)
        return False

"""The squadron's place on the game's squadron leaderboard (Design 018 Phase 3, ADR 166).

Between rounds, in the lobby, wingman opens the menu at the top right, then
LEADERBOARDS, then the SQUADRONS tab, reads its own squadron's row (pinned at
the bottom of the list, highlighted: rank and trophies), and goes back with the
page's own back arrow (operator, 2026-10-31, from four screenshots taken by
hand). ESC is never pressed: on the lobby it opens Exit to Desktop.

Each step is clicked only once the screen shows the step before it, and the
reading takes two frames alike, as the PERFORMANCE panel's does.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import NamedTuple

import cv2

logger = logging.getLogger(__name__)

# The crops in wingman/config.yaml `crops:`.
MENU = "SQ_MENU"                          # the lobby's menu button, top right
MENU_LEADERBOARDS = "SQ_MENU_LEADERBOARDS"  # LEADERBOARDS in the open menu
BOARD_TITLE = "SQ_BOARD_TITLE"            # LEADERBOARDS, the page's title
BACK = "SQ_BACK"                          # the page's back arrow, left of its title
TAB_SQUADRONS = "SQ_TAB_SQUADRONS"        # the SQUADRONS tab
BOARD_HEADER = "SQ_BOARD_HEADER"          # CURRENT TOP SQUADRONS (PLAYERS on the first tab)
OWN_RANK = "SQ_OWN_RANK"                  # our row: the rank,
OWN_NAME = "SQ_OWN_NAME"                  # the squadron's name,
OWN_TROPHIES = "SQ_OWN_TROPHIES"          # and its trophies
PLAY = "PLAY"                             # the lobby's own crop: the lobby is up
CROPS = (MENU, MENU_LEADERBOARDS, BOARD_TITLE, BACK, TAB_SQUADRONS, BOARD_HEADER,
         OWN_RANK, OWN_NAME, OWN_TROPHIES, PLAY)

# Where `squadron_ranking.capture` saves the frames: the one each reading was
# taken from, and the last one of a navigation that gave up.
CAPTURE_DIR = "test_screenshots/squadron_ranking"

# A step's screen is looked for this often, until the step's time is up.
POLL_S = 0.4
# Frames read for one reading, this far apart; two alike make the reading.
MAX_READS = 6
READ_INTERVAL_S = 0.5

_NUMBER_ALLOWLIST = "0123456789,."
# A count is one run of digits, or thousands groups with a separator between
# each. OCR splits "358,032" at the comma, and lists the pieces in no set order
# (Action Item 005, 2026-10-30: a 1,255 read as 25513).
_COUNT_RE = re.compile(r"0|[1-9]\d{0,7}|[1-9]\d{0,2}(?:,\d{3})+")


class SquadronReading(NamedTuple):
    rank: int
    trophies: int


class RowRead(NamedTuple):
    """One frame's read of the squadron leaderboard: the reading, or None, and
    what OCR returned for each crop it read."""

    reading: "SquadronReading | None"
    header: str
    name: str = ""
    rank_text: str = ""
    trophies_text: str = ""


def letters(text: str) -> str:
    """Upper-case letters only: OCR spaces "LEADERBOARDS" and "MOBIUS" apart."""
    return "".join(c for c in text.upper() if c.isalpha())


def parse_count(text: str) -> "int | None":
    """A rank or a trophy count from its crop's text, separators dropped; None
    unless its digit groups are those of a number."""
    grouped = re.sub(r",+", ",", "".join(text.split()).replace("O", "0").replace(".", ","))
    return int(grouped.replace(",", "")) if _COUNT_RE.fullmatch(grouped) else None


def _crop(frame, crop, scale: float = 1.0):
    h, w = frame.shape[:2]
    region = frame[int(crop.y1 * h):int(crop.y2 * h), int(crop.x1 * w):int(crop.x2 * w)]
    if region.size == 0:
        return None
    # Read at the 1920 px width the crops were measured at, or larger.
    scale *= max(1.0, 1920.0 / max(1, w))
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    if scale != 1.0:
        gray = cv2.resize(gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return gray


def crop_text(reader, frame, crop) -> str:
    """The crop's words, left to right."""
    gray = _crop(frame, crop)
    if gray is None:
        return ""
    boxes = reader.readtext(gray, detail=1, paragraph=False, workers=0)
    return " ".join(str(text) for _x, text in
                    sorted((min(p[0] for p in box), text) for box, text, _conf in boxes))


def count_text(reader, frame, crop) -> str:
    """A number's crop, doubled, its boxes left to right with a separator between
    them, so that a box out of place cannot pass as a digit group."""
    gray = _crop(frame, crop, scale=2.0)
    if gray is None:
        return ""
    boxes = reader.readtext(gray, detail=1, paragraph=False, workers=0,
                            allowlist=_NUMBER_ALLOWLIST)
    return ",".join(str(text) for _x, text in
                    sorted((min(p[0] for p in box), text) for box, text, _conf in boxes))


def shows(reader, frame, crop, word: str) -> bool:
    """True when the crop reads the word, spaces and case aside."""
    return letters(word) in letters(crop_text(reader, frame, crop))


def read_row(reader, frame, crops, squadron: str) -> RowRead:
    """One frame's read of our squadron's row: only on the SQUADRONS tab (the
    first tab, TOTAL TROPHIES, pins our own player's row in the same place) and
    only with the squadron's name in the row."""
    header = crop_text(reader, frame, crops[BOARD_HEADER])
    if "SQUADRONS" not in letters(header):
        return RowRead(None, header)
    name = crop_text(reader, frame, crops[OWN_NAME])
    if letters(squadron) not in letters(name):
        return RowRead(None, header, name)
    rank_text = count_text(reader, frame, crops[OWN_RANK])
    trophies_text = count_text(reader, frame, crops[OWN_TROPHIES])
    rank, trophies = parse_count(rank_text), parse_count(trophies_text)
    reading = (SquadronReading(rank, trophies)
               if rank is not None and rank > 0 and trophies is not None else None)
    return RowRead(reading, header, name, rank_text, trophies_text)


def describe(row: "RowRead | None") -> str:
    """One frame's read for the log."""
    if row is None:
        return "no OCR"
    return (f"header {row.header!r} name {row.name!r} rank {row.rank_text!r} "
            f"trophies {row.trophies_text!r}")


class _Stopped(Exception):
    """The operator stopped wingman, or a stop is pending: leave at once."""


class Navigation:
    """One trip from the lobby to the squadron leaderboard and back.

    `grab_frame()` gives a fresh frame or None; `ocr(fn, *args)` runs
    `fn(reader, *args)` with an EasyOCR reader and returns its answer, or None;
    `click(crop_name)` clicks a crop; `stop()` is true once wingman must leave.
    """

    def __init__(self, crops, grab_frame, ocr, click, stop, squadron: str,
                 step_timeout_s: float = 8.0, sleep_fn=time.sleep, clock=time.monotonic):
        self.crops = crops
        self.grab_frame = grab_frame
        self.ocr = ocr
        self.click = click
        self.stop = stop
        self.squadron = squadron
        self.step_timeout_s = step_timeout_s
        self.sleep_fn = sleep_fn
        self.clock = clock
        self.last_frame = None
        self.reads: list = []

    def _shows(self, frame, crop: str, word: str) -> bool:
        return bool(self.ocr(shows, frame, self.crops[crop], word))

    def _in_lobby(self, frame) -> bool:
        """PLAY, with the menu shut: the open menu leaves most of PLAY in view."""
        return self._shows(frame, PLAY, "PLAY") and not self._shows(
            frame, MENU_LEADERBOARDS, "LEADERBOARDS")

    def _wait_until(self, check, timeout_s: "float | None" = None) -> bool:
        """True once a fresh frame passes `check`; False when the step's time
        runs out (one frame at a timeout of 0)."""
        deadline = self.clock() + (self.step_timeout_s if timeout_s is None else timeout_s)
        while True:
            if self.stop():
                raise _Stopped
            frame = self.grab_frame()
            if frame is not None:
                self.last_frame = frame
                if check(frame):
                    return True
            if self.clock() >= deadline:
                return False
            self.sleep_fn(POLL_S)

    def _wait_for(self, crop: str, word: str) -> bool:
        return self._wait_until(lambda frame: self._shows(frame, crop, word))

    def _read(self) -> "SquadronReading | None":
        previous = None
        for i in range(MAX_READS):
            if i:
                self.sleep_fn(READ_INTERVAL_S)
            if self.stop():
                raise _Stopped
            frame = self.grab_frame()
            if frame is None:
                continue
            self.last_frame = frame
            row = self.ocr(read_row, frame, self.crops, self.squadron)
            self.reads.append((frame, row))
            reading = None if row is None else row.reading
            if reading is not None and reading == previous:
                return reading
            previous = reading
        return None

    def _go_back(self) -> bool:
        """Back to the lobby by the screens' own controls: the back arrow on the
        leaderboard, the menu button again on the open menu. True once the lobby
        shows."""
        for _attempt in range(3):
            frame = self.grab_frame()
            if frame is None:
                self.sleep_fn(POLL_S)
                continue
            self.last_frame = frame
            if self._in_lobby(frame):
                return True
            if self._shows(frame, BOARD_TITLE, "LEADERBOARDS"):
                self.click(BACK)
            elif self._shows(frame, MENU_LEADERBOARDS, "LEADERBOARDS"):
                self.click(MENU)
            if self._wait_until(self._in_lobby):
                return True
        return False

    def run(self) -> "tuple[SquadronReading | None, str]":
        """(the reading or None, what happened). Never raises for a screen that
        is not what it should be: it goes back and says why."""
        try:
            if not self._wait_until(self._in_lobby, timeout_s=0.0):
                return None, "the lobby is not on screen"
            self.click(MENU)
            if not self._wait_for(MENU_LEADERBOARDS, "LEADERBOARDS"):
                return None, self._back("the menu did not open")
            self.click(MENU_LEADERBOARDS)
            if not self._wait_for(BOARD_TITLE, "LEADERBOARDS"):
                return None, self._back("the leaderboards did not open")
            self.click(TAB_SQUADRONS)
            reading = self._read()
            reason = "read" if reading is not None else (
                f"no two of {len(self.reads)} frames read alike")
            return reading, self._back(reason)
        except _Stopped:
            return None, "stopped: wingman is leaving"

    def _back(self, reason: str) -> str:
        if self._go_back():
            return reason
        return f"{reason}; NOT back in the lobby"


class SquadronRanking:
    """When the next reading is due, and the one navigation in flight.

    A reading is due at the first lobby of the session and `every_s` after the
    last attempt, read or not, and at most once in a lobby: the lobby must be
    left (`lobby_left`) before the next. Without that, `every_s` 0 read the
    leaderboard back to back and never started a round (2026-10-31 02:17).
    While one is due or running, the lobby's PLAY click waits (`holds_play`);
    while one runs, the lobby quick-scan, the escape loop and the deferred exit
    wait too (`active`)."""

    def __init__(self, enabled: bool, every_s: float, clock=time.monotonic):
        self.enabled = enabled
        self.every_s = every_s
        self.clock = clock
        self._last_attempt: "float | None" = None
        self._tried_this_lobby = False
        self._active = threading.Event()

    def due(self) -> bool:
        return (self.enabled and not self._active.is_set() and not self._tried_this_lobby
                and (self._last_attempt is None
                     or self.clock() - self._last_attempt >= self.every_s))

    def lobby_left(self) -> None:
        """The game left the lobby: the next lobby may take a reading."""
        self._tried_this_lobby = False

    def active(self) -> bool:
        return self._active.is_set()

    def holds_play(self) -> bool:
        return self.active() or self.due()

    def start(self, navigation: Navigation, on_done) -> threading.Thread:
        """Run the navigation on a thread of its own; `on_done(reading, why,
        navigation)` gets its answer. The attempt counts from now, so a
        navigation that gives up is not retried before `every_s`, nor in this
        lobby. Active first: `holds_play` must not drop for an instant between
        a reading being due and its navigation running (2026-10-31 02:17:56,
        the quick-scan clicked PLAY in that gap)."""
        self._active.set()
        self._tried_this_lobby = True
        self._last_attempt = self.clock()

        def _run():
            try:
                reading, why = navigation.run()
            except Exception:
                logger.exception("SQUADRON RANKING: the navigation failed")
                reading, why = None, "the navigation failed"
            finally:
                self._active.clear()
            on_done(reading, why, navigation)

        thread = threading.Thread(target=_run, daemon=True, name="squadron-ranking")
        thread.start()
        return thread

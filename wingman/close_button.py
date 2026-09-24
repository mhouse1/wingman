"""Generic close-button (the white cross) finder and the GAME_UNKNOWN recovery
that uses it (ADR 146, 2026-09-24).

Why it exists: on 2026-09-24 a full-screen promotional window ("A-10 Thunderbolt,
limited time offer") covered the lobby at start-up. It matches none of the
popups wingman knows (each is an OCR-detected, hand-calibrated crop, ADR 074), so
the classifier sat in GAME_UNKNOWN for three minutes and the stuck-state recovery
only warns. The game draws every modal's close button with one white-cross
sprite at different sizes (measured: about 29 px on the A-10 window, 38 px on
another player's profile screen, 45 px on the flight-pass window, all on a 1920 x 1200
frame), so one detector covers windows nobody has calibrated yet.

ESC is not the answer: pressed on a plain lobby it opens "Exit to Desktop" with
Exit as the highlighted default (measured live, 2026-09-24), so a blind key
press from a stuck classifier could quit the game.

Matching is on a white-pixel mask, not on colour, because the window behind the
cross is a photograph. Measured over the 3,258 full-size frames in
`test_screenshots/` (multi-scale, half resolution): real close buttons scored
0.951 (flight-pass window), 0.859 (another player's profile screen, two frames)
and 0.937 (the A-10 window the sprite was cut from, at the nearest scale in the
grid); the best score on any other non-gameplay frame was 0.747, on the "Exit to
Desktop" dialog. The default threshold, 0.82, sits between them. Three genuine
examples is a thin sample: the recovery below is therefore rate-limited and only
runs when the game has been unclassifiable for a long time.
"""

import logging
import time
from typing import NamedTuple

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# The sprite as measured on the A-10 window (29 x 29 px, 278 white pixels).
_SPRITE_ROWS = (
    "..##......................##.",
    ".####....................####",
    "######..................#####",
    ".######................#####.",
    "..######..............#####..",
    "...######............#####...",
    "....######..........#####....",
    ".....######........#####.....",
    "......######......#####......",
    ".......######....#####.......",
    "........######..#####........",
    ".........###########.........",
    "..........#########..........",
    "...........#######...........",
    "............######...........",
    "...........########..........",
    "..........##########.........",
    ".........#####.######........",
    "........#####...######.......",
    ".......#####.....######......",
    "......#####.......######.....",
    ".....#####.........######....",
    "....#####...........######...",
    "...#####.............######..",
    "..#####...............######.",
    ".#####.................######",
    "#####...................#####",
    "..##.....................###.",
    "..#.......................#..",
)
_SPRITE = np.array([[1.0 if ch == "#" else 0.0 for ch in row] for row in _SPRITE_ROWS],
                   dtype=np.float32)

# Sprite sizes seen on a 1920 x 1200 frame are 29-45 px, so 0.9-1.65 of the source.
# 0.75 is left out on purpose: at 22 px the measured false matches in ordinary
# frames were the highest of any scale.
DEFAULT_SCALES = (0.9, 1.05, 1.25, 1.45, 1.65)
DEFAULT_MIN_SCORE = 0.82
_WHITE_MIN = 225        # every channel at least this counts as "white"
_WORK_SCALE = 0.5       # match at half resolution: 4x fewer pixels, same result


class CloseHit(NamedTuple):
    x: int              # centre, in frame pixels
    y: int
    score: float        # normalised correlation of the white masks, 0..1
    scale: float        # sprite size relative to the 29 px source


def _white_mask(frame: np.ndarray) -> np.ndarray:
    return (frame.min(axis=2) >= _WHITE_MIN).astype(np.float32)


def find_close_button(frame: "np.ndarray | None", min_score: float = DEFAULT_MIN_SCORE,
                      scales: "tuple[float, ...]" = DEFAULT_SCALES) -> "CloseHit | None":
    """The best close-button candidate in a BGR frame, or None below `min_score`."""
    if frame is None or frame.ndim != 3 or frame.shape[0] < 300 or frame.shape[1] < 300:
        return None
    mask = cv2.resize(_white_mask(frame), None, fx=_WORK_SCALE, fy=_WORK_SCALE,
                      interpolation=cv2.INTER_AREA)
    best: "CloseHit | None" = None
    for scale in scales:
        tpl = cv2.resize(_SPRITE, None, fx=scale * _WORK_SCALE, fy=scale * _WORK_SCALE,
                         interpolation=cv2.INTER_AREA)
        if mask.shape[0] < tpl.shape[0] + 2 or mask.shape[1] < tpl.shape[1] + 2:
            continue
        result = cv2.matchTemplate(mask, tpl, cv2.TM_CCOEFF_NORMED)
        _, top, _, loc = cv2.minMaxLoc(result)
        if best is None or top > best.score:
            best = CloseHit(int((loc[0] + tpl.shape[1] / 2.0) / _WORK_SCALE),
                            int((loc[1] + tpl.shape[0] / 2.0) / _WORK_SCALE),
                            float(top), float(scale))
    if best is None or best.score < min_score:
        return None
    return best


def click_region(hit: CloseHit, frame_shape: "tuple[int, ...]", half: int = 24
                 ) -> "tuple[float, float, float, float]":
    """The (x1, y1, x2, y2) frame fractions of a small box centred on `hit`,
    for `Controller.click_crop`, which takes fractional crop coordinates. The
    click lands at the box centre, so `half` only bounds the box at the frame edge."""
    height, width = frame_shape[:2]
    return (max(0.0, (hit.x - half) / width), max(0.0, (hit.y - half) / height),
            min(1.0, (hit.x + half) / width), min(1.0, (hit.y + half) / height))


class GenericCloseRecovery:
    """Click a close button when GAME_UNKNOWN has outlasted every known remedy.

    Called once per main-loop tick with the frame and whether the game state is
    GAME_UNKNOWN. It returns a `CloseHit` when a click is due, else None, and
    the caller performs the click — this class holds policy only.

    Guards, all measured against the 2026-09-24 incident (47 timeouts over about
    three minutes before a hand click cleared it): nothing happens until the
    state has been unknown for `min_stuck_s`, well past the 1-2 s a normal
    classification takes and past the known-popup scan; a search runs at most
    once per `retry_interval_s`; at most `max_clicks` clicks per stuck episode;
    and the episode resets the moment the state leaves GAME_UNKNOWN.
    """

    def __init__(self, cfg: "dict | None" = None, clock=time.time) -> None:
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", False))
        self._min_stuck_s = float(cfg.get("min_stuck_s", 25.0))
        self._retry_interval_s = float(cfg.get("retry_interval_s", 15.0))
        self._max_clicks = int(cfg.get("max_clicks", 3))
        self._min_score = float(cfg.get("min_score", DEFAULT_MIN_SCORE))
        self._clock = clock
        self._since = 0.0
        self._last_search_ts = 0.0
        self._clicks = 0

    @property
    def enabled(self) -> bool:
        return self._enabled

    def tick(self, frame: "np.ndarray | None", unknown: bool) -> "CloseHit | None":
        if not self._enabled:
            return None
        now = self._clock()
        if not unknown:
            self._since = 0.0
            self._last_search_ts = 0.0
            self._clicks = 0
            return None
        if self._since == 0.0:
            self._since = now
            return None
        stuck_for = now - self._since
        if (stuck_for < self._min_stuck_s or self._clicks >= self._max_clicks
                or (self._last_search_ts and now - self._last_search_ts < self._retry_interval_s)):
            return None
        self._last_search_ts = now
        hit = find_close_button(frame, min_score=self._min_score)
        if hit is None:
            logger.debug("GenericCloseRecovery: GAME_UNKNOWN for %.0fs, no close button "
                         "found (threshold %.2f)", stuck_for, self._min_score)
            return None
        self._clicks += 1
        logger.info("\033[93m📋 GenericCloseRecovery: GAME_UNKNOWN for %.0fs — clicking a "
                    "close button at (%d,%d), score %.3f, scale %.2f (click %d of %d)\033[0m",
                    stuck_for, hit.x, hit.y, hit.score, hit.scale, self._clicks,
                    self._max_clicks)
        return hit

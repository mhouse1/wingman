"""Code Review 019 cycle 19: what ended each life, read from the game's kill card.

After an enemy kill the respawn screen carries a card along its bottom edge,
"KILLED BY | AIRCRAFT | WEAPON | HEALTH | ASSIST", naming the killer and the
weapon. After the terrain crash at 2026-10-17 00:44:18 (a dive after an A-4 into
canyon spires) the screen had none. Of the 17 deaths since 00:14 that the black
box kept, 16 had a card naming an enemy and a weapon (guns, R-77, AIM-54, Meteor,
MICA, AIM-7); the session summary's own count (ADR 143, from missile-alert and
dive-emergency recency) had put terrain at 7 of 34 armed deaths the session
before. Terrain avoidance is judged on its terrain deaths, so this counts them
from the card. Shadow: one DEATH CARD line per death and a summary at the end;
nothing steers on it.

The reads run off the main loop: an EasyOCR read of the three crops takes tens
of milliseconds on an idle CPU and seconds on a loaded one. A thread here waits
for each read, which `read_fn` runs on the analyzer's OCR pool with that pool
thread's reader (`GameStateAnalyzer.run_ocr`): a reader of its own would cost
about 300 MB (Performance 008).
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from dataclasses import dataclass

import cv2

logger = logging.getLogger(__name__)

# The card's crops as (x1, y1, x2, y2) fractions of the frame, measured on the
# black box's 960x600 frames of 2026-10-17: the "KILLED BY" heading, the
# killer's name with its squadron line, and the weapon's name under its icon.
HEADER_BOX = (0.06, 0.79, 0.30, 0.835)
KILLER_BOX = (0.08, 0.875, 0.32, 0.955)
WEAPON_BOX = (0.50, 0.93, 0.70, 0.975)

_MIN_CONF = 0.2


@dataclass(frozen=True)
class DeathCard:
    """One death's reading of the respawn screen."""

    reads: int                  # respawn-screen frames read
    card_reads: int = 0         # of them, with the KILLED BY heading
    killer: str = ""
    weapon: str = ""

    @property
    def verdict(self) -> str:
        """ "enemy" (a card), "no card" (read, never a card: a crash or
        self-inflicted) or "unread" (no frame reached the reader)."""
        if self.card_reads:
            return "enemy"
        return "no card" if self.reads else "unread"


def _crop(frame, box):
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = box
    return frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]


def _read(reader, frame, box) -> str:
    crop = _crop(frame, box)
    if crop.size == 0:
        return ""
    # Upscaled to twice the black box's 960 px width: its 2x reads were measured.
    scale = max(1.0, 1920.0 / max(1, frame.shape[1]))
    if scale > 1.0:
        crop = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    return " ".join(text for _, text, conf in reader.readtext(crop, detail=1, paragraph=False)
                    if conf > _MIN_CONF).strip()


def has_heading(text: str) -> bool:
    """True when OCR text from the heading crop is the card's KILLED BY."""
    letters = "".join(c for c in text.upper() if c.isalpha())
    return "KILLED" in letters or "KILLEDBY" in letters


def read_card(reader, frame) -> "tuple[bool, str, str]":
    """(heading seen, killer, weapon) from one respawn-screen frame. The two
    names are read only when the heading is there."""
    if not has_heading(_read(reader, frame, HEADER_BOX)):
        return False, "", ""
    return True, _read(reader, frame, KILLER_BOX), _read(reader, frame, WEAPON_BOX)


class KillCardReader:
    """Reads the kill card on each death's respawn-screen frames, off the main
    loop. `submit` each frame of the screen, `end` when it goes: that logs the
    death's DEATH CARD line and returns its `DeathCard`. `stop` at shutdown
    logs the session's counts.

    `read_fn(frame)` returns `read_card`'s (heading seen, killer, weapon) for
    one frame, or None when it could not read it; it blocks, on the reader's
    own thread.
    """

    def __init__(self, read_fn, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self._read_fn = read_fn
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: "threading.Thread | None" = None
        self._pending = None            # (death, frame), latest wins
        self._death = 0                 # the death the frames now belong to
        self._reads = 0
        self._card_reads = 0
        self._killer = ""
        self._weapon = ""
        self._verdicts: Counter = Counter()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kill-card", daemon=True)
        self._thread.start()

    def submit(self, frame) -> None:
        """Hand the reader one frame of the respawn screen; returns at once. A
        frame it has not taken yet is replaced by this one."""
        if self._thread is None or frame is None or getattr(frame, "ndim", 0) != 3:
            return
        with self._lock:
            self._pending = (self._death, frame)
        self._wake.set()

    def end(self) -> "DeathCard | None":
        """Close this death: log its DEATH CARD line and return it. A read
        still under way is not waited for; its answer is dropped."""
        if self._thread is None:
            return None
        with self._lock:
            card = DeathCard(self._reads, self._card_reads, self._killer, self._weapon)
            self._death += 1
            self._pending = None
            self._reads = self._card_reads = 0
            self._killer = self._weapon = ""
        self._verdicts[card.verdict] += 1
        if card.verdict == "enemy":
            logger.info("DEATH CARD: killed by '%s' with '%s' (the card on %d of %d reads)",
                        card.killer, card.weapon, card.card_reads, card.reads)
        elif card.verdict == "no card":
            logger.info("DEATH CARD: no kill card on %d read(s) of the respawn screen: "
                        "a crash or self-inflicted", card.reads)
        else:
            logger.info("DEATH CARD: not read (no respawn-screen frame reached the reader)")
        return card

    def summary(self) -> "str | None":
        deaths = sum(self._verdicts.values())
        if not deaths:
            return None
        return ("DEATH CARDS: %d deaths: enemy %d, no card %d, unread %d" % (
            deaths, self._verdicts["enemy"], self._verdicts["no card"], self._verdicts["unread"]))

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the thread and log the session's counts."""
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        line = self.summary()
        if line:
            logger.info(line)

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._wake.wait(timeout=0.5):
                continue
            self._wake.clear()
            with self._lock:
                item, self._pending = self._pending, None
            if item is None:
                continue
            death, frame = item
            try:
                result = self._read_fn(frame)
            except Exception:
                logger.debug("Kill card: read failed", exc_info=True)
                continue
            if result is None:
                continue
            seen, killer, weapon = result
            with self._lock:
                if death != self._death:
                    continue                    # the death closed while this was read
                self._reads += 1
                if seen:
                    self._card_reads += 1
                    # The longest names read win: a later frame may be sharper.
                    if len(killer) > len(self._killer):
                        self._killer = killer
                    if len(weapon) > len(self._weapon):
                        self._weapon = weapon
            logger.debug("KILLCARD: heading=%s killer=%r weapon=%r", seen, killer, weapon)

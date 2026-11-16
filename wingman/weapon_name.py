"""The selected weapon, read from its name on the weapon panel (ADR 180 D1).

The weapon panel (bottom right) prints the selected weapon's name above its slot,
right-aligned to x 1901 on 1920 x 1200: "LAU-3 Hydra Rocket Pod", "AIM-9M
Sidewinder", "PL-8" in the operator's screenshots of 2026-11-15. The name is
sorted into a class by `weapon.classes` in config.yaml, in order, each with the
way `f` fires it and the words its names contain. A new weapon needs a word in
that table, not a new detector; a name in no class keeps the tap that every
weapon had before, and is logged once so it can be added.

EasyOCR's misreads of these labels (2026-11-15): "LAU- 3 Hydro Rocket Pod",
"LAU 3 Hydra RRocket Pod", "AIM-9M Sidewindel". So a word matches anywhere in
the name with its punctuation and spaces taken out ("AIM9" in "AIM9MSIDEWINDEL"),
or, five letters or longer, against a single word of the name at a similarity
of 0.8 or more.
"""

from __future__ import annotations

import difflib
import logging
import re
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# How the active-weapon key fires a class: tapped about every 0.35 s by the
# pursuit, or held while the weapon's lead mark is on the crosshair.
FIRE_MODES = ("tap", "hold_on_lead")
_FUZZY_MIN_LEN = 5
_FUZZY_RATIO = 0.8


@dataclass(frozen=True)
class SelectedWeapon:
    name: str                 # as read
    cls: "str | None"         # the class it matched; None for a name in no class
    fire: str                 # one of FIRE_MODES; "tap" when cls is None


def _compact(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def classify(name: str, classes: dict) -> "tuple[str | None, str]":
    """(class, fire mode) of a weapon name, by the first class with a matching
    word; (None, "tap") when none matches."""
    compact = _compact(name)
    tokens = [_compact(t) for t in re.split(r"[\s\-/]+", name) if _compact(t)]
    for cls, spec in (classes or {}).items():
        for word in spec.get("names") or ():
            w = _compact(str(word))
            if not w:
                continue
            if w in compact:
                return cls, spec.get("fire", "tap")
            if len(w) >= _FUZZY_MIN_LEN and any(
                    difflib.SequenceMatcher(None, w, t).ratio() >= _FUZZY_RATIO
                    for t in tokens):
                return cls, spec.get("fire", "tap")
    return None, "tap"


class WeaponNameReader:
    """The newest reading of the selected weapon's name, for the controller to ask.
    Written by an OCR worker's done-callback, read by the steering threads."""

    def __init__(self, classes: dict, stale_after_s: float = 6.0, clock=time.monotonic):
        self._classes = dict(classes or {})
        self._stale_after_s = float(stale_after_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._weapon: "SelectedWeapon | None" = None
        self._ts = 0.0
        self._unknown_logged: "set[str]" = set()
        self._unclassed_run = 0

    def note_text(self, text: "str | None") -> "SelectedWeapon | None":
        """Record one OCR read of the label. Blank text (the panel not drawn, or
        nothing read) changes nothing; it lets the last reading go stale.

        A name in no class replaces a classed weapon only on its second read in
        a row: over bright ground "R-77 Adder" read as "RiZZ Adder" and "RZZ
        lidiel" (2026-11-13), and one such read of the rocket pod's name must not
        hand its `f` back to the tap."""
        text = " ".join((text or "").split())
        if len(_compact(text)) < 2:
            return None
        cls, fire = classify(text, self._classes)
        weapon = SelectedWeapon(text, cls, fire)
        with self._lock:
            was = self._weapon
            self._unclassed_run = self._unclassed_run + 1 if cls is None else 0
            if (cls is None and was is not None and was.cls is not None
                    and self._unclassed_run < 2):
                logger.debug("Weapon name: %r is in no class; keeping %s for one "
                             "more read", text, was.cls)
                return was
            self._weapon = weapon
            self._ts = self._clock()
        if cls is None:
            key = _compact(text)
            if key not in self._unknown_logged:
                self._unknown_logged.add(key)
                logger.info("Weapon selected: %r is in no weapon class, so `f` is "
                            "tapped (add a word for it to weapon.classes)", text)
        elif was is None or (was.cls, was.fire) != (cls, fire):
            logger.info("Weapon selected: %r -> %s (%s)", text, cls, fire)
        return weapon

    def selected(self) -> "SelectedWeapon | None":
        """The selected weapon while its reading is fresh; None otherwise."""
        with self._lock:
            if self._weapon is None or self._clock() - self._ts > self._stale_after_s:
                return None
            return self._weapon

"""Warns when a weapon-panel read has produced nothing usable for a stretch of battle.

On 2026-11-18 a game update redrew the weapon panel: the selected weapon's count
moved from the right of the panel to the left, and the panel's name and flare rows
moved up about 20 px. The crops in config.yaml stayed where they were, so the
missile count read no digits for 15 hours and the weapon name read 'ee'. With no
count of 0, the primary-empty switch never pressed `g`; with no weapon class, the
rocket pod was tapped at 5.6 km instead of held on its lead diamond. Nothing in
the log said so above DEBUG. This watch says so at WARNING, once per silent
stretch, so the next redraw shows in the first round (FR-012).

Only battle time counts: a gap of more than `max_gap_s` between two reads (a
lobby, a match load) does not, so the clock measures reads that should have
worked. A read is usable when the reader returned what the panel shows:
- ammo counts: any digits;
- the weapon name: a name in a weapon class, or the same name twice in a row (a
  weapon not yet in weapon.classes reads the same way each time; misreads of a
  clipped label, 'ee' then 'ae', do not).
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)


class PanelReadWatch:
    """Battle seconds since each label's last usable read, and one warning per
    silent stretch of `silent_s`. 0 turns the watch off."""

    def __init__(self, silent_s: float, max_gap_s: float = 5.0, clock=time.monotonic):
        self._silent_s = float(silent_s)
        self._max_gap_s = float(max_gap_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._silent: "dict[str, float]" = {}
        self._last_note: "dict[str, float]" = {}
        self._warned: "set[str]" = set()
        self._last_text: "dict[str, str]" = {}

    def note(self, label: str, usable: bool, shown: object = None) -> bool:
        """Record one read of `label`. Returns True when this read raised the
        warning. `shown` is what the reader saw, quoted in the warning.

        @relation(FR-012, scope=function)
        """
        if self._silent_s <= 0:
            return False
        now = self._clock()
        with self._lock:
            prev = self._last_note.get(label)
            self._last_note[label] = now
            if usable:
                if label in self._warned:
                    logger.info("Weapon panel: %s reads again after %.0f s of battle "
                                "without a usable read", label, self._silent.get(label, 0.0))
                    self._warned.discard(label)
                self._silent[label] = 0.0
                return False
            if prev is not None and now - prev <= self._max_gap_s:
                self._silent[label] = self._silent.get(label, 0.0) + (now - prev)
            silent = self._silent.get(label, 0.0)
            if silent < self._silent_s or label in self._warned:
                return False
            self._warned.add(label)
        logger.warning("Weapon panel: %s has had no usable read for %.0f s of battle "
                       "(last read %r) — the crop is likely off the HUD; recalibrate "
                       "it (make calibrate-crop) (FR-012)", label, silent, shown)
        return True

    def note_count(self, label: str, value: "int | None") -> bool:
        """A count read (AMMO_MISSILE, AMMO_FLARES): usable when it has digits."""
        return self.note(label, value is not None, value)

    def note_weapon_name(self, text: str, cls: "str | None") -> bool:
        """A weapon-name read: usable when it is in a class or repeats the last
        read exactly."""
        key = "".join(c for c in (text or "").upper() if c.isalnum())
        with self._lock:
            repeat = bool(key) and self._last_text.get("WEAPON_NAME") == key
            self._last_text["WEAPON_NAME"] = key
        return self.note("WEAPON_NAME", cls is not None or repeat, text)

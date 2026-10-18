"""Code Review 019 cycle 25 (operator, 2026-10-18: "yes"): two terrain warnings
that agree start the emergency climb.

Three shadow warnings watch for terrain, each from its own measurement: the
clear-path planner's ESCAPE (the line to the goal runs into terrain closing
fast), LOOM (the view expanding round the flight path) and SHAPE (a shape in
the middle of the screen growing). Over 130 missions and 145 crashes the kill
card confirmed on 2026-10-17, two of them firing within 1 s of each other came
1 to 6 s before 34% of the crashes (49 of 145), about twice a mission, and 16%
of those alarms were followed by a crash within 6 s; most of the crashes the
recovery failed had its climb start under 2 s before the impact. This turns
that agreement into a hard emergency, held for `hold_s` so the tree, which
looks once a tick, sees it.

The events are the moments the warnings switch on (LOOM, SHAPE) and each ESCAPE
plan, exactly what the scoring counted. They arrive from two threads: the main
loop (LOOM, SHAPE) and the pursuit's path worker (ESCAPE).
"""

from __future__ import annotations

import logging
import threading
import time

logger = logging.getLogger(__name__)

KINDS = ("ESCAPE", "LOOM", "SHAPE")


class WarningAgreement:
    """The latest time each warning fired, and whether two of them agree.

    `note(kind)` records one event; `agreeing()` is True for `hold_s` after the
    latest moment two different kinds fired within `window_s` of each other.
    """

    def __init__(self, window_s: float = 1.0, hold_s: float = 3.0, clock=time.time,
                 shadow: bool = False) -> None:
        self.window_s = float(window_s)
        # In shadow the agreements are logged and nothing flies on them: the
        # climb condition does not force a climb and the controller does not
        # set aside its objective hold-off (live 2026-10-18 07:13:40, cycle 26).
        self.shadow = bool(shadow)
        self.hold_s = float(hold_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._last: "dict[str, float]" = {}
        self._agreed_at: "float | None" = None
        self.agreements = 0

    def note(self, kind: str, ts: "float | None" = None) -> bool:
        """Record that `kind` fired at `ts` (now by default); True when this
        event makes two kinds agree."""
        if kind not in KINDS:
            raise ValueError(f"unknown warning {kind!r}")
        ts = self._clock() if ts is None else float(ts)
        if not self._lock.acquire(timeout=0.5):
            return False
        try:
            self._last[kind] = ts
            partners = sorted(k for k, t in self._last.items()
                              if k != kind and abs(ts - t) <= self.window_s)
            if not partners:
                return False
            fresh = self._agreed_at is None or ts - self._agreed_at > self.hold_s
            self._agreed_at = ts
        finally:
            self._lock.release()
        if fresh:
            self.agreements += 1
            logger.warning("TERRAIN WARNINGS AGREE: %s and %s within %.1f s — %s "
                           "(Code Review 019 cycle 25)",
                           kind, " and ".join(partners), self.window_s,
                           "[SHADOW - not actuating]" if self.shadow
                           else "the emergency climb starts")
        return True

    def live(self, now: "float | None" = None) -> bool:
        """True while agreeing and not in shadow: what the controller flies on."""
        return not self.shadow and self.agreeing(now)

    def agreeing(self, now: "float | None" = None) -> bool:
        """True while the latest agreement is under `hold_s` old."""
        now = self._clock() if now is None else float(now)
        agreed = self._agreed_at
        return agreed is not None and 0.0 <= now - agreed <= self.hold_s

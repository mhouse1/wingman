"""Liveness guard — bound a session that has stopped making progress (ADR 093).

On 2026-08-24 wingman ran 3h27m and was functionally dead for 110 minutes of
it: zero OCR, zero control actions, zero FSM transitions, while still logging
~4,000 lines per 15 minutes. A PROFILE overlay had opened over the lobby, and
none of the three recovery paths could act on it — the popup scan had nothing
calibrated, the exit dialog was not present, and ESC was suppressed by the
blackout itself.

ADR 093 fixes those specific paths. This guard exists because the next one will
be a different unrecognised screen, and the deeper defect is not any single
overlay: **wingman could not tell it was doing nothing.**

Same shape as ADR 090's memory guard, and for the same reason — bound the
symptom generically rather than enumerating causes:

    soft limit -> log loudly, let recovery try harder
    hard limit -> end the session at a safe point

Progress means an FSM state change **or** OCR activity. Both, deliberately: a
long battle produces few state changes but plenty of OCR, and a quiet lobby
produces little OCR but transitions normally. Only losing both at once is a
stall, which is exactly what the livelock looked like.

A finished session with an honest summary is far more useful than a process
that logs for eight hours and flies for one.
"""

import logging
import time

logger = logging.getLogger(__name__)


class LivenessGuard:
    """Self-throttled progress watchdog. Call `note_progress` and `check`."""

    def __init__(self, cfg: "dict | None" = None, clock=time.time):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._soft_s = float(cfg.get("stall_limit_s", 300.0))
        self._hard_s = float(cfg.get("hard_limit_s", 900.0))
        self._clock = clock
        self._last_progress = clock()
        self._soft_fired = False
        self._hard = False
        self._reason = ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    def note_progress(self, source: str = "") -> None:
        """Record that the loop achieved something this tick."""
        self._last_progress = self._clock()
        if self._soft_fired:
            logger.info("Liveness guard: progress resumed via %s — clearing stall",
                        source or "activity")
        self._soft_fired = False

    def stalled_for(self, now: "float | None" = None) -> float:
        return max(0.0, (self._clock() if now is None else now) - self._last_progress)

    def check(self, now: "float | None" = None) -> bool:
        """Return True once the soft limit is crossed (recovery should escalate).

        Never raises: a watchdog that can take down the loop is worse than the
        stall it watches for.
        """
        if not self._enabled:
            return False
        try:
            stalled = self.stalled_for(now)
            if stalled >= self._hard_s and not self._hard:
                self._hard = True
                self._reason = (f"no progress for {stalled:.0f}s "
                                f"(hard limit {self._hard_s:.0f}s)")
                logger.error(
                    "LIVENESS GUARD hard limit: %s — ending the session "
                    "(ADR 093). A finished session beats a hung one.", self._reason)
                return True
            if stalled >= self._soft_s and not self._soft_fired:
                self._soft_fired = True
                logger.error(
                    "LIVENESS GUARD: no FSM state change and no OCR for %.0fs "
                    "— wingman is not making progress (ADR 093)", stalled)
            return self._soft_fired
        except Exception as e:
            logger.warning("Liveness guard: check failed: %s", e)
            return False

    def should_stop(self) -> bool:
        """True once the hard limit has fired."""
        return self._hard

    @property
    def reason(self) -> str:
        return self._reason

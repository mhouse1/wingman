"""Anomaly 003 — detect a session stuck in GAME_BATTLE_EJECT with no
resolution, and end it for review.

Diagnostic-only: the caller must gate this on --record-session (Design 012)
being active — see docs/anomaly/003-eject-no-telemetry-false-death-leaves-
aircraft-unflown.md. Not a general safety mechanism; no live-trial has
validated it as one, and its 40s default threshold is calibrated from a
single incident, not a corpus of healthy ejects — see that doc's "Signal —
revised twice" and the 2026-09-13 live-test addendum before trusting the
default uncalibrated.

Three signal choices, in order, each rejected or corrected by evidence —
also documented in the anomaly record:

1. Dwell + no-observed-death + sustained climb — rejected: the
   no-observed-death check was blind at the exact moment that mattered.
2. Telemetry-freshness gap duration alone — rejected: would not have fired
   on the incident it was built from (gap closed at ~12s, under the
   proposed 13s threshold), and goes quiet the moment telemetry recovers,
   but the real harm ran 63.3s, almost entirely AFTER recovery.
3. Raw GAME_BATTLE_EJECT dwell (no gate on descent-control state) —
   **live-tested 2026-09-13 and false-positived within the first session**:
   fired on a legitimately long, still-actively-diving eject (health
   critical, visibly banking hard in the recorded video, ttg=10s, altitude
   falling continuously and smoothly the whole time — nothing stuck about
   it). Corrected: the clock must only start once `_eject_descent_control`
   has already given up (`Controller.eject_descent_active()` is False) —
   that is the actual moment nothing is flying the aircraft, not entry
   into GAME_BATTLE_EJECT itself, which also covers ordinary, healthy,
   still-in-progress dives of any length.
"""

import logging
import time

from .analyzer import GameState

logger = logging.getLogger(__name__)


class EjectStuckDetector:
    """Self-throttled, single-shot. Call `check(game_state,
    eject_descent_active)` once per tick."""

    def __init__(self, cfg: "dict | None" = None, clock=time.time):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._stuck_after_s = float(cfg.get("eject_stuck_after_s", 40.0))
        self._clock = clock
        self._stuck_since = None      # wall-clock time descent control gave up
        self._fired = False

    def check(self, game_state, eject_descent_active: bool) -> bool:
        """Return True once GAME_BATTLE_EJECT has persisted, WITH descent
        control already given up, past the threshold. Never fires while
        eject_descent_active is True, however long that legitimately takes.
        Fires at most once per instance."""
        if not self._enabled or self._fired:
            return False
        if game_state != GameState.GAME_BATTLE_EJECT or eject_descent_active:
            self._stuck_since = None
            return False
        if self._stuck_since is None:
            self._stuck_since = self._clock()
            return False
        dwell = self._clock() - self._stuck_since
        if dwell >= self._stuck_after_s:
            self._fired = True
            logger.error(
                "ANOMALY 003 DETECTED: GAME_BATTLE_EJECT with descent "
                "control already given up for %.0fs (threshold %.0fs) — "
                "ending session for review (docs/anomaly/003-eject-no-"
                "telemetry-false-death-leaves-aircraft-unflown.md)",
                dwell, self._stuck_after_s)
            return True
        return False

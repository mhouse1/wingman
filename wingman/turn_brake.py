"""The turn brake: in a share of rounds, the chase holds the airbrake while the
aim point (the lead ring, or the marker) is far off the crosshair at gun range,
so the jet turns tighter onto it; cruise's afterburner comes back the moment it
lets go.

Operator, 2026-11-21: "improve the machine gun accuracy, use throttle up and
down to increase turn rate".

Measured on logs/wingman_20261121_134757.log (08:47 to 13:45, 50 rounds):
- 404 gun bursts, median 0.6 s; 1 of 111 bursts without a missile gained damage;
  the ring was within 60 px of the crosshair on 193 of 1,201 pictures under 1.2 km.
- After a steering hold the model ended (predicted error about 0.03), the next
  press 0.35 s later found the error at 0.86 (pitch, 720) and 0.90 (roll, 1,105)
  of the error at the release: the jet is not closing the angle.
- Of 1,782 pictures with the target under 1.5 km, 28% had it 0.25 half-frame or
  more off the crosshair; the median speed was 704 KPH.

That a slower jet turns tighter here is the operator's (assumed, not measured);
the trial's measure says whether it does: by arm, the share of pictures under
`max_km` with the aim point within `on_target_err`, and how long the aim took to
come in from `on_err` to `off_err`.

No I/O and no clock: every time is passed in.
"""

from __future__ import annotations

import random


class TurnBrake:
    """The round's arm, the brake decision and the trial's measure."""

    def __init__(self, share: float, on_err: float, off_err: float, max_km: float,
                 max_hold_s: float, cooldown_s: float, on_target_err: float = 0.1,
                 rng: "random.Random | None" = None) -> None:
        if not 0.0 <= share <= 1.0:
            raise ValueError(f"turn_brake share {share!r} is outside 0 to 1")
        if off_err > on_err:
            raise ValueError("turn_brake off_err must not exceed on_err")
        self.share = float(share)
        self.on_err = float(on_err)
        self.off_err = float(off_err)
        self.max_km = float(max_km)
        self.max_hold_s = float(max_hold_s)
        self.cooldown_s = float(cooldown_s)
        self.on_target_err = float(on_target_err)
        self._rng = rng if rng is not None else random.Random()
        self.arm: "str | None" = None
        self.rounds = 0
        self._hold_since: "float | None" = None
        self._no_hold_before = 0.0
        self.why_off = ""
        # The measure, by arm.
        self._catch_since: "float | None" = None
        self.catches: "dict[str, list[float]]" = {}         # arm -> seconds
        self.pictures: "dict[str, list[int]]" = {}          # arm -> [near, on target]
        self._last_ts: "float | None" = None

    @property
    def enabled(self) -> bool:
        return self.share > 0.0

    @property
    def holding(self) -> bool:
        return self._hold_since is not None

    def new_round(self) -> "str | None":
        """Draw this round's arm, or None while the trial is off."""
        self._hold_since = None
        self._catch_since = None
        self._no_hold_before = 0.0
        if not self.enabled:
            self.arm = None
            return None
        self.arm = "brake" if self._rng.random() < self.share else "control"
        self.rounds += 1
        return self.arm

    def update(self, err: "float | None", err_y: "float | None", km: "float | None",
               now: float, picture_ts: "float | None" = None) -> bool:
        """One pursuit cycle with the aim point's error (half-frame units) and
        the tracked target's fresh range (None without one). Tallies the
        measure in both arms; returns True while a `brake` round should hold
        the airbrake."""
        near = err is not None and err_y is not None and km is not None and km < self.max_km
        miss = max(abs(err), abs(err_y)) if near else None
        self._tally(miss, now, picture_ts)
        if self.arm != "brake":
            return self._end("not a brake round")
        if miss is None:
            return self._end("no target near" if err is not None else "no target")
        if self._hold_since is None:
            if miss < self.on_err or now < self._no_hold_before:
                return False
            self._hold_since = now
            self.why_off = ""
            return True
        if miss < self.off_err:
            return self._end("aim within %.2f" % miss)
        if now - self._hold_since >= self.max_hold_s:
            self._no_hold_before = now + self.cooldown_s
            return self._end("held %.1f s" % (now - self._hold_since))
        return True

    def stop(self, why: str) -> None:
        """The chase cannot brake now: a hold in hand is over."""
        self._end(why)

    def _end(self, why: str) -> bool:
        if self._hold_since is not None:
            self._hold_since = None
            self.why_off = why
        return False

    def _tally(self, miss: "float | None", now: float, picture_ts: "float | None") -> None:
        arm = self.arm or "off"
        if miss is None:
            self._catch_since = None
            return
        if picture_ts is None or picture_ts != self._last_ts:
            self._last_ts = picture_ts
            row = self.pictures.setdefault(arm, [0, 0])
            row[0] += 1
            row[1] += miss <= self.on_target_err
        if self._catch_since is None:
            if miss >= self.on_err:
                self._catch_since = now
        elif miss < self.off_err:
            self.catches.setdefault(arm, []).append(now - self._catch_since)
            self._catch_since = None

    def summary(self) -> str:
        parts = []
        for arm in sorted(set(self.pictures) | set(self.catches)):
            near, on = self.pictures.get(arm, [0, 0])
            secs = sorted(self.catches.get(arm, []))
            parts.append("%s arm %d of %d pictures on target (%.0f%%), %d catches%s" % (
                arm, on, near, 100.0 * on / near if near else 0.0, len(secs),
                ", median %.1f s" % secs[len(secs) // 2] if secs else ""))
        return "; ".join(parts) or "no pictures near"

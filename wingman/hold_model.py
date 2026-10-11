"""How long a steering key has to stay down (HLDD 005, Sustained-Hold Actuation).

The picture answers a flight key late. Measured on four sessions of 2026-10-11
(the travel of the steered point between two pictures, against the seconds of
key held between them): the two agree best when the key time is taken 0.20 to
0.30 s earlier than the pictures, on both axes, and the point then moves 0.6 to
1.15 half-frames a second in pitch and 0.2 to 0.4 in roll (r 0.91 and 0.84 on a
control point, which does not move by itself).

A hold that ends when the picture shows the point at the centre therefore ends
that much too late: the nose keeps going for the lag, the opposite key is
pressed, and it comes back the same way. In those sessions 70 to 80 percent of
pitch presses and 62 to 78 percent of roll presses were the opposite of the one
before.

`HoldModel` keeps one axis's recent key history and says how far the keys have
already sent the nose beyond what a picture shows, and so how much longer the
key is needed. It also keeps the target's own motion across the screen, read
from what the pictures show beyond the keys' doing: without it the hold trails
a crossing target and never reaches it (simulated at 0.4 half-frames a second:
0 percent of ticks inside the deadband, against 62 percent with it).
`PictureClock` says when the picture in hand was first grabbed:
with a steering key held, so the nose certainly moving, 23 to 26 percent of
consecutive steering grabs in those sessions showed the picture of the grab
before, unchanged for 0.17 s at the median and 0.34 to 0.44 s at the 90th
percentile.

No I/O and no clock of its own: every time is passed in, from one monotonic
clock.
"""

import math
from typing import NamedTuple

import numpy as np


class HoldPlan(NamedTuple):
    """`sign` +1 or -1 is the key to hold, for `seconds`; 0 is no key.
    `predicted` is the error expected when a key pressed now starts to act."""

    sign: int
    seconds: float
    predicted: float


def _sign(value: float) -> int:
    return (value > 0) - (value < 0)


class HoldModel:
    """One steering axis: the error falls `rate_per_s` for each second of key,
    `lag_s` after the key.

    +1 is the key that lowers a positive error (ROLL_RIGHT, NOSE_DOWN), -1 the
    other, 0 neither. `key` is called from whichever thread presses or lets go;
    it only appends, or swaps in a new list, so readers need no lock.
    """

    # The target's own motion is averaged over about this long.
    DRIFT_TAU_S = 0.4
    # Two pictures further apart than this are not one motion (the target was
    # lost in between), nor are two closer than _MIN_GAP_S (pixel noise).
    DRIFT_MAX_GAP_S = 0.6
    _MIN_GAP_S = 0.02
    # Faster than this across the screen is a different target, not a moving one.
    DRIFT_MAX_PER_S = 1.5
    # The most the target's motion may add to the error, in half-frames.
    LEAD_MAX = 0.2

    def __init__(self, rate_per_s: float, lag_s: float, memory_s: float = 3.0) -> None:
        self.rate_per_s = float(rate_per_s)
        self.lag_s = max(0.0, float(lag_s))
        # How far back a picture may date and still be believed: an unchanged
        # picture older than this is taken as that old, no older.
        self.memory_s = float(memory_s)
        self._events: "list[tuple[float, int]]" = []
        # The target's own motion: error per second with no key held. A rate
        # set too high or too low for the airframe shows up here too, as motion
        # with the key or against it, and is led the same way.
        self.drift_per_s = 0.0
        self._sample: "tuple[float, float] | None" = None   # last new picture

    def key(self, sign: int, now: float) -> None:
        """The axis's key state from `now`."""
        events = self._events
        horizon = now - self.memory_s - self.lag_s
        if len(events) > 1 and events[1][0] < horizon:
            # The newest entry before the horizon is the state at the horizon.
            keep = max(i for i, e in enumerate(events) if e[0] < horizon)
            events = events[keep:]
        self._events = events + [(now, _sign(sign))]

    def key_time(self, start: float, end: float) -> float:
        """Signed seconds of key held between `start` and `end`."""
        total = 0.0
        state = 0
        at = start
        for when, sign in list(self._events):
            if when >= end:
                break
            if when > at:
                total += state * (when - at)
                at = when
            state = sign
        return total + state * max(0.0, end - at)

    def pending(self, seen_at: float, now: float) -> float:
        """How far the keys have sent the error down that a picture first
        grabbed at `seen_at` does not show yet."""
        return self.rate_per_s * self.key_time(seen_at - self.lag_s, now)

    def _observe(self, error: float, seen_at: float) -> None:
        """Update the target's own motion from a picture not seen before."""
        prev = self._sample
        if prev is not None and seen_at <= prev[0]:
            return                              # the same picture again
        self._sample = (seen_at, error)
        if prev is None:
            return
        gap = seen_at - prev[0]
        if not self._MIN_GAP_S <= gap <= self.DRIFT_MAX_GAP_S:
            self.drift_per_s = 0.0
            return
        # What the error did beyond what the keys account for.
        moved = error - prev[1] + self.rate_per_s * self.key_time(
            prev[0] - self.lag_s, seen_at - self.lag_s)
        if abs(moved) / gap > self.DRIFT_MAX_PER_S:
            self.drift_per_s = 0.0
            return
        weight = 1.0 - math.exp(-gap / self.DRIFT_TAU_S)
        self.drift_per_s += weight * (moved / gap - self.drift_per_s)

    def plan(self, error: float, seen_at: float, now: float, *,
             deadband: float, min_pulse_s: float = 0.0) -> HoldPlan:
        """What to hold for `error`, read from a picture first grabbed at `seen_at`.

        `predicted` is the error when a key pressed now starts to act: the
        error shown, plus the target's own motion until then, less the travel
        already commanded. No key when that is inside the deadband. With the
        error shown outside the deadband a key is only ever held toward it;
        inside, the target's motion may ask for a key before the picture does.
        """
        seen_at = min(now, max(seen_at, now - self.memory_s))
        self._observe(error, seen_at)
        lead = self.drift_per_s * (now - seen_at + self.lag_s)
        lead = max(-self.LEAD_MAX, min(self.LEAD_MAX, lead))
        predicted = error + lead - self.pending(seen_at, now)
        if (abs(predicted) <= deadband or self.rate_per_s <= 0
                or (abs(error) > deadband and _sign(predicted) != _sign(error))):
            return HoldPlan(0, 0.0, predicted)
        # A target moving away is closed on more slowly than one standing still.
        closing = max(self.rate_per_s - _sign(predicted) * self.drift_per_s,
                      0.25 * self.rate_per_s)
        return HoldPlan(_sign(predicted),
                        max(abs(predicted) / closing, min_pulse_s), predicted)


class PictureClock:
    """When the picture in hand was first grabbed.

    Two grabs are the same picture when every `step`-th pixel of every
    `step`-th row matches: 2,280 pixels of a 1920 x 1200 frame at the default.
    """

    def __init__(self, step: int = 32) -> None:
        self._step = max(1, int(step))
        self._probe = None
        self._seen_at = 0.0

    def seen(self, frame, now: float) -> float:
        if not isinstance(frame, np.ndarray):
            return now      # no picture to compare: taken as new
        probe = np.ascontiguousarray(frame[::self._step, ::self._step])
        if (self._probe is not None and probe.shape == self._probe.shape
                and np.array_equal(probe, self._probe)):
            return self._seen_at
        self._probe = probe
        self._seen_at = now
        return now

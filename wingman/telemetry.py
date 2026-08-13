"""Pure telemetry signal processing for ADR 038.

Filter, smoothing, rate derivation, and pitch-band estimation for the
altitude/speed signals read from the combined ALTITUDE_SPEED crop. Follows the
crop_region.py precedent: no internal imports, no threads, no OCR — every rule
is unit-testable with plain numbers. GameStateAnalyzer owns an instance and
serializes access with its telemetry lock; this module itself is not
thread-safe.

The plausibility filter adopts the ADR 030 pattern (cheap reader plus a
rejection gate in front of decision logic) with bounds derived from physics
rather than a rolling ceiling:

- speed is filtered first against an acceleration envelope, so one bogus
  speed read cannot inflate the altitude gate on the same tick
- the altitude gate bounds climb/descent by the last accepted speed (vertical
  speed can never exceed total speed), falling back to max_speed_mph when the
  speed signal is stale
- rejected readings keep the last accepted value; after
  reseed_after_rejections consecutive rejections the signal re-seeds, so one
  bogus seed cannot lock out all subsequent real values
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

MPH_TO_FPS = 5280.0 / 3600.0
# The HUD telemetry block is actually metric (speed "KPH", altitude "m") —
# see pitch_angle_deg(). The mph/ft naming across this module and its config
# keys predates that discovery; the filter envelopes are tuned in raw display
# units, so only ratio-based consumers care about the distinction.
KPH_TO_MPS = 1000.0 / 3600.0

# Pitch bands returned by pitch_band(). Sine is symmetric about vertical, so a
# shallow band cannot distinguish under-rotation from over-rotation past
# vertical — callers must correct with measure-correct-measure, not blind
# re-issue (ADR 038).
BAND_STEEP_DIVE = "steep_dive"
BAND_DIVE = "dive"
BAND_LEVEL = "level"
BAND_CLIMB = "climb"
BAND_STEEP_CLIMB = "steep_climb"

TREND_RISING = "rising"
TREND_FALLING = "falling"
TREND_FLAT = "flat"
TREND_UNKNOWN = "unknown"


@dataclass(frozen=True)
class TelemetrySignal:
    """Immutable per-signal state: last accepted reading plus derived values."""

    value: int | None = None          # last accepted raw reading
    ts: float | None = None           # when it was accepted (caller clock)
    stable_value: float | None = None # mean of the last smoothing_window accepted readings
    rate: float | None = None         # units/s between the last two accepted readings
    trend: str = TREND_UNKNOWN
    rejected_streak: int = 0          # consecutive rejections since last accept

    def age_s(self, now_s: float) -> float | None:
        if self.ts is None:
            return None
        return max(0.0, now_s - self.ts)

    def is_fresh(self, now_s: float, stale_after_s: float) -> bool:
        age = self.age_s(now_s)
        return age is not None and age <= stale_after_s


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Atomic pairing of both signals from the same processor state.

    Consumers must take speed and altitude from one snapshot — nose-direction
    estimation divides altitude rate by speed, so a torn pair from different
    cycles produces a wrong pitch estimate.
    """

    speed: TelemetrySignal = field(default_factory=TelemetrySignal)
    altitude: TelemetrySignal = field(default_factory=TelemetrySignal)
    taken_at_s: float = 0.0
    stale_after_s: float = 6.0

    def speed_fresh(self) -> bool:
        return self.speed.is_fresh(self.taken_at_s, self.stale_after_s)

    def altitude_fresh(self) -> bool:
        return self.altitude.is_fresh(self.taken_at_s, self.stale_after_s)

    def _ratio_speed(self) -> "float | None":
        """Speed for the flight-path ratio: the LAST ACCEPTED reading, not the
        smoothed mean (ADR 069 d6).

        ``stable_value`` averages the last smoothing_window accepted readings
        (~9 s). In a dive the aircraft accelerates faster than that window
        tracks, so the ratio alt_rate/speed inflates and saturates at 1.0 —
        reported as 90 degrees. Measured 2026-08-10 06:21:24: rate -110 m/s at
        469 KPH is -58 degrees, but the smoothed 313 KPH gave ratio -1.26 and
        a bogus -90. The smoothed value stays the right choice for the
        plausibility filter, whose job is noise immunity; it is the wrong
        denominator for a ratio whose denominator is changing fast.
        """
        return None if self.speed.value is None else float(self.speed.value)

    def pitch_band(
        self,
        *,
        steep_min_sin: float = 0.8,
        level_max_sin: float = 0.15,
    ) -> str | None:
        """Classify nose attitude, or None when either signal is missing/stale."""
        if not (self.speed_fresh() and self.altitude_fresh()):
            return None
        return pitch_band(
            self._ratio_speed(),
            self.altitude.rate,
            steep_min_sin=steep_min_sin,
            level_max_sin=level_max_sin,
        )

    def pitch_angle_deg(self) -> float | None:
        """Flight-path angle in degrees, or None when either signal is missing/stale."""
        if not (self.speed_fresh() and self.altitude_fresh()):
            return None
        return pitch_angle_deg(self._ratio_speed(), self.altitude.rate)


def pitch_band(
    speed_kph: float | None,
    alt_rate_mps: float | None,
    *,
    steep_min_sin: float = 0.8,
    level_max_sin: float = 0.15,
    min_speed_mps: float = 10.0,
) -> str | None:
    """Estimate the flight-path-angle band from altitude rate and speed.

    Altitude rate is approximately speed times the sine of the flight-path
    angle, so the ratio alt_rate / speed bounds the attitude without extra
    HUD parsing. Units are METRIC (HUD shows KPH and meters — ADR 067);
    speed converts at KPH_TO_MPS. Returns None when inputs are missing or
    speed is too small for the ratio to be meaningful.

    Physical caveats: this measures the velocity-vector angle, not nose
    attitude — they differ by angle of attack, most at low speed. And the
    displayed speed under-represents actual motion during hard maneuvers
    (ADR 067 replay: eject descents sustain ratios of 1.2 to 2.9), so past
    the clamp the ratio is an ordinal steepness signal, not a sine. The
    steep_min_sin default of 0.8 (approx 53 degrees) separated 83 percent
    of 495 archived eject windows from normal flight in the ADR 067 replay.
    """
    if speed_kph is None or alt_rate_mps is None:
        return None
    speed_mps = speed_kph * KPH_TO_MPS
    if speed_mps < min_speed_mps:
        return None
    ratio = alt_rate_mps / speed_mps
    ratio = max(-1.5, min(1.5, ratio))  # displayed speed can undershoot real motion
    if ratio <= -steep_min_sin:
        return BAND_STEEP_DIVE
    if ratio < -level_max_sin:
        return BAND_DIVE
    if ratio <= level_max_sin:
        return BAND_LEVEL
    if ratio < steep_min_sin:
        return BAND_CLIMB
    return BAND_STEEP_CLIMB


def pitch_angle_deg(
    speed_kph: float | None,
    alt_rate_mps: float | None,
    *,
    min_speed_mps: float = 10.0,
) -> float | None:
    """Flight-path angle in degrees from altitude rate and speed.

    The HUD units are METRIC — the telemetry block reads "NNNN KPH" over
    "NNNN m" (verified against the integration screenshots, e.g.
    P1_030_BATTLE_HUD_MISSILES_4.png: "1022 KPH / 554 m"), so the altitude
    rate is m/s and speed converts at 1/3.6, not the mph-to-fps factor the
    legacy naming elsewhere in this module assumes. Using MPH_TO_FPS here
    compresses the ratio by 3.6 x 1.4667 = 5.3x — the "systematically
    compressed by a units mismatch" case ADR 058 anticipated.

    Same physics and caveats as pitch_band(): angle = asin(alt_rate / speed)
    is the velocity-vector angle averaged over the OCR cadence, not
    instantaneous nose attitude, and it compresses near vertical. The ratio
    is clamped to plus/minus 1 before asin so a stalled or falling aircraft
    (vertical rate exceeding displayed forward speed) saturates at 90
    degrees instead of raising ValueError.
    """
    if speed_kph is None or alt_rate_mps is None:
        return None
    speed_mps = speed_kph * KPH_TO_MPS
    if speed_mps < min_speed_mps:
        return None
    ratio = max(-1.0, min(1.0, alt_rate_mps / speed_mps))
    return math.degrees(math.asin(ratio))


def pitch_band_from_angle_deg(
    angle_deg: float | None,
    *,
    steep_min_sin: float = 0.8,
    level_max_sin: float = 0.15,
) -> str | None:
    """Band label for a corrected flight-path angle.

    pitch_band() itself still computes its ratio with the legacy MPH_TO_FPS
    conversion that the eject thresholds were flight-tuned against, so it
    cannot be reused for display next to the corrected angle without the two
    disagreeing. This maps the corrected angle onto the same documented band
    boundaries (level_max_sin 0.15 approx 8.6 deg, steep_min_sin 0.8 approx
    53 deg).
    """
    if angle_deg is None:
        return None
    sin_angle = math.sin(math.radians(angle_deg))
    if sin_angle <= -steep_min_sin:
        return BAND_STEEP_DIVE
    if sin_angle < -level_max_sin:
        return BAND_DIVE
    if sin_angle <= level_max_sin:
        return BAND_LEVEL
    if sin_angle < steep_min_sin:
        return BAND_CLIMB
    return BAND_STEEP_CLIMB


class TelemetryProcessor:
    """Filter + smoothing + rate state for both telemetry signals.

    Feed accepted-or-not raw readings via update(); read one atomic
    TelemetrySnapshot via snapshot(). Not thread-safe — the caller serializes.
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        self.max_speed_mph = float(cfg.get("max_speed_mph", 2000.0))
        self.max_altitude_ft = float(cfg.get("max_altitude_ft", 60000.0))
        self.max_speed_change_mph_s = float(cfg.get("max_speed_change_mph_s", 300.0))
        self.plausibility_margin = float(cfg.get("plausibility_margin", 1.5))
        # Upper bound on the dt multiplier in the delta gates — the design tick
        # the envelopes were tuned against, not the (possibly throttled) real
        # sample interval. See _gate().
        self.max_gate_dt_s = float(cfg.get("max_gate_dt_s", 1.5))
        self.reseed_after_rejections = max(1, int(cfg.get("reseed_after_rejections", 3)))
        self.smoothing_window = max(1, int(cfg.get("smoothing_window", 3)))
        self.stale_after_s = float(cfg.get("stale_after_s", 6.0))
        self.trend_min_alt_rate_fps = float(cfg.get("trend_min_alt_rate_fps", 20.0))
        self.trend_min_speed_rate_mph_s = float(cfg.get("trend_min_speed_rate_mph_s", 15.0))
        self.steep_min_sin = float(cfg.get("steep_dive_min_sin", 0.5))
        self.level_max_sin = float(cfg.get("level_max_sin", 0.15))

        self._speed = TelemetrySignal()
        self._altitude = TelemetrySignal()
        # Accepted (ts, value) history, newest last, capped at smoothing_window.
        self._speed_hist: list[tuple[float, float]] = []
        self._alt_hist: list[tuple[float, float]] = []
        self.rejected_total = 0

    def reset(self) -> None:
        self._speed = TelemetrySignal()
        self._altitude = TelemetrySignal()
        self._speed_hist = []
        self._alt_hist = []

    def update(
        self,
        speed_raw: int | None,
        altitude_raw: int | None,
        now_s: float,
    ) -> None:
        """Apply the plausibility filter to one tick's raw OCR readings.

        Speed is filtered first: the altitude gate uses the last accepted
        speed, never the raw speed from the same tick (ADR 038 filter
        ordering).
        """
        if speed_raw is not None:
            self._speed = self._update_signal(
                signal=self._speed,
                hist=self._speed_hist,
                raw=float(speed_raw),
                now_s=now_s,
                absolute_max=self.max_speed_mph * self.plausibility_margin,
                max_delta_per_s=self.max_speed_change_mph_s * self.plausibility_margin,
                trend_min_rate=self.trend_min_speed_rate_mph_s,
                # The speed bound is an ACCELERATION envelope tuned at the 1.5s
                # design tick (ADR 038); clamping dt keeps a throttled sampler
                # from widening it (the 1114 -> 8 mph collapse this caught).
                gate_dt_cap_s=self.max_gate_dt_s,
            )
        if altitude_raw is not None:
            self._altitude = self._update_signal(
                signal=self._altitude,
                hist=self._alt_hist,
                raw=float(altitude_raw),
                now_s=now_s,
                absolute_max=self.max_altitude_ft,
                max_delta_per_s=self._altitude_bound_fps(now_s) * self.plausibility_margin,
                trend_min_rate=self.trend_min_alt_rate_fps,
                # The altitude bound is PHYSICS (vertical speed cannot exceed
                # total speed), so it scales correctly with the real gap and
                # must NOT be clamped below the actual sample cadence: with
                # ocr_every_n_ticks=2 (~3.0s gap), a 1.5s clamp shrank the
                # allowance to margin x clamp / gap = 0.75 x speed — rejecting
                # every dive steeper than sin 0.75 while the confirm band
                # starts at 0.8. Verified against the 2026-07-30 18:51 session:
                # this one mechanism produced 0 dive confirmations, froze
                # altitude.ts (starving the confirm dedup), and fed the eject
                # loop stale level bands that drove 50 blind nose-down
                # re-issues. Freshness is still bounded: the seed_usable check
                # below skips the gate entirely past stale_after_s.
                gate_dt_cap_s=self.stale_after_s,
            )

    def snapshot(self, now_s: float) -> TelemetrySnapshot:
        return TelemetrySnapshot(
            speed=self._speed,
            altitude=self._altitude,
            taken_at_s=now_s,
            stale_after_s=self.stale_after_s,
        )

    def _altitude_bound_fps(self, now_s: float) -> float:
        """Max plausible climb/descent rate: vertical speed cannot exceed total
        speed. Falls back to the aircraft envelope maximum when the speed
        signal is stale, so a dead speed read cannot freeze the altitude gate.
        """
        if self._speed.value is not None and self._speed.is_fresh(now_s, self.stale_after_s):
            bound_mph = max(float(self._speed.value), 60.0)  # floor: parked reads of 0 must not zero the gate
        else:
            bound_mph = self.max_speed_mph
        return bound_mph * MPH_TO_FPS

    def _update_signal(
        self,
        *,
        signal: TelemetrySignal,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        absolute_max: float,
        max_delta_per_s: float,
        trend_min_rate: float,
        gate_dt_cap_s: float,
    ) -> TelemetrySignal:
        if raw < 0.0 or raw > absolute_max:
            # Out-of-envelope readings are never seedable — a consistent
            # stream of absolute-bound garbage must not become the new seed.
            return self._reject(signal, hist, raw, now_s, seedable=False)

        # The delta gate only means anything while the seed is still a
        # trustworthy description of the aircraft. Past stale_after_s the seed
        # says nothing about the present, so gating against it would reject the
        # first good reading after any telemetry gap (respawn, OCR downtime,
        # a rejection streak) and force a slow reseed — during an eject that is
        # exactly when the dive confirmation needs the signal back.
        seed_age = None if signal.ts is None else (now_s - signal.ts)
        seed_usable = (
            signal.value is not None
            and seed_age is not None
            and seed_age <= self.stale_after_s
        )
        if seed_usable:
            dt = max(seed_age, 0.1)  # guard duplicate timestamps
            # Cap the dt multiplier per-gate (see the two call sites above):
            # acceleration-envelope gates must not widen when the sampler is
            # throttled; physics gates must not shrink below the real cadence.
            dt = min(dt, gate_dt_cap_s)
            if abs(raw - float(signal.value)) > max_delta_per_s * dt:
                return self._reject(signal, hist, raw, now_s, seedable=True)
        elif signal.value is not None:
            # Stale-seed bypass: the reading is accepted unconditionally, but
            # the rate history still holds PRE-GAP entries. Pairing the first
            # post-gap reading with one of those fabricates a rate spanning the
            # whole gap — a single bogus post-gap read (e.g. 3950 vs true 8900
            # after an 8s outage) manufactured a confirm-grade -625 ft/s
            # "steep dive" sample and then delta-blocked the true series for
            # ~9s. Clear the history so the post-gap reading seeds fresh with
            # rate=None; the next real sample restores the rate honestly.
            hist.clear()

        return self._accept(hist, raw, now_s, trend_min_rate)

    def _reject(
        self,
        signal: TelemetrySignal,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        *,
        seedable: bool,
    ) -> TelemetrySignal:
        self.rejected_total += 1
        streak = signal.rejected_streak + 1
        if streak >= self.reseed_after_rejections:
            # The seed itself is suspect — recalibrate. A delta-rejected
            # stream is usually the consistent real value blocked by a bogus
            # seed, so seed from it; out-of-envelope streams clear to empty
            # and the next in-bounds reading seeds instead.
            hist.clear()
            if seedable:
                return TelemetrySignal(
                    value=int(raw),
                    ts=now_s,
                    stable_value=raw,
                )
            return TelemetrySignal()
        # Keep the last accepted value; age keeps growing (ts unchanged).
        return TelemetrySignal(
            value=signal.value,
            ts=signal.ts,
            stable_value=signal.stable_value,
            rate=signal.rate,
            trend=signal.trend,
            rejected_streak=streak,
        )

    def _accept(
        self,
        hist: list[tuple[float, float]],
        raw: float,
        now_s: float,
        trend_min_rate: float,
    ) -> TelemetrySignal:
        hist.append((now_s, raw))
        del hist[:-self.smoothing_window]

        stable = sum(v for _, v in hist) / len(hist)

        # Rate from post-filter accepted readings with real timestamp deltas —
        # deliberately not from stable_value, whose smoothing lag is too slow
        # for dive verification (ADR 038 data model).
        rate = None
        if len(hist) >= 2:
            (t_prev, v_prev), (t_new, v_new) = hist[-2], hist[-1]
            dt = t_new - t_prev
            if dt > 0.0:
                rate = (v_new - v_prev) / dt

        if rate is None:
            trend = TREND_UNKNOWN
        elif rate > trend_min_rate:
            trend = TREND_RISING
        elif rate < -trend_min_rate:
            trend = TREND_FALLING
        else:
            trend = TREND_FLAT

        return TelemetrySignal(
            value=int(raw),
            ts=now_s,
            stable_value=stable,
            rate=rate,
            trend=trend,
        )

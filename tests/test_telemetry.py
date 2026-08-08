"""Unit tests for wingman/telemetry.py — ADR 038 signal model.

Pure-module tests: no OCR, no threads, plain numbers only.
"""

import pytest

from wingman.telemetry import (
    BAND_CLIMB,
    BAND_DIVE,
    BAND_LEVEL,
    BAND_STEEP_CLIMB,
    BAND_STEEP_DIVE,
    MPH_TO_FPS,
    TREND_FALLING,
    TREND_FLAT,
    TREND_RISING,
    TREND_UNKNOWN,
    TelemetryProcessor,
    TelemetrySnapshot,
    pitch_angle_deg,
    pitch_band,
    pitch_band_from_angle_deg,
)


def _proc(**overrides):
    cfg = {
        "max_speed_mph": 2000.0,
        "max_altitude_ft": 60000.0,
        "max_speed_change_mph_s": 300.0,
        "plausibility_margin": 1.5,
        "reseed_after_rejections": 3,
        "smoothing_window": 3,
        "stale_after_s": 6.0,
    }
    cfg.update(overrides)
    return TelemetryProcessor(cfg)


# ---------------------------------------------------------------------------
# Seeding and acceptance
# ---------------------------------------------------------------------------

class TestSeedingAndAcceptance:
    def test_first_reading_seeds_unconditionally_within_bounds(self):
        p = _proc()
        p.update(530, 27681, now_s=0.0)
        snap = p.snapshot(0.0)
        assert snap.speed.value == 530
        assert snap.altitude.value == 27681

    def test_gradual_change_accepted_and_rate_derived(self):
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        p.update(610, 19400, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == 19400
        assert snap.altitude.rate == pytest.approx(-400.0)  # 600 ft in 1.5 s
        assert snap.speed.rate == pytest.approx(10 / 1.5)

    def test_stable_value_is_windowed_mean(self):
        p = _proc(smoothing_window=3)
        for i, alt in enumerate([10000, 10100, 10200, 10300]):
            p.update(None, alt, now_s=float(i))
        snap = p.snapshot(3.0)
        assert snap.altitude.stable_value == pytest.approx((10100 + 10200 + 10300) / 3)


# ---------------------------------------------------------------------------
# Plausibility filter — the ADR 030 pattern with physics bounds
# ---------------------------------------------------------------------------

class TestPlausibilityFilter:
    def test_stray_digit_altitude_spike_rejected(self):
        # The ADR 038 example: 2768 read as 27681 in one tick.
        p = _proc()
        p.update(600, 2768, now_s=0.0)
        p.update(600, 27681, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == 2768          # last accepted kept
        assert snap.altitude.ts == 0.0              # age keeps growing
        assert snap.altitude.rejected_streak == 1

    def test_max_rate_dive_is_accepted(self):
        # Vertical dive at 600 MPH ≈ 880 ft/s — the very signal being measured
        # must pass (analogue of ADR 030 accepting health restores).
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        dive_delta = 880 * 1.5  # 1320 ft in one tick, within speed bound
        p.update(600, int(20000 - dive_delta), now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.altitude.value == int(20000 - dive_delta)
        assert snap.altitude.rejected_streak == 0

    def test_bogus_speed_does_not_inflate_altitude_gate_same_tick(self):
        # Filter ordering: altitude gate uses the last *accepted* speed, never
        # the raw speed from the same tick. 13550 MPH is rejected, so the
        # 25000 ft jump must also be rejected.
        p = _proc()
        p.update(1355, 2768, now_s=0.0)
        p.update(13550, 27681, now_s=1.5)
        snap = p.snapshot(1.5)
        assert snap.speed.value == 1355
        assert snap.altitude.value == 2768

    def test_stale_speed_falls_back_to_envelope_bound(self):
        # With no speed signal at all, the altitude gate uses max_speed_mph —
        # conservative but still tight enough to reject huge jumps.
        p = _proc(max_speed_mph=2000.0, plausibility_margin=1.0)
        p.update(None, 5000, now_s=0.0)
        # 2000 MPH ≈ 2933 ft/s → 4400 ft allowed per 1.5 s tick.
        p.update(None, 5000 + 4000, now_s=1.5)   # within envelope bound
        assert p.snapshot(1.5).altitude.value == 9000
        p.update(None, 40000, now_s=3.0)          # far beyond envelope bound
        assert p.snapshot(3.0).altitude.value == 9000

    def test_throttled_cadence_does_not_widen_the_speed_gate(self):
        """Slowing the sampler must not relax the filter.

        Every delta gate is a per-second rate times the real inter-sample gap,
        so adding ocr_every_n_ticks=2 (v1.6.27) halved telemetry cadence to
        ~3.0s and thereby doubled every allowance the bounds were sized for at
        the 1.5s design tick — letting a 1114 -> 8 mph collapse through on
        2026-07-30. dt is now clamped to max_gate_dt_s.
        """
        p = _proc(max_speed_change_mph_s=300.0, plausibility_margin=1.0,
                  max_gate_dt_s=1.5)
        p.update(500, None, now_s=0.0)
        # +800 over a 3.0s gap: inside 300*3.0=900 (what the unclamped gate
        # would allow) but outside the clamped 300*1.5=450.
        p.update(1300, None, now_s=3.0)
        assert p.snapshot(3.0).speed.value == 500, "throttled cadence widened the gate"

    def test_gate_is_skipped_once_the_seed_is_stale(self):
        """A seed older than stale_after_s says nothing about the present.

        Without this, clamping dt would make the first good reading after any
        telemetry gap (respawn, OCR downtime, a rejection streak) fail the gate
        and force a slow reseed — during an eject that is exactly when the dive
        confirmation needs the signal back.
        """
        p = _proc(max_speed_change_mph_s=300.0, plausibility_margin=1.0,
                  max_gate_dt_s=1.5, stale_after_s=6.0)
        p.update(500, None, now_s=0.0)
        # 30s later the aircraft is somewhere else entirely; the stale seed must
        # not veto the new reading.
        p.update(1300, None, now_s=30.0)
        assert p.snapshot(30.0).speed.value == 1300

    def test_steep_dive_is_accepted_at_throttled_cadence(self):
        """The ALTITUDE gate must not reject a real steep dive at the 3.0s cadence.

        The altitude bound is physics (vertical speed cannot exceed total
        speed), so it scales with the real inter-sample gap. Clamping its dt to
        max_gate_dt_s=1.5 against the ~3.0s ocr_every_n_ticks=2 cadence shrank
        the allowance to margin x 1.5/3.0 = 0.75 x speed — structurally
        rejecting every dive steeper than sin 0.75 while the confirm band
        starts at 0.8. Verified root cause of the 2026-07-30 18:51 session's
        0-of-26 dive confirmations.
        """
        p = _proc(max_gate_dt_s=1.5, plausibility_margin=1.5, stale_after_s=6.0)
        speed_fps = 500 * 5280 / 3600
        alt = 12000.0
        p.update(500, int(alt), now_s=0.0)
        accepted = 0
        for i in range(1, 6):
            alt -= 0.85 * speed_fps * 3.0     # steady sin-0.85 dive, 3.0s gaps
            p.update(500, int(alt), now_s=i * 3.0)
            if p.snapshot(i * 3.0).altitude.value == int(alt):
                accepted += 1
        assert accepted == 5, (
            f"altitude gate rejected {5 - accepted}/5 samples of a genuine "
            "sin-0.85 dive at the throttled cadence"
        )

    def test_stale_seed_bypass_does_not_fabricate_a_rate_across_the_gap(self):
        """The first post-gap reading must carry rate=None, not a cross-gap rate.

        Pairing a post-gap reading with a pre-gap history entry fabricates a
        rate spanning the whole outage: a single bogus read after an 8s gap
        (3950 vs true ~8900) manufactured a confirm-grade -625 ft/s "steep
        dive" sample and then delta-blocked the true series for ~9s.
        """
        p = _proc(stale_after_s=6.0)
        p.update(600, 8900, now_s=0.0)
        p.update(600, 8890, now_s=1.5)        # hist now has pre-gap entries
        # 8s outage, then a bogus reading arrives via the stale-seed bypass.
        p.update(600, 3950, now_s=9.5)
        snap = p.snapshot(9.5)
        assert snap.altitude.value == 3950     # bypass still accepts it...
        assert snap.altitude.rate is None      # ...but must not invent a rate
        # The very next TRUE reading must not be delta-blocked by the bogus
        # seed pairing (it is blocked by the bogus VALUE, which is correct
        # delta behaviour — but the rate recovers on the reseed path).
        p.update(600, 8870, now_s=12.5)

    def test_speed_acceleration_envelope(self):
        p = _proc(max_speed_change_mph_s=300.0, plausibility_margin=1.0)
        p.update(500, None, now_s=0.0)
        p.update(940, None, now_s=1.5)  # +440 in 1.5s < 450 allowed
        assert p.snapshot(1.5).speed.value == 940
        p.update(1500, None, now_s=3.0)  # +560 in 1.5s > 450 allowed
        assert p.snapshot(3.0).speed.value == 940


# ---------------------------------------------------------------------------
# Reseed behavior
# ---------------------------------------------------------------------------

class TestReseed:
    def test_bogus_seed_cannot_lock_out_real_values(self):
        # Seed with a bogus 27681; real readings around 2768 are rejected until
        # the reseed threshold clears the bad seed.
        p = _proc(reseed_after_rejections=3)
        p.update(None, 27681, now_s=0.0)
        p.update(None, 2768, now_s=1.5)
        p.update(None, 2770, now_s=3.0)
        p.update(None, 2772, now_s=4.5)  # third consecutive rejection → reseed
        snap = p.snapshot(4.5)
        assert snap.altitude.value == 2772
        assert snap.altitude.rejected_streak == 0

    def test_out_of_envelope_stream_never_becomes_seed(self):
        p = _proc(reseed_after_rejections=2, max_altitude_ft=60000.0)
        p.update(None, 5000, now_s=0.0)
        p.update(None, 99999, now_s=1.5)
        p.update(None, 99999, now_s=3.0)  # reseed fires, but not seedable
        snap = p.snapshot(3.0)
        assert snap.altitude.value is None
        p.update(None, 5100, now_s=4.5)   # next in-bounds reading seeds
        assert p.snapshot(4.5).altitude.value == 5100

    def test_accept_resets_rejected_streak(self):
        p = _proc(reseed_after_rejections=3)
        p.update(None, 5000, now_s=0.0)
        p.update(None, 50000, now_s=1.5)   # rejected
        p.update(None, 5050, now_s=3.0)    # accepted
        snap = p.snapshot(3.0)
        assert snap.altitude.value == 5050
        assert snap.altitude.rejected_streak == 0


# ---------------------------------------------------------------------------
# Trend and staleness
# ---------------------------------------------------------------------------

class TestTrendAndStaleness:
    def test_trend_classification(self):
        p = _proc(trend_min_alt_rate_fps=20.0)
        p.update(None, 10000, now_s=0.0)
        assert p.snapshot(0.0).altitude.trend == TREND_UNKNOWN
        p.update(None, 10005, now_s=1.5)
        assert p.snapshot(1.5).altitude.trend == TREND_FLAT
        p.update(None, 10500, now_s=3.0)
        assert p.snapshot(3.0).altitude.trend == TREND_RISING
        p.update(None, 9800, now_s=4.5)
        assert p.snapshot(4.5).altitude.trend == TREND_FALLING

    def test_staleness_from_snapshot_time(self):
        p = _proc(stale_after_s=6.0)
        p.update(600, 10000, now_s=0.0)
        assert p.snapshot(3.0).altitude_fresh()
        assert not p.snapshot(10.0).altitude_fresh()

    def test_rejection_keeps_age_growing(self):
        p = _proc()
        p.update(None, 10000, now_s=0.0)
        p.update(None, 50000, now_s=5.0)  # rejected — ts stays 0.0
        snap = p.snapshot(8.0)
        assert snap.altitude.age_s(8.0) == pytest.approx(8.0)
        assert not snap.altitude_fresh()


# ---------------------------------------------------------------------------
# Pitch bands — nose-direction estimation
# ---------------------------------------------------------------------------

class TestPitchBand:
    # Metric normalization (ADR 067): 600 KPH is 166.67 m/s, so sin(angle)
    # maps to alt rates of 25 m/s (level boundary, 0.15) and 133 m/s (steep
    # boundary, 0.8) at that speed.

    def test_level_flight(self):
        assert pitch_band(600.0, 0.0) == BAND_LEVEL

    def test_shallow_dive_20_degrees_at_600kph(self):
        # sin(20°) ≈ 0.342 → ~57 m/s descent at 600 KPH.
        assert pitch_band(600.0, -57.0) == BAND_DIVE

    def test_30_degree_dive_is_not_steep(self):
        # sin(30°) = 0.5 → ~83 m/s at 600 KPH. The flight-tested failure:
        # with steep_min_sin 0.5 this counted as steep and the eject settled
        # at a 30-degree dive; the 0.8 default keeps it in the dive band.
        assert pitch_band(600.0, -83.3) == BAND_DIVE

    def test_vertical_dive_at_600kph(self):
        # sin(90°) = 1 → ~167 m/s descent at 600 KPH.
        assert pitch_band(600.0, -167.0) == BAND_STEEP_DIVE

    def test_steep_climb(self):
        assert pitch_band(600.0, 140.0) == BAND_STEEP_CLIMB

    def test_shallow_climb(self):
        assert pitch_band(600.0, 57.0) == BAND_CLIMB

    def test_missing_inputs_return_none(self):
        assert pitch_band(None, -57.0) is None
        assert pitch_band(600.0, None) is None

    def test_too_slow_for_meaningful_ratio(self):
        assert pitch_band(10.0, -10.0) is None

    def test_snapshot_pitch_band_requires_both_signals_fresh(self):
        p = _proc(stale_after_s=6.0)
        p.update(600, 20000, now_s=0.0)
        p.update(600, 19775, now_s=1.5)  # -150 m/s at 600 KPH → ratio 0.9, steep dive
        assert p.snapshot(2.0).pitch_band() == BAND_STEEP_DIVE
        # Stale snapshot must return None — corrections need contrary
        # evidence, never absence of data (ADR 038).
        assert p.snapshot(20.0).pitch_band() is None


class TestPitchAngleDeg:
    # The HUD is metric (KPH / meters) — see pitch_angle_deg(). 600 KPH is
    # 166.67 m/s, so a 30-degree flight path is an 83.3 m/s altitude rate.

    def test_level_flight_is_zero(self):
        assert pitch_angle_deg(600.0, 0.0) == pytest.approx(0.0)

    def test_30_degree_dive(self):
        # sin(30°) = 0.5 → -83.3 m/s at 600 KPH (166.67 m/s).
        assert pitch_angle_deg(600.0, -83.33) == pytest.approx(-30.0, abs=0.1)

    def test_30_degree_climb(self):
        assert pitch_angle_deg(600.0, 83.33) == pytest.approx(30.0, abs=0.1)

    def test_uses_metric_conversion_not_mph(self):
        # ADR 058 flight data: -389 m/s at 1782 KPH (495 m/s) was a hard dive.
        # The legacy mph-as-displayed conversion read this as -8.6°; the metric
        # conversion must place it in the low 50s.
        assert pitch_angle_deg(1782.0, -389.0) == pytest.approx(-51.8, abs=0.5)

    def test_ratio_past_vertical_saturates_at_90(self):
        # A stalled/falling aircraft can descend faster than its displayed
        # forward speed; asin must saturate, not raise.
        assert pitch_angle_deg(600.0, -400.0) == pytest.approx(-90.0)

    def test_missing_inputs_return_none(self):
        assert pitch_angle_deg(None, -80.0) is None
        assert pitch_angle_deg(600.0, None) is None

    def test_too_slow_for_meaningful_ratio(self):
        assert pitch_angle_deg(10.0, -10.0) is None

    def test_snapshot_pitch_angle_requires_both_signals_fresh(self):
        p = _proc(stale_after_s=6.0)
        p.update(600, 20000, now_s=0.0)
        p.update(600, 19875, now_s=1.5)  # -83.3 m/s at 600 KPH → -30°
        assert p.snapshot(2.0).pitch_angle_deg() == pytest.approx(-30.0, abs=0.1)
        assert p.snapshot(20.0).pitch_angle_deg() is None


class TestPitchBandFromAngleDeg:
    def test_level_band(self):
        assert pitch_band_from_angle_deg(0.0) == BAND_LEVEL
        assert pitch_band_from_angle_deg(8.0) == BAND_LEVEL

    def test_dive_and_climb_bands(self):
        assert pitch_band_from_angle_deg(-30.0) == BAND_DIVE
        assert pitch_band_from_angle_deg(30.0) == BAND_CLIMB

    def test_steep_bands(self):
        # steep_min_sin 0.8 ≈ 53.1°
        assert pitch_band_from_angle_deg(-60.0) == BAND_STEEP_DIVE
        assert pitch_band_from_angle_deg(60.0) == BAND_STEEP_CLIMB

    def test_none_angle_returns_none(self):
        assert pitch_band_from_angle_deg(None) is None


# ---------------------------------------------------------------------------
# Snapshot atomicity
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_is_immutable(self):
        snap = TelemetrySnapshot()
        with pytest.raises(Exception):
            snap.taken_at_s = 99.0

    def test_reset_clears_both_signals(self):
        p = _proc()
        p.update(600, 20000, now_s=0.0)
        p.reset()
        snap = p.snapshot(1.0)
        assert snap.speed.value is None
        assert snap.altitude.value is None

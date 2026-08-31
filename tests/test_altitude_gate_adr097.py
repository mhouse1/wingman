"""Altitude plausibility gate: units and anchor poisoning (ADR 097).

The gate's physics argument — vertical speed cannot exceed total speed — was
right; the arithmetic converted a KPH reading with MPH_TO_FPS and compared the
result against a delta in metres, leaving it 5.28x looser than physics allows.
The evidence is the session of 2026-08-27 19:13 - 2026-08-28 01:35: 24 of 55
DIVE RECOVERY events were artefacts, every one announcing "2s to ground".
"""

import pytest

from wingman.telemetry import KPH_TO_MPS, MPH_TO_FPS, TelemetryProcessor

# The real readings either side of the 21:35:38 false dive, from wingman.log.
# The aircraft was above 6 km and climbing throughout; 1187 is the artefact.
ALT_2135 = [(6007, 1314), (6700, 1139), (1187, 863), (7394, 595), (7498, 385)]


def _proc(**over):
    cfg = {"max_speed_mph": 2000, "max_altitude_ft": 60000, "plausibility_margin": 1.5,
           "stale_after_s": 6.0, "smoothing_window": 3, "reseed_after_rejections": 3,
           "max_alt_rate_mps": 1000.0, "reseed_agreement_m": 150.0}
    cfg.update(over)
    return TelemetryProcessor(cfg)


def _feed(proc, rows, t0=1000.0, step=3.0):
    """Replay at the real cadence (ocr_every_n_ticks: 2 gives about 3.0 s)."""
    seen = []
    for i, (alt, spd) in enumerate(rows):
        proc.update(spd, alt, t0 + i * step)
        seen.append(proc.snapshot(t0 + i * step).altitude.value)
    return seen


# --- D1: units ---------------------------------------------------------------

def test_the_gate_is_an_absolute_ceiling_not_a_speed_derivative():
    """Speed must not enter the altitude gate at all: a stalled aircraft
    descends faster than its forward airspeed, and coupling the two crops let a
    speed misread license an altitude misread."""
    p = _proc()
    p.update(1139, 5000, 1000.0)
    slow = p._altitude_bound_mps()
    p.update(2652, 5000, 1003.0)
    assert p._altitude_bound_mps() == slow == 1000.0


def test_the_old_conversion_was_5_28x_too_loose():
    """Pins the size of the defect so a revert is loud rather than subtle."""
    assert pytest.approx(5.28, abs=0.01) == MPH_TO_FPS / KPH_TO_MPS


def test_the_false_dive_reading_is_rejected():
    """6700 -> 1187 is a 5513 m drop in 3.0 s, implying 1838 m/s. Allowance is
    1000 m/s x 3.0 s = 3000 m, so it fails. Under the old conversion the
    allowance was 1139 x 1.4667 x 1.5 x 3.0 = 7518 m and it passed."""
    p = _proc()
    values = _feed(p, ALT_2135[:3])
    assert values[-1] == 6700, f"the bogus 1187 was accepted: {values}"


def test_a_real_dive_still_passes():
    """The 31 genuine events showed coherent profiles (-9, -109, -181, -242,
    -447 m/s). Tightening the gate must not starve dive confirmation — the
    2026-07-30 starvation case in ADR 038 is the failure in this direction."""
    p = _proc()
    rows = [(8000, 900), (7100, 900), (6300, 900), (5100, 900)]   # to -400 m/s
    values = _feed(p, rows)
    assert values == [8000, 7100, 6300, 5100], values


def test_climb_at_the_observed_rate_still_passes():
    """The session climbed at +231 to +252 m/s under the BT's Climb tactic."""
    p = _proc()
    values = _feed(p, [(3551, 1000), (4093, 1000), (4661, 1000), (5270, 1000)])
    assert values[-1] == 5270, values


# --- D2: the ceiling ---------------------------------------------------------

def test_the_ceiling_sits_between_the_two_populations():
    """Calibration, pinned: across 9020 accepted samples the plausible dives
    top out at 919 m/s and the artefacts start at 1036 m/s."""
    p = _proc()
    assert 919 < p.max_alt_rate_mps < 1036


def test_a_dead_speed_read_cannot_freeze_the_altitude_gate():
    """The ADR 038 failure this must not reintroduce. With no speed term in the
    gate, a stale speed signal cannot affect altitude at all."""
    p = _proc()
    p.update(900, 5000, 1000.0)
    assert p._altitude_bound_mps() == 1000.0


def test_the_fastest_real_dive_is_still_admitted():
    """919 m/s, observed 2026-08-27 00:56:42. 2757 m in 3.0 s."""
    p = _proc()
    values = _feed(p, [(8000, 1763), (5243, 1763)])
    assert values[-1] == 5243, values


def test_the_slowest_artefact_is_still_rejected():
    """1036 m/s, observed 2026-08-27 19:17:03. 3108 m in 3.0 s."""
    p = _proc()
    values = _feed(p, [(8000, 1763), (4892, 1763)])
    assert values[-1] == 8000, values


# --- D3: anchor poisoning ----------------------------------------------------

def test_agreeing_rejections_reseed_the_anchor():
    """7394 and 7498 are 104 m apart and both correct. Two agreeing rejections
    mean the anchor is wrong, so the filter must reseed instead of continuing
    to publish the error."""
    p = _proc(reseed_after_rejections=99)     # disable the count-only path
    p.update(863, 1187, 1000.0)               # poison the anchor directly
    p.update(595, 7394, 1003.0)               # reject #1
    p.update(385, 7498, 1006.0)               # reject #2 — agrees with #1
    assert p.snapshot(1006.0).altitude.value == 7498


def test_disagreeing_rejections_do_not_reseed_early():
    """Scattered rejections are a noisy sensor, not a poisoned anchor, and must
    still take the full count before reseeding."""
    p = _proc(reseed_after_rejections=99)
    p.update(863, 5000, 1000.0)
    p.update(595, 100, 1003.0)                # reject: 4900 m against a 3000 m allowance
    # The allowance grows with seed age (the anchor's ts does not move while it
    # is being defended), so this must stay outside the 4 s allowance of 4000 m.
    p.update(385, 9500, 1004.0)               # reject: 9400 m from the previous one
    assert p.snapshot(1004.0).altitude.value == 5000


def test_out_of_envelope_garbage_never_reseeds_by_agreement():
    """A consistent stream of absolute-bound garbage agrees with itself. It
    must not become the seed (the seedable=False path)."""
    p = _proc(reseed_after_rejections=99, max_altitude_ft=60000)
    p.update(900, 5000, 1000.0)
    p.update(900, 99999, 1003.0)
    p.update(900, 99999, 1006.0)
    assert p.snapshot(1006.0).altitude.value == 5000


def test_full_2135_sequence_recovers_without_publishing_the_artefact():
    """End to end: the artefact is never published, and the true altitude is
    back within the samples the real session took to recover."""
    p = _proc()
    values = _feed(p, ALT_2135)
    assert 1187 not in values, values
    assert values[-1] == 7498, values


# --- V3: the fix converts bad accepts into rejects, not good ones ------------

def test_rejection_count_does_not_balloon_on_normal_flight():
    """A gate that is too tight starves dive confirmation. Normal climb and
    descent profiles must produce no rejections at all."""
    p = _proc()
    rows = [(1000, 900), (1600, 950), (2300, 1000), (3000, 1050), (3700, 1100),
            (3200, 1100), (2600, 1050), (2000, 1000), (1500, 950)]
    _feed(p, rows)
    assert p.rejected_total == 0

# ADR 123 — NOSE_DIRECTION and the Loiter Entry Pull-Up

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

The survival hold is started by the operator at a moment the operator chooses —
which is usually a moment when something has gone wrong. Measured
2026-09-05 21:31:41:

```
21:31:41  mission_loiter - holding to stay alive (target 7000 m)
21:31:41  mission_loiter - climbing (2953 m below hold)
21:31:42  Altitude: 139 | Speed: 2652 | Nose: -74 deg (steep_dive)
21:31:46  mission_loiter - ended          <- ground
```

The hold was asked to save an aircraft 139 m above the ground, pointed 74
degrees down, doing 2652 KPH. It had about one second. It used it to start a
climb.

Nothing already in the hold could have helped:

- **ADR 114's recovery phase** runs only inside or above the hold band. At 139 m
  the aircraft is far below it, so the hold climbs — correctly, by ADR 114 D4,
  since below the band a steep nose-up is the climb doing its job.
- **`climb_mode`** takes seconds to establish and has its own confirmation.
- **The behaviour tree** re-evaluates on a 1.5 s tick. The ground arrived first.

Every pitch input in the hold is a short non-blocking pulse re-evaluated on the
next tick. That is right for trimming a drift and useless against a vertical
dive.

## Decision

**D1. Track NOSE_DIRECTION continuously.** `NOSE_UP` / `NOSE_DOWN` /
`NOSE_UNKNOWN`, updated from the altitude rate on every telemetry update and
held as state on the analyzer.

**D2. As STATE, not a computation on demand.** The hold starts at an instant it
did not choose, and a fresh telemetry pair may not exist on that tick. Tracked
state holds the last known answer across the gap; a derivation would return
None exactly when it is needed.

**D3. A deadband (`nose_direction_deadband_mps`, 5 m/s), holding the last value
inside it.** Level flight jitters around zero altitude rate. A direction that
flips every tick is not a direction, and the consumer reads it once.

**D4. On entry with NOSE_DOWN, pull up for `entry_pullup_s` (5 s), BLOCKING,
before the hold loop starts.** This is the one place the hold is allowed to
simply hold the stick back rather than pulse. It runs before the loop, before
the tree, before anything else gets a vote.

**D5. NOSE_UNKNOWN does not pull up.** No evidence of a descent is not evidence
of one, and five seconds of held back-pressure is itself a way to stall a
healthy entry.

## Consequences

A hold entered in a dive now spends its first five seconds recovering, which is
what the aircraft needs and what no other mechanism was going to provide in
time.

Those five seconds are spent even when the dive is shallow and recoverable
without help. That is deliberate: the check is cheap, the failure it prevents is
total, and the alternative is a threshold on dive angle that would need its own
evidence to set.

A hold entered nose-down at very low altitude may still hit the ground. Five
seconds of back-pressure at 139 m and 2652 KPH may not be enough. This improves
the odds; it does not make the hold a ground-collision system.

NOSE_DIRECTION is now available to any other consumer that needs "which way is
the nose pointing" without a fresh telemetry pair. Nothing else reads it yet.

## Found live, within three minutes (2026-09-05 10:12:59)

The first live hold caught an ordering bug in this ADR's own change:

```
10:12:59,779  entry with the nose DOWN — holding nose up for 5.0s
10:12:59,779  nose_up - pressing 'i' key for 5.0 seconds
10:12:59,789  nose_up cancelled
```

Ten milliseconds. `mission_loiter` calls `cancel_mission()` to pre-empt the
running mission (ADR 111), which sets `_mission_cancel`; the pull-up was placed
BEFORE that flag is cleared, and `nose_up(block=True)` honoured it. The hold
entered a -64 degree dive having done nothing at all.

The call now runs after `_mission_cancel.clear()`.

**The tests could not have caught it**, and that is the more useful finding: the
fixture built a controller with `_mission_cancel` already clear, which is a
state the real entry path never reaches. Nearly every hold pre-empts something,
so nearly every hold took the broken path. The regression test now sets the flag
first, and it fails against the old ordering.

## The guard now checks itself (ADR 126, same day)

The 10:12:59 failure was **silent**. `nose_up` returned after ten milliseconds
and the log showed only that a pull-up had been requested — a hold entering a
-64 degree dive looked, in the log, exactly like one that had recovered. It was
found by reading a DEBUG line in a window that happened to be open.

The call is now timed, and a hold that gets less than 90% of its requested
back-pressure logs `ENTRY PULL-UP CUT SHORT` at ERROR with what it actually got.

A guard that can silently not run is not a guard. The same shape has now
appeared three times in this work — a fixture that never reproduced the caller's
real state, a monitor filter that could not distinguish a legitimate pulse
cancellation from this failure, and the failure itself.

## Validation

- **V1.** A nose-down entry pulls up for the configured duration, blocking,
  before the hold begins.
- **V2.** A nose-up entry does not pull up.
- **V3.** An unknown direction does not pull up.
- **V4.** The tracked direction follows the altitude rate: rising is UP, falling
  is DOWN.
- **V5.** The deadband holds the last direction through level-flight jitter.
- **V6.** A stale reading does not change the direction — holding the last known
  answer is the point of tracking it as state.
- **V7.** The pull-up runs with `_mission_cancel` CLEAR, so a hold that
  pre-empted a mission still gets its five seconds.
- **V8.** A pull-up that does not hold its full duration is reported at ERROR;
  one that does is not.
- **V9 — live.** A hold started in a dive recovers rather than continuing into
  the ground. Not yet observed.

## References

- ADR 114 — the in-band recovery phase, which by design cannot cover this case
- ADR 110 — the stall/dive cycle this is the entry-side counterpart to
- ADR 073 / ADR 086 — `climb_mode`, too slow to establish here
- `wingman/analyzer.py` — `nose_direction`, `_update_nose_direction`
- `wingman/controller.py` — `_loiter_entry_pull_up`
- `tests/test_mission_loiter.py` — V1-V6

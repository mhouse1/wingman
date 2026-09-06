# ADR 128 — Afterburner During an Incoming Alert

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

On an incoming-missile detection wingman deploys a three-burst of flares and
nothing else. Flares change what the missile is *tracking*. They do not change
whether it can still *reach* the aircraft, and the two are complementary rather
than alternative.

The measured picture supports doing more. Across the 2026-09-05 sessions,
survival ten seconds after an alert:

| session | with evade | without evade |
|---|---:|---:|
| 6h16m soak (125 engagements) | 81% (70/86) | 64% (25/39) |
| 2h28m (32 engagements) | 81% (17/21) | 64% (7/11) |

Consistent, and consistently short of what it could be — roughly one alert in
five is still fatal with the current response.

## Decision

**D1. Hold the afterburner while an incoming detection is present.** Alongside
the flares, not instead of them.

**D2. Release on the ALERT going quiet, not on a fixed burn time.**
`afterburner_clear_s` (4 s) after the last detection. How long the alert
persists is the only signal available for whether the threat is still live; a
fixed burn would guess.

**D3. Refresh the deadline on EVERY tick that sees a detection**, not only on a
new one. The requirement is "until incoming has not appeared for N seconds", so
an alert that persists across ticks has to extend the burn rather than let it
lapse mid-threat.

**D4. Idempotent — a repeat detection extends, it does not start a second
hold.** Two rival holds on one key would fight: whichever finished first would
release the throttle while the other still wanted it.

**D5. Bounded twice: by the quiet deadline and by an absolute cap
(`afterburner_max_s`, 20 s).** `AFTERBURNER_KEY` is not a watched maneuver key,
so a stuck press would not surface as a manual takeover — it would simply be a
throttle nobody could release. The cap logs when it fires with the alert still
live, because that is a different situation from a normal release.

**D6. Re-press the key about once a second.** `climb_mode` drives the same key
and releases it on its own schedule, so a climb ending mid-alert would cut the
burn silently — the feature would be off exactly when a missile is inbound, with
nothing in the log to say so. Pressing an already-held key is harmless.

## Consequences

Fuel burns faster during alerts. `climb_mode` has a fuel floor for its own
afterburner use; this hold does not consult it, deliberately — a missile in the
air outranks fuel economy, and the alert is bounded at 20 s.

Two subsystems now drive one key on independent schedules. D6 makes the evade
robust against the climb, but not the reverse: a climb that expects the
afterburner released may find it held for up to 4 s afterwards. That is the
known interaction, and it is the acceptable direction of the two.

This does not change flare behaviour, the missile-evade manoeuvre (ADR 070), or
tactic selection. It adds throttle to an existing response.

**Whether it helps is an open question.** The survival-rate split above is the
metric, and it needs a soak of comparable size to move meaningfully. It is also
possible that speed hurts — a faster aircraft has a wider turn radius and less
ability to defeat a missile geometrically. The measurement decides.

## Validation

- **V1.** An incoming detection holds the afterburner and later releases it.
- **V2.** A tick with no alert presses nothing.
- **V3.** The hold lasts at least the quiet window, not a fixed burn.
- **V4.** A repeat detection extends the hold rather than starting a second.
- **V5.** The absolute cap bounds the hold and still releases the key.
- **V6.** The key is re-pressed, so a concurrent climb cannot cut the burn.
- **V7.** Shutdown releases the key.
- **V8 — live.** Survival ten seconds after an alert rises above the 81% / 64%
  split measured before this change, over a comparable number of engagements.
  Not yet observed.

## References

- FR-008 — the requirement this implements
- ADR 070 — the missile-evade manoeuvre, unchanged
- ADR 073 / ADR 086 — `climb_mode`, the other consumer of the afterburner key
- ADR 116 — why the config block is read from the ControllerConfig attribute
- `wingman/controller.py` — `note_incoming`, `_start_afterburner_evade`
- `tests/test_afterburner_evade.py` — V1-V7

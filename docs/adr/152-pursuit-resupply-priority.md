# ADR 152 — Escalating Resupply Priority in Pursuit Mode

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-29 | 1.9.0           |

## Decision

**D1. Pursuit mode recognizes the yellow resupply icon as a distinct target.**
Add a detector for the yellow icon shown in
`tests/test-output/MISSILE_EMPTY_RESUPPLY.png` and
`tests/test-output/MISSILE_EMPTY_RESUPPLY2.png`. The source captures are in the
ignored test-output directory; committed copies in `tests/fixtures/` are the
detector tests' inputs. The resupply icon is a distinct target; do not feed it
through the red/orange `IconPoints` detector or assume it shares that
detector's ring geometry. Use its detected position to guide pursuit steering
toward resupply.

**D2. Resupply urgency is cumulative and monotonic.** Keep a confirmed
remaining-ammo count for each rack across weapon switches. During the existing
post-switch grace period, ignore zero readings that may still belong to the
previous rack, but accept stable positive readings as the new rack's baseline:
the previous rack was confirmed empty before switching. Use
`empty_confirm_reads` consecutive readings to confirm an ammo-count change.
Each confirmed missile spent adds one unit to the resupply urgency. A rack
switch, unreadable reading, or transient OCR increase must not reduce urgency.
Do not add configurable urgency thresholds in the first implementation. Each
missile spent adds one `icon_steering.points_scale` unit to resupply urgency.
A locked target has one such unit of priority, and the target wins ties. Thus a
visible resupply marker does not interrupt a target attack after only one
missile is spent, but can take priority after further missiles are used. Zero
confirmed missiles remains the maximum-priority state.

**D3. Resupply priority arbitrates pursuit steering.** While missiles remain,
the locked target keeps priority until resupply urgency strictly exceeds its
one-unit baseline; against unlocked search, compare resupply urgency with the
current opponent-icon points. When a visible resupply marker wins, temporarily
yield target steering and focus on resupply. Confirmed rearm ends that focus,
resets urgency, and resumes target pursuit. At confirmed zero missiles, the
marker always wins and firing is disabled. If the marker is not detected,
continue existing pursuit behavior and keep scanning; do not steer toward a
stale marker position or lower urgency. Existing hard safety ownership still
takes precedence, and urgency is retained while steering yields.

**D4. Resupply focus is gated and not limited to zero missiles.** When
`pursuit_mode.resupply_priority.actuate` is true, a visible marker can interrupt
target attack as soon as its urgency wins the priority comparison, including
before zero. At confirmed zero, a visible marker always takes steering
priority, firing stops, and the current ammo-exhaustion handoff to
`eject_and_dive` is suppressed. Until actuation is enabled, shadow mode logs
the same proposals and preserves existing control and handoff behavior. Keep
the configured pursuit duration cap and external cancellation behavior
(respawn, manual takeover, shutdown) unchanged. If a positive cap expires
before rearm, retain the existing fall-through to `eject_and_dive`. The shipped
cap is `0` (unbounded), so with no marker or confirmed rearm the actuated
pursuit can continue until respawn or external stop; this remains an explicit
operational risk.

**D5. Confirmed rearm or respawn starts a fresh priority cycle.** While the
resupply proposal is active, a confirmed positive increase in the same rack's
ammo count indicates rearm even if the rack never reached zero. Record the new
baseline, clear the spent count, end resupply focus, and resume ordinary target
pursuit. Ignore increases outside an active resupply focus as OCR noise.
Respawn always ends the current pursuit and clears all rack counts and urgency;
the next pursuit starts at its initial priority. No state carries across lives.

## Consequences

- The pursuit loop gains yellow-icon detection and a resupply steering
  objective, while opponent icon detection remains unchanged.
- Confirmed ammunition readings must be attributed to the correct weapon rack
  so a weapon switch neither resets urgency nor falsely declares the inventory
  empty.
- A visible resupply marker competes with a locked target before zero using
  the linear urgency comparison. The target wins ties; resupply wins only when
  more missiles spent makes its urgency strictly higher.
- At zero missiles the aircraft may remain in pursuit until it reaches
  resupply, is stopped externally, or reaches its configured duration cap.
  The shipped cap is currently unbounded, so failure to detect a resupply icon
  can leave a zero-ammo pursuit running until an external stop or respawn.
- The named captures are currently ignored under `tests/test-output/`; tests
  remain there as operator references; detector tests use the committed copies
  in `tests/fixtures/` and include negative yellow lookalikes.

## Implementation Status

Implemented 2026-09-29 in `wingman/resupply.py` and
`Controller.pursue_and_engage`. Resupply detection and urgency telemetry are
enabled in shipped config, while resupply steering actuation remains disabled.
Ammo changes are confirmed by consecutive reads, tracked per rack, and stale
post-switch zeroes are ignored during the existing grace period. Resupply
urgency competes with target attack before zero; target priority wins ties.
During resupply focus, a confirmed same-rack ammo increase resets urgency and
resumes target pursuit. At terminal zero, actuated mode stops firing; a
detected marker wins steering, while a missing marker preserves ordinary
pursuit. Every new pursuit after respawn resets urgency.

## Evidence and Assumptions

- **Measured:** the latest `wingman.log` summary records 61 missions and zero
  missiles-empty events. Its first line is timestamped 00:52, before this ADR
  was edited at 06:53; it provides no evidence about resupply behavior.
- **Measured:** both source captures exist in ignored `tests/test-output/`;
  byte-for-byte copies are committed as `tests/fixtures/` inputs.
- **Inferred:** the resupply target can be used to steer by its detected
  on-screen position. It is a different visual signal from the red/orange
  enemy direction markers.
- **Assumed:** detecting and approaching the yellow marker leads to a rearm
  that can be confirmed by a positive ammo reading. Validate this in shadow
  mode and a live trial before enabling resupply steering.

## Validation

- **V1. Passed.** `tests/test_resupply.py` recognizes both committed captures
  and rejects synthetic solid-yellow and small-glyph lookalikes.
- **V2. Passed.** Unit tests cover confirmed per-rack decreases, monotonic
  urgency, switch preservation, transient increases, terminal-zero
  confirmation, and rearm reset.
- **V3. Passed.** Pursuit tests cover the pre-zero lock threshold, marker
  override once urgency exceeds target priority, confirmed rearm and target
  resumption, no firing at zero, no-marker fallback, and legacy shadow behavior.
- **V4. Passed.** Policy and pursuit tests cover respawn/external stop, rearm
  reset, duration-cap fall-through, and deferred rack switching.
- **V5. Pending live evidence.** Shipped config logs marker visibility,
  attributed ammo by rack, urgency, and proposed steering with `actuate: false`.
  A live trial must confirm marker detection and that approaching it produces
  a positive ammo reading before actuation is enabled.

Final automated gates on 2026-09-29: `make lint` passed; `make test` passed
with 2,471 passed and 75 skipped. Live marker/rearm behavior has not been
observed, so ADR status remains Draft and steering actuation remains gated off.

## References

- [Design 015 — Target-Tracking Pursuit Mode](../hldd/015-target-tracking-pursuit-mode-hldd.md)
- [ADR 144 — Mission SU-30 scripted sequence](144-mission-su30-scripted-sequence.md)
- `wingman/controller.py`: `pursue_and_engage`, `_icon_rung`
- Operator reference screenshots: [MISSILE_EMPTY_RESUPPLY.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY2.png)
- Committed detector fixtures: [MISSILE_EMPTY_RESUPPLY.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY2.png)
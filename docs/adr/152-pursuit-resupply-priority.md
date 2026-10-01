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
Do not add a configurable urgency threshold. A visible resupply marker takes
priority once at least two missiles have been confirmed spent; one spent
missile alone does not interrupt normal pursuit. Confirmed zero remains the
maximum-priority state.

**D3. Resupply priority arbitrates pursuit steering.** Once at least two
missiles have been confirmed spent, a visible marker temporarily takes priority
over target and opponent-icon steering. If a later scan misses the marker,
continue steering toward its last accepted position for at most half a second
while scanning for it again. Then return to normal pursuit until it is detected.
Small icons rejected by the detector remain ignored. Confirmed rearm ends the
focus, clears the held position, resets urgency, and resumes normal pursuit. At
confirmed zero missiles, the marker always wins and firing is disabled. Existing
hard safety ownership still takes precedence, and urgency is retained while
steering yields. The selected objective is shown by the same magenta HUD
steering vector and ring used for target pursuit, labeled `RESUPPLYING` (or
`RESUPPLYING (lost)` while using the bounded hold). The enemy tracker remains
unchanged; the HUD receives the active steering objective separately.

**D4. Resupply focus is gated and not limited to zero missiles.** When
`pursuit_mode.resupply_priority.actuate` is true, a visible or briefly held
marker can interrupt normal pursuit once two missiles are spent, before zero.
At confirmed zero, a visible or briefly held marker always takes steering
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
  objective, while opponent icon detection remains unchanged. HUD annotation
  follows whichever objective currently owns steering.
- Confirmed ammunition readings must be attributed to the correct weapon rack
  so a weapon switch neither resets urgency nor falsely declares the inventory
  empty.
- A visible resupply marker takes priority over normal pursuit after two
  confirmed missiles have been spent, regardless of target or opponent-icon
  strength.
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
enabled in shipped config. Shadow-session coverage supports advancing to a
live actuation trial; `pursuit_mode.resupply_priority.actuate` is enabled for
that trial.
Ammo changes are confirmed by consecutive reads, tracked per rack, and stale
post-switch zeroes are ignored during the existing grace period. Resupply
steering begins when a marker is visible or recently detected and
either two missiles are confirmed spent or ammo is confirmed zero. The recently
detected position is retained for at most half a second during focus; detector
thresholds remain unchanged. The shared magenta HUD selected-target annotation
is labeled `RESUPPLYING` while this objective controls the axes. During resupply
focus, a confirmed same-rack ammo increase resets urgency and resumes target
pursuit. At terminal zero, actuated mode stops firing; without a fresh or
briefly held marker, ordinary pursuit steering resumes. Every new pursuit after
respawn resets urgency.

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
  that can be confirmed by a positive ammo reading. The shadow run below
  supports trying actuation, but only live evidence can establish marker
  precision and confirm rearm.

## Validation

- **V1. Passed.** `tests/test_resupply.py` recognizes both committed captures
  and rejects synthetic solid-yellow and small-glyph lookalikes.
- **V2. Passed.** Unit tests cover confirmed per-rack decreases, monotonic
  urgency, switch preservation, transient increases, terminal-zero
  confirmation, and rearm reset.
- **V3. Passed.** Pursuit tests cover the two-missile marker threshold,
  bounded hold through a brief detector dropout and expiry, confirmed rearm and
  target resumption, HUD selected-target replacement and label, no firing at
  zero, no-marker fallback, and legacy shadow behavior.
- **V4. Passed.** Policy and pursuit tests cover respawn/external stop, rearm
  reset, duration-cap fall-through, and deferred rack switching.
- **V5a. Shadow coverage (2026-09-29).** The 13h54m session in
  `logs/wingman_20260929_222852.log` covered 135 missions and 543 pursuit
  episodes. It recorded 53,657 resupply telemetry lines, including 8,969
  marker-positive scans and 2,678 ticks where resupply would have won priority.
  No resupply errors were logged. The shadow run did not save candidate frames
  or record confirmed rearm resets, so it is enough to advance to an actuated
  trial, not to validate marker precision or close V5.
- **V5b. Partial live evidence (2026-09-29 canary).** One `make r1` session used
  a local 30-second pursuit cap and `actuate: true`; shipped config remained
  unchanged. Across 10 pursuit episodes, two transient marker candidates were
  logged and one priority crossover occurred at `spent=2` (marker positions
  `(211,551)` and `(177,153)`). The pursuit then reached its cap at 30.4s with
  ammo `4->2`; no positive ammo increase or rearm was observed. The marker was
  absent on intervening scans, so sustained resupply steering was not
  demonstrated. No resupply scan or pursuit-loop errors occurred. V5 remains
  open pending a frame-verified marker and a confirmed ammo increase.
- **V5c. Actuated trial (2026-09-30).** The current live log recorded two
  pursuits with resupply actuation enabled. One candidate frame visibly shows
  the yellow marker; a second candidate appears to be an explosion/fire
  false positive. Priority crossed at `spent=1`, but both pursuits ended
  externally with ammo `2->2`; no confirmed rearm was observed. This supports
  that the live path can detect and prioritize a marker, while marker precision
  and rearm remain unverified. V5 remains open.

Final automated gates on 2026-09-29: `make lint` passed; `make test` passed
with 2,471 passed and 75 skipped. ADR status remains Draft; V5 remains open
pending a reliable marker and confirmed ammo increase during resupply focus.

## References

- [Design 015 — Target-Tracking Pursuit Mode](../hldd/015-target-tracking-pursuit-mode-hldd.md)
- [ADR 144 — Mission SU-30 scripted sequence](144-mission-su30-scripted-sequence.md)
- `wingman/controller.py`: `pursue_and_engage`, `_icon_rung`
- Operator reference screenshots: [MISSILE_EMPTY_RESUPPLY.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY2.png)
- Committed detector fixtures: [MISSILE_EMPTY_RESUPPLY.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/fixtures/MISSILE_EMPTY_RESUPPLY2.png)
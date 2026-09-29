# ADR 152 — Escalating Resupply Priority in Pursuit Mode

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-29 | 1.9.0           |

## Decision

**D1. Pursuit mode recognizes the yellow resupply icon as a distinct target.**
Add a detector for the yellow icon shown in `MISSILE_EMPTY_RESUPPLY.png` and
`MISSILE_EMPTY_RESUPPLY2.png`. It is not an opponent direction icon: do not
feed it through the red/orange `IconPoints` detector or assume it shares that
detector's ring geometry. Use the icon's observed position to guide pursuit
steering toward resupply.

**D2. Resupply urgency rises as missiles are spent.** During one pursuit life,
each confirmed missile spent raises the resupply priority monotonically. The
priority is computed across weapon switches; a zero reading for a rack being
switched away from does not count as zero missiles remaining when another rack
is available. When the confirmed available missile count reaches zero,
resupply becomes the highest-priority pursuit objective and firing is disabled.

**D3. Resupply priority arbitrates pursuit steering.** When a resupply icon is
visible, its bearing increasingly takes precedence over opponent-directed
search as priority rises. At zero missiles, steer toward the resupply icon in
preference to enemy search or tracking objectives. Existing dive-recovery and
other hard safety ownership still take precedence over pursuit steering; the
resupply urgency is retained while steering yields.

**D4. Zero missiles no longer ends pursuit by itself.** The current
`pursue_and_engage` path falls through to `eject_and_dive` after confirmed
secondary-ammo exhaustion. Replace that ammo-exhaustion handoff with the
zero-ammo resupply-seeking state. Keep the configured pursuit duration cap and
external cancellation behavior (respawn, manual takeover, shutdown) unchanged.
If the cap expires before resupply, retain the existing fall-through to
`eject_and_dive`.

**D5. Respawn starts a fresh priority cycle.** Missile usage and resupply
priority are scoped to one pursuit life. A respawn ends the current pursuit;
the next pursuit starts at its initial priority using the rearmed missile
inventory. No urgency or spent-ammo state carries across lives.

## Consequences

- The pursuit loop gains yellow-icon detection and a resupply steering
  objective, while opponent icon detection remains unchanged.
- Confirmed ammunition readings must be attributed to the correct weapon rack
  so a weapon switch neither resets urgency nor falsely declares the inventory
  empty.
- At zero missiles the aircraft may remain in pursuit until it reaches
  resupply, is stopped externally, or reaches its configured duration cap.
  The existing duration-cap default and safety behavior are not changed here.
- The named screenshot files are detector fixtures for implementation and
  replay validation at `tests/test-output/`; include negative yellow
  lookalikes as well.

## Validation

- **V1.** Detector tests recognize the yellow resupply icon in both named
  screenshots and reject representative yellow non-resupply elements.
- **V2.** Pursuit tests prove priority never decreases as confirmed missiles
  are spent, reaches its maximum at zero available missiles, and does not peak
  on a zero reading that only triggers a weapon switch.
- **V3.** When resupply is visible, steering preference follows the priority
  progression and at zero missiles overrides opponent-directed search;
  firing is disabled at zero.
- **V4.** Respawn stops the old pursuit and the next pursuit initializes at
  its starting priority. A configured duration-cap expiry still falls through
  to `eject_and_dive`, and external cancellation never does.
- **V5.** Shadow-first/live validation confirms the yellow marker's position
  is stable enough to steer toward and that the aircraft does not mistake
  unrelated yellow HUD content for resupply before actuation is enabled.

## References

- [Design 015 — Target-Tracking Pursuit Mode](../hldd/015-target-tracking-pursuit-mode-hldd.md)
- [ADR 144 — Mission SU-30 scripted sequence](144-mission-su30-scripted-sequence.md)
- `wingman/controller.py`: `pursue_and_engage`, `_icon_rung`
- Operator reference screenshots: [MISSILE_EMPTY_RESUPPLY.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY.png),
  [MISSILE_EMPTY_RESUPPLY2.png](../../tests/test-output/MISSILE_EMPTY_RESUPPLY2.png)
# ADR 190 — An unknown special weapon is pressed whenever it is lit

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-18 | 1.9.5           |

## Decision

Operator, 2026-11-18: "the special weapon for phantom missiles should be activated
whenever it appears, implement it such that anytime an undefined special weapon appears
activate it automatically so this would make the ACS system more adaptable for new unknown
abilities".

- **D1. A glyph no row knows gets `special_weapon.unknown_trigger`** (shipped `ready`). The
  ring reader names a lit ring round an unmatched glyph `other` (ADR 186). `ability_profile`
  now answers `other` with `{ability: unknown, trigger: <unknown_trigger>}`. With `ready`,
  the battle tick presses `q` whenever the icon reads READY, and again on every recharge.
  - A jet's own `jet_profile.<jet>.special_weapon` still wins, as before.
  - A named glyph keeps its row's rule (refuel, gun cool-down, nuke, pair fire).
  - `unknown_trigger: none` turns the rule off. An aimed trigger (`lead_aligned`) is not
    allowed, because it needs a lead colour from a row.
- **D2. Phantom missiles get their own row**: `phantom_missiles: {glyph: …/phantom_missiles.png,
  trigger: ready}`. D1 would press them anyway; the row names them in the log
  (`Special weapon glyph: phantom_missiles`) and keeps their rule if D1 is ever turned off.

## Consequences

- A new jet with an ability wingman has never seen gets `q` pressed whenever it is lit,
  with no config and no code. The `GLYPH: unknown` line and the saved `unknown_*` crop
  (ADR 173 D2e) still name it, so it can be given a row with a better trigger.
- An ability that is worth saving (refuel, an aimed weapon) is spent as soon as it lights
  until it has a row. That is the trade the operator asked for.
- A jet whose known glyph is read as `other` first is pressed early. `other` needs two lit
  reads in a row that match no template at 0.85; the known templates matched their own
  jets' saved crops at 0.95 to 1.00 (refuel on 689 crops, ADR 173; pair fire and phantom
  missiles, ADR 188 and this ADR). A named glyph replaces `other` once two
  reads agree.
- Glyphs without rows today that this now presses: the Su-47 loop, the F-5 arrows, the
  Mirage glyphs and any jet without a profile in `jet_profile`. The MiG-23, J-20, JAS 39 and
  Tornado keep their own profiles.

## Evidence

The operator's three `v` screenshots of 2026-11-18 07:31 to 07:33, panel weapon PL-8
(saved as `tests/fixtures/phantom_missiles/*.jpg`):

| Screenshot | Icon | Ring lit (of 16) | Phantom-missiles glyph overlap |
|---|---|---|---|
| 073143 | ready (the template) | 16 | 1.00 |
| 073256 | ready | 16 | 0.99 |
| 073300 | countdown "18" | 0 | 0.06 |

- Every other glyph crop: 0.12 or less (a match needs `glyph_min_iou` 0.85).
- Saved icon crops at 0.5 or more: four, all from this jet between 07:11 and 07:31 today
  (0.95 to 1.00); the live log named them `unknown_refuel11` and `unknown_refuel12`.

## Implementation

- `wingman/special_weapon.py`: `ability_profile` answers `other`.
- `wingman/config_schema.py`, `wingman/config.yaml`: `unknown_trigger`, the
  `phantom_missiles` row.
- `test_screenshots/special_weapon/glyphs/phantom_missiles.png` (untracked, like the other
  glyph crops).
- `tests/test_unknown_ability.py`; fixtures in `tests/fixtures/phantom_missiles/` need
  `git add -f`. `tests/test_special_weapon.py`: the refuel-glyph test uses another known
  glyph, since `other` is now pressed.

## Validation

- [ ] Live, PL-8 jet: `Special weapon glyph: phantom_missiles`, then
      `✨ Special weapon phantom_missiles: trigger ready, icon READY` on each recharge.
- [ ] Live, any jet with no row: `Special weapon glyph: other`, then
      `✨ Special weapon unknown: trigger ready, icon READY` and the icon leaving READY.

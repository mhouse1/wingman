# ADR 186 — An ability aimed on its lead diamond: the nuke

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-17 | 1.9.5           |

## Decision

Operator, 2026-11-17, flying the F-106 Delta Dart: "when the nuke special ability is
detected as available, wingman has a single use nuke [that] needs the boresight to align
with the diamond before launching ... when nuke is available it should select the target
closes to the center of the screen, in this case as shown in screenshot_20261117_024050.png
it should select the su27 to target. the special abilities should be built agnostic to jet
type so same jets with the same abilities uses the same implementation. The implementation
should be baked into the ACS system so its scalable, refactor the ACS system if needed."

- **D1. An ability is a row, not a jet.** `special_weapon.abilities` in `config.yaml` maps
  each ability known by its glyph to `{glyph, trigger, ...}`. Any jet whose special-weapon
  icon draws that glyph gets that row's rule. A jet's own `jet_profile.<jet>.special_weapon`
  still wins over its glyph (`special_weapon.resolve_profile`, the one resolver that the
  battle tick and the pursuit both use). Refuel and gun cool-down move into the table from
  their hard-coded branches (ADR 173 D2a, D2b), with their rules unchanged. The
  `refuel_glyph` and `gun_cooldown_glyph` keys are gone.
  - A new jet with a known ability needs nothing.
  - A new ability is a glyph crop and a row, and needs code only for a new trigger.
- **D2. The nuke's row:** `nuke: {glyph: …/nuke.png, trigger: lead_aligned, lead: yellow}`.
- **D3. `lead_aligned` is aimed on the pursuit's own pictures**, several times a second, not
  on the 1.5 s battle tick, which now skips it (`AIMED_TRIGGERS`). While the icon reads
  READY:
  - The rule looks for the yellow lead diamond within `aimed.search_px` (600 px at 1200 high)
    of the crosshair. It uses the rocket pod's diamond template (ADR 180 D3) in the
    ability's colour.
  - **The target is the diamond nearest the crosshair**: the one closest to the centre of
    the screen. It must be the nearest on two pictures in a row.
  - The rule searches nothing while the icon is not READY.
- **D4. The ACS pursuit has one aim chain** (`Controller._pursuit_aim`), highest precedence
  first: ability (this ADR), rocket lead (ADR 180), gun lead ring (Design 021). Each source
  sees every picture, because each keeps its own two-picture confirmation, and the first
  source with an aim wins. This replaces two inline overrides; the rocket-over-ring order is
  unchanged. A new aimed weapon is one more source here and one fire rule beside
  `_pursuit_gun_tick` and `_pursuit_rocket_tick`.
- **D5. `q` goes out with the diamond within `aimed.fire_px` (60 px) of the crosshair**,
  through `SpecialWeaponPresser`: READY only, and one retry after `press_retry_s` if the icon
  does not answer, as for every other trigger. Taking the aim off resets the count.
- **D6.** `special_weapon.aimed.enabled: false` turns the rule off. The nuke is then never
  pressed.

## Consequences

- With the nuke READY and a diamond in view, the chase leaves its tracked target for the
  diamond nearest the centre until `q` is pressed and the icon leaves READY.
- Steering on the diamond needs the tracker to have a target in view (`control_visible`), as
  the rocket and ring aims do. The press needs only the diamond on the crosshair.
- Resupply and priority-marker control still outrank every aim source (unchanged). With the
  nuke READY on the way to a resupply, the nuke waits.
- The search costs about 24 ms per pursuit picture, measured offline on a 1920x1200 frame,
  and only while the icon is READY on a `lead_aligned` jet.
- Only the pursuit (ACS, and J20 after its descent) runs the rule. The heatdive does not.
- On `ready_one_diamond_023941` the diamond sits beside a friendly's marker. The diamond is
  the game's lead, so wingman fires where the game aims; it does not check for friendlies.

## Evidence

The operator's five `v` screenshots, 2026-11-17 02:38 to 02:41, F-106 (saved as
`tests/fixtures/nuke/*.jpg`):

| Screenshot | Ring lit (of 16) | Nuke glyph overlap | Yellow diamonds (score) |
|---|---|---|---|
| 023810 charging | 8 | 1.00 | none |
| 023941 ready | 16 | 0.98 | (950, 523) 0.82 |
| 024050 ready | 16 | 0.99 | FGR4 (491, 589) 0.80, Su-27 (946, 562) 0.79 |
| 024121 ready, no diamond | 16 | 1.00 | none above 0.40 |
| 024123 ready | 16 | 0.99 | (732, 595) 0.83 |

- The nuke glyph against every other glyph crop: 0.40 or less, where a match needs 0.85.
- The best yellow-diamond match in 153 older full-size screenshots: 0.43. `min_score` is
  0.65.
- The session log shows the F-106's glyph as `other`, with the ring READY and no press:
  `02:52:15,341 Special weapon glyph: other (best match 0.14)`.

## Implementation

- `wingman/aimed_ability.py` (new): `AimedAbility`, the `lead_aligned` rule, with no key I/O.
- `wingman/special_weapon.py`: `abilities`, `ability_profile`, `resolve_profile`, the
  `lead_aligned` trigger and `AIMED_TRIGGERS`. Glyph templates now come from the table.
- `wingman/rocket_lead.py`: `LEAD_COLOURS`; `find_diamonds` and `RocketLead` take a colour.
- `wingman/controller.py`: `_pursuit_aim`, `_ability_aim`, `_pursuit_ability_tick`.
- `wingman/tick_handlers.py`: the shared resolver. The battle tick skips aimed triggers.
- `wingman/perception.py`, `tests/perception_fake.py`: `special_weapon_ready`,
  `note_special_weapon_press`.
- `wingman/config.yaml`, `wingman/config_schema.py`, `wingman/controller_config.py`.
- `test_screenshots/special_weapon/glyphs/nuke.png`: a copy of `other_nuclear_f106.png`.
- `tests/test_aimed_ability.py`. Its fixtures in `tests/fixtures/nuke/` need `git add -f`.

## Validation

- [ ] Live, F-106: `Special weapon glyph: nuke` within a few seconds of the icon lighting.
- [ ] Live: `ABILITYAIM: diamond at …` with a diamond in view, and the chase turning onto it.
- [ ] Live: `Special weapon nuke: lead diamond on the crosshair` followed by the icon leaving
      READY (the press taken), and the kill feed showing the nuke's hits.
- [ ] Live: no `q` from the nuke with no diamond on screen.

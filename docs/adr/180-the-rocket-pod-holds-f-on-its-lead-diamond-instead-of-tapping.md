# ADR 180 — The rocket pod holds `f` on its lead diamond instead of tapping

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-15 | 1.9.4           |

## Decision

Operator, 2026-11-15, flying the F-100 with its LAU-3 Hydra Rocket Pod: "fire
rockets by holding down the 'f' FIRE_ACTIVE_WEAPON key … currently the
FIRE_ACTIVE_WEAPON is pressed at an interval assuming missiles are active; when
rockets are engaged it should disable the FIRE_ACTIVE_WEAPON continuous interval
and hold FIRE_ACTIVE_WEAPON when rockets need to fire". The rocket's lead is the
"diamond shaped box, same color as the machine gun lead".

On how the selected weapon is known: "reading weapon type based on icon is not
scalable … replace it with text reading … we'll need this in the future to
distinguish between radar missiles (long range) and heatseeking missiles (short
range)", and "there's other weapon types that we haven't designed for yet such
as guided missiles, the design should be scalable to include new weapons".

- **D1. The selected weapon is read from its name.** The weapon panel prints the
  selected weapon's name above its slot (crop `WEAPON_NAME`). The analyzer reads
  it by OCR on each battle pass, one read in flight at a time, and nothing waits
  on it. `weapon.classes` in `config.yaml` sorts the name into a class. Each
  class has the words its weapons' names contain and a `fire` mode:

  | Class          | `fire`         | Words                      |
  |----------------|----------------|----------------------------|
  | `rocket`       | `hold_on_lead` | Rocket, Hydra              |
  | `heat_seeking` | `tap`          | Sidewinder, AIM-9, PL-8    |
  | `radar`        | `tap`          | Adder, R-77                |

  - A new weapon is a word in the table. A new kind of weapon (a guided
    missile) is a new class. Neither needs code while its `fire` mode exists.
  - A new way of firing is a new `fire` mode: one entry in the schema's choices
    and one rule in the controller beside the rocket rule.
  - A name in no class is tapped, as every weapon was before, and logged once
    (`Weapon selected: … is in no weapon class`).
  - `heat_seeking` and `radar` fire the same way today. They are separate so
    that range rules can tell them apart later.
  - A reading older than `stale_after_s` (6 s) is no reading: `f` is tapped.
  - One read in no class does not displace a classed weapon; the second in a
    row does. Over bright ground "R-77 Adder" read as "RZZ lidiel".
- **D2. No tap for a weapon that is not fired by tapping.** Every loop that taps
  `FIRE_ACTIVE_WEAPON` at an interval calls `fire_active_weapon` (the pursuit,
  the heatdive, boresight, search-and-destroy). That function presses nothing
  while the selected weapon's `fire` mode is not `tap`.
- **D3. The lead diamond** is found by template. The template is the diamond's
  outline and ticks, 57 px across the ticks at 1200 px high. The search covers
  HUD-green pixels within `search_px` (450) of the tracked marker, above the
  weapon panel. A match counts from `min_score` (0.6). Two pictures in a row
  within 160 px confirm it, and it stays the aim for `memory_s` (0.45 s).
- **D4. The chase steers the crosshair onto the diamond** while one is confirmed
  (`steer: true`). It takes precedence over the gun ring's aim (Design 021
  Phase 3), because the pod is the selected weapon.
- **D5. `f` is held** while the diamond is within `fire_px` (60 px at 1200 high)
  of the crosshair. The gun's rule (ADR 178 Rev 4) is the same: get it close and
  the game's aim assist does the rest. The hold lets go at 1.5 × `fire_px`, or
  when the target or the diamond is lost, or when the selected weapon is no
  longer a `hold_on_lead` one. Every other release (takeover, cancel, loop exit,
  yield) goes through `release_tracking_holds`. Only the pursuit and the
  heatdive run this rule.
- **D6.** `pursuit_mode.rocket_lead.enabled: false` or `weapon.enabled: false`
  restores the tap for every weapon.

No A/B trial: the tap fires the pod blind, and the operator asked for this rule.

## Consequences

- The pod's rockets go only where the diamond is on the crosshair. Before, 38
  went in about 23 s.
- In a mission that never runs the pursuit or the heatdive, a selected rocket
  pod does not fire at all.
- After a switch to the pod, the tap carries on until the name is read: up to
  one battle pass (1.5 s) plus the read.
- One more OCR read per battle pass, off the pass's critical path. One more
  template match per picture while a `hold_on_lead` weapon is selected and a
  target is in view: about 22 ms on a 1000 × 900 px window.
- **Not fixed here: the weapon switch flip-flops.** After the AIM-9Ms ran out at
  14:08:22, the empty-weapon rule pressed `g` six times in 26 s, between the pod
  and the empty rack. `AMMO_MISSILE` (x 1851–1869) clips the first digit of a
  two-digit rocket count (x 1846 onward), so a count of 10 to 19 can read as a
  single digit, and 10 as "0". Three such reads switch away from a pod that
  still has rockets. That needs its own change, made against the OCR on real
  panel crops.

## Evidence

The operator's `v` screenshots in `tests/test-output/`, all 1920 × 1200. The
name is the `WEAPON_NAME` crop read by `_process_weapon_name_region` (EasyOCR at
twice the size):

| Frame               | Name as read              | Class          | Lead diamond            |
|---------------------|---------------------------|----------------|-------------------------|
| 2026-11-15 14:08:36 | LAU-3 Hydra Rocket Pod    | `rocket`       | (1445, 308), score 0.66 |
| 2026-11-15 14:08:47 | LAU 3 Hydra RRocket Pod   | `rocket`       | (448, 733), score 0.73  |
| 2026-11-15 14:08:29 | AIM-gM Sidewinder         | `heat_seeking` | none                    |
| 2026-11-15 07:03:34 | AIM-9M Sidewinder         | `heat_seeking` | none                    |
| 2026-11-15 04:48:49 | PL-8                      | `heat_seeking` | none                    |
| 2026-11-13 11:28:10 | R-77 Adder                | `radar`        | none                    |

- Three other 2026-11-13 frames of the R-77 over bright ground read "RiZZ
  Adder" (still `radar`), "R-17 Adtel" and "RZZ lidiel" (no class).
- Of eight recent screenshots, the best diamond match that was not a diamond
  scored 0.55 (07:03:34): the crosshair over a tilted, tickless missile box.
- The crops are in `tests/fixtures/weapon_name/` and `tests/fixtures/rockets/`.

First live data, 2026-11-15 15:54 (F-4 Phantom, on the earlier icon read of the
selected weapon): the F-4's secondary is a rocket pod too (count 75). The taps
stopped at 15:54:48, 0.13 s after the switch, and stayed off for the 10 s the
pod was selected, until the aircraft died. No diamond was confirmed in that
time, so nothing was held.

`wingman.log` (operator's `make r2` session, F-100):

```
14:08:22,896 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the secondary
14:08:25,865 [DEBUG] BT[active]: selected=Climb missiles=37
14:08:28,945 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the primary
14:08:31,742 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the secondary
14:08:40,952 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the primary
14:08:43,792 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the secondary
14:08:48,887 [INFO] Controller: pursue_and_engage — selected weapon empty (3 consecutive zero reads), switching to the primary
```

The `f` taps ran 0.31 to 0.42 s apart throughout (for example 14:08:00,205,
…00,538, …00,924, …01,261).

## Validation

- [ ] Live: `Weapon selected: 'LAU-3 Hydra Rocket Pod' -> rocket (hold_on_lead)`
      after the switch to the pod, then `FIRE_ACTIVE_WEAPON is held on the lead
      diamond, not tapped`, and no `fire_active_weapon - pressing 'f'` line
      while the pod is selected.
- [ ] Live: `HOLD[rockets]: None -> fire` lines with `diamond_px` under 60, and
      the pod's count falling only during holds.
- [ ] Live: `ROCKETAIM: diamond` while the pod is selected and a target is near.
- [ ] Live: every `is in no weapon class` name reviewed and given a class.
- [ ] `tests/test_rocket_lead.py` and `tests/test_weapon_name.py` green.

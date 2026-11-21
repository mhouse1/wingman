# ADR 193 — The chase steers through the flare roll

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-19 | 1.9.5           |

Supersedes ADR 164's yield (the pursuit writing neither pitch nor roll while the flare
roll is held). The roll itself (ADR 164, 184, 185) is unchanged.

## Decision

Operator, 2026-11-19, after a jet flew straight into a cliff during a flare roll: "no,
flare roll should still happen but flight control should also work at the same time, the
jet can roll while still steering towards targets".

- **D1. The pursuit no longer yields to the flare roll.** The roll holds the barrel roll
  (`;`) as before, from the alert until `flare_roll_after_s` after the last flare and the
  last sight of the warning (ADR 184). The chase keeps steering on j/l (bank and turn) and
  i/k (pitch), and the aim chain keeps working (ADR 186: the nuke can be aimed and pressed
  during a roll).
- **D2.** The dive recovery (ADR 148) and the missile evade (ADR 163) still take both axes.
  They fly a manoeuvre with those keys; the barrel roll uses only `;`.
- FR-011 now says the pursuit keeps steering while the roll is held.

## Consequences

- A LOCK WARNING that stays up no longer leaves the jet unsteered: the roll lasts as long
  as the warning (ADR 184), but the chase flies through it.
- The flight path is no longer left unchanged by the roll: the chase's turns and the roll
  combine. ADR 164 yielded to stop exactly that corkscrew; the operator chose steering.
- The chase's tracking holds are no longer released at each roll, so `yielding pitch and
  roll to the flare roll` and `flare roll over, steering resumes` are no longer logged.

## Evidence

`wingman.log`, 2026-11-19, F-106 with the nuke (the operator's `v` screenshot at 03:17:40):

```
03:17:22,199 🌀 FLARE ROLL — holding the barrel roll until 2.0s after the flares
03:17:22,244 pursue_and_engage — yielding pitch and roll to the flare roll (ADR 164)
03:17:26,197 Special weapon icon: READY (lit 16/16)
03:17:30,004 ABILITYAIM: diamond at (760,674) diamond_px=210 score=0.77
03:17:34,568 ABILITYAIM: diamond at (1284,303) diamond_px=446 score=0.85
03:17:53,997 DEATH CARD: no kill card on 3 read(s) of the respawn screen: a crash or self-inflicted
03:17:44,419 flare roll released (flares done, 22.2s)
```

- The warning was read 14 times between 03:17:22 and 03:17:44, so the roll never ended,
  and `flare_roll_max_s` (15 s) does not apply while the warning is detected (FR-011).
- The pursuit logged `rung=recovery … keys=-` on every picture of the roll: no steering.
  The nuke's diamonds were 210-620 px off the crosshair, against a 60 px press.
- Flare rolls this session: 29, most 6-9 s; two held 17.7 s and 22.2 s.

## Validation

- [x] `tests/test_flare_roll.py::test_the_chase_keeps_steering_while_the_jet_rolls`: the
      chase holds roll and pitch keys while `;` is held, and no yield is logged.
- [ ] Live: a flare roll with a target in view shows `HOLD[roll]`/`HOLD[pitch]` lines
      between `FLARE ROLL` and `flare roll released`.
- [ ] Live: per-life crash share during flare rolls, before against after.

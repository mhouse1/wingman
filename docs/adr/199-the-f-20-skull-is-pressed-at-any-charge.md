# ADR 199 — The F-20 skull is pressed at any charge

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-20 | 1.9.5           |

## Decision

Operator, 2026-11-20, after the F-20 Tigershark flew a round without pressing `q`: "for that
specific ability it should be used anytime there is a charge level".

- **D1. A new trigger, `charged`.** `q` goes out while the ring has `charge_at` or more lit
  sectors (of 16), not only on a full ring (READY, ADR 173). The presser's retry rule is
  unchanged: a charge that stays after a press is pressed again after `press_retry_s`, at
  most `max_presses` times, until the ring empties.
- **D2. The ability gets its own row.** `special_weapon.abilities.skull_shot`: the glyph (a
  flaming skull with speed lines), `trigger: charged`, `charge_at: 1`.
- **D3. The reader reports the charge.** `SpecialWeaponReader.charge()` and
  `GameStateAnalyzer.special_weapon_charge()`: the lit sectors on the last fresh read, None
  when stale. A stale read never presses.

## Consequences

- The skull goes out on the first read with any lit sector, a few seconds into each life,
  at whatever strength the game gives a low charge. That is the trade the operator chose.
- Other abilities keep their rule. An unknown glyph is still named only from a lit ring
  and pressed on READY (ADR 190).

## Evidence

`wingman.log`, 2026-11-20, `JET NAME: F-20 TIGERSHARK`, the round of 08:28:52 to 08:31.
The ring never read READY, so `q` never went out:

```
08:29:36,716 GLYPH: unknown for 43s, best nuke 0.39, ring 7/16 lit
08:29:45,687 Game state: GAME_BATTLE → GAME_BATTLE_EJECT
08:30:08,176 GLYPH: unknown for 74s, best nuke 0.39, ring 6/16 lit
08:30:22,036 Game state: GAME_BATTLE_EJECT → GAME_BATTLE
08:30:30,891 GLYPH: unknown for 97s, best refuel 0.00, ring 1/16 lit
```

- The most the ring read was 7 of 16. It restarted after each respawn, and lives lasted
  20-45 s.
- The operator's `v` screenshot at 08:30:26 shows the ring about a third lit.
- Glyph template (the saved crop `20261120_082924.286_unknown_nuke39.png`): 1.00 on the
  screenshot, 0.39 or less against every other glyph. Of 1,578 saved icon crops, the three
  at 0.5 or more are this glyph (1.00), one of them from 2026-11-11.

## Implementation

- `wingman/special_weapon.py`: `charged` in `TRIGGERS`, `SpecialWeaponReader.charge()`.
- `wingman/analyzer.py`: `special_weapon_charge()`.
- `wingman/tick_handlers.py`: the `charged` trigger reads the charge in place of READY.
- `wingman/config_schema.py`, `wingman/config.yaml`: the trigger, `charge_at`, the row.
- `test_screenshots/special_weapon/glyphs/skull_shot.png` (untracked, like the other glyph
  crops); `tests/fixtures/skull_shot/charging_083026.jpg` needs `git add -f`.
- `tests/test_skull_shot.py`.

## Validation

- [x] `tests/test_skull_shot.py`: one lit sector presses with the icon not READY, an empty
      or stale ring does not, a charge that stays is retried, not pressed every tick, and
      the screenshot names the glyph with a partial ring.
- [ ] Live, F-20: `Special weapon glyph: skull_shot`, then
      `✨ Special weapon skull_shot: trigger charged, icon charged` once a life, and the ring
      emptying after it.

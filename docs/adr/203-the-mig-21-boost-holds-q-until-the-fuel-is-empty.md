# ADR 203 — The MiG-21 boost holds q until the fuel is empty

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-24 | 1.9.5           |

## Decision

Operator, 2026-11-24, on the `v` screenshot of 17:55:10 (the MiG-21 Fishbed, a double
chevron pointing up): "activation requires holding the 'q' button, it should be held until
fuel reports 0%".

- **D1. A new trigger, `hold_to_empty`.**
  - When it starts:
    - the icon reads READY (ADR 173), and
    - a fresh afterburner fuel reading is above the row's `fuel_at` (0).
  - What it does: `q` goes down and stays down.
  - When the controller lets go, on the first of:
    - two fuel readings in a row at or under `fuel_at` (`FuelEmptyWatch`, counted per
      reading as the refuel's `_low_fuel` is, so one misread 0 does not end it);
    - `special_weapon.hold_max_s` (30 s);
    - manual takeover (SAF-001);
    - leaving battle;
    - exit;
    - the flight writers' stop.
- **D2. The ability gets its own row.** `special_weapon.abilities.boost_hold`: the glyph,
  `trigger: hold_to_empty`, `fuel_at: 0`. Any jet that draws the glyph gets the hold.
- **D3. One hold at a time.** While the hold runs, the battle tick presses nothing for it.
  The hold is listed in `Controller._flight_writers()`, so takeover and cleanup stop it.

## Consequences

- The hold starts only with fuel on the gauge, so the release can always see the fuel run
  out. With the gauge unread, nothing is pressed.
- If the icon still reads READY after a hold, it is held again once the fuel reads above 0.
  The presser's `max_presses` limit still applies.
- Wingman's own afterburner (ADR 134) draws on the same gauge. Holding `q` does not change
  when the afterburner is held.

## Evidence

`wingman.log`, 2026-11-24, `JET NAME: MIG-21 FISHBED`. The glyph was unknown, so
`unknown_trigger: ready` tapped `q` for 0.1 s. The icon stayed READY after both taps:

```
17:55:13,392 ✨ Special weapon unknown: trigger ready, icon READY
17:55:16,911 ✨ Special weapon unknown: trigger ready, icon READY
17:55:27,749 Special weapon: 2 presses and the icon still reads READY; waiting for it to change (ready trigger)
```

- Fuel OCR in battle reads about every 1.5 s (`Afterburner fuel: 81%`, `59%`, … `15%` from
  17:54:30 to 17:54:38), so two readings at 0 take about 3 s.
- Glyph template: the saved crop `20261124_175433.405_retry.png`.
  - It scores 1.00 on the screenshot.
  - Against every other glyph it scores 0.46 or less (`skull_shot` 0.46, every other glyph
    0.30 or less).
  - 44 of 2,493 saved icon crops score 0.5 or more: 14 from 2026-11-24 17:5x and 30 from
    2026-11-22 18:xx.

## Implementation

- `wingman/special_weapon.py`: `hold_to_empty` in `TRIGGERS`, `FuelEmptyWatch`.
- `wingman/controller.py`: `hold_special_weapon`, `special_hold_active`, `_run_special_hold`,
  and the writer entry.
- `wingman/tick_handlers.py`: the trigger's start condition, and `_hold_special_weapon` in
  place of the tap.
- `wingman/config_schema.py`, `wingman/config.yaml`: the trigger, `hold_max_s`, the row.
- `test_screenshots/special_weapon/glyphs/boost_hold.png` (untracked, like the other glyph
  crops). `tests/fixtures/boost_hold/ready_175510.png` needs `git add -f`.
- `tests/test_boost_hold.py`.

## Validation

- [x] `tests/test_boost_hold.py` checks:
  - READY with fuel holds `q` instead of tapping it;
  - nothing is held with the fuel empty or unread, or with the icon not READY;
  - the hold lets go on two readings at 0 (one misread does not end it), at its cap, on
    leaving battle, and on takeover;
  - the screenshot names the glyph.
- [ ] Live, MiG-21:
  - `Special weapon glyph: boost_hold`;
  - `trigger hold_to_empty`;
  - `Special weapon released (fuel 0%, N s held)`;
  - the icon leaving READY after it.

# ADR 197 — The KF-21 never presses its q ability

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-19 | 1.9.5           |

Narrows ADR 190 (an unknown special weapon is pressed whenever it is lit) for one jet.

## Decision

Operator, 2026-11-19, on a `v` screenshot of the KF-21 at 19:58:45: "in this case it should
not press 'q'".

- **D1. A jet profile can say `special_weapon: {trigger: none}`.** That jet never presses `q`.
  This comes before its glyph's row in `special_weapon.abilities` and before
  `unknown_trigger`. `resolve_profile` returns no profile for it, so neither the battle tick
  nor the aimed-ability chain presses anything.
- **D2. The KF-21 gets it**: `jet_profile.profiles.kf21.special_weapon: {ability: kf21_q,
  trigger: none}`. Its `w` auxiliary feature (ADR 183) is unchanged.

## Consequences

- The KF-21's `q` ability (a jet with four arrows) is never spent by wingman.
- Other jets whose glyph has no row are still pressed whenever lit (ADR 190).
- If the operator later gives the ability a condition, the KF-21 row takes that trigger in
  place of `none`.

## Evidence

`wingman.log`, 2026-11-19, `JET NAME: KF-21 BORAMAE`. The glyph matched no row, so ADR 190
pressed `q` on every recharge, five times in four minutes:

```
19:55:50,729 Special weapon glyph: other (best match 0.16)
19:55:50,729 ✨ Special weapon unknown: trigger ready, icon READY
19:56:49,120 ✨ Special weapon unknown: trigger ready, icon READY
19:58:01,469 ✨ Special weapon unknown: trigger ready, icon READY
19:58:42,261 ✨ Special weapon unknown: trigger ready, icon READY
19:59:43,029 ✨ Special weapon unknown: trigger ready, icon READY
```

## Validation

- [x] `tests/test_unknown_ability.py`: a jet with `trigger: none` resolves to no profile
      whatever its glyph, the shipped KF-21 row does, and the battle tick presses nothing
      for it with the icon READY.
- [ ] Live, KF-21: `Special weapon glyph: other` with no `✨ Special weapon` line after it.

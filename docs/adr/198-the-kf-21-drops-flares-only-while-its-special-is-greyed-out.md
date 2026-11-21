# ADR 198 — The KF-21 drops flares only while its special is greyed out

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-20 | 1.9.5           |

## Decision

Operator, 2026-11-20: "change it so that flares only deploy when the kf21 special is greyed
out".

- **D1. A jet profile can hold its flares while its special is lit.** With
  `jet_profile.profiles.<jet>.flares_only_when_special_grey: true`, an incoming alert drops
  no flares while the special weapon's icon reads READY (lit ring, ADR 173). Greyed out
  (counting down or in use) the three-flare burst goes out as on any jet.
- **D2. No fresh read of the icon counts as greyed out**, so a reader fault cannot leave the
  jet without flares.
- **D3. Only the flares are held.** The flare roll (FR-011, ADR 184/185), the missile evade
  (ADR 163) and the `w` drone press on the first incoming (ADR 183 D4) still answer the
  alert.
- **D4. The KF-21 gets it.** Its `q` is never pressed (ADR 197), so the icon only greys out
  when the game spends the ability itself.

## Consequences

- An alert that arrives while the icon is lit is answered without flares. A later alert,
  with the icon greyed out, gets them. An alert is not replayed when the icon greys out.
- Log line: `INCOMING MISSILE DETECTED - flares held: the special weapon is lit (ADR 198)`
  in place of `Deploying flares`. No `flare_burst_deployed` event is counted for it.
- Every other jet drops flares on every alert, as before.

## Implementation

- `wingman/config_schema.py`, `wingman/config.yaml`: the profile flag, on the KF-21.
- `wingman/controller.py`: `flares_wait_for_special_grey()`.
- `wingman/tick_handlers.py`: `deploy_flares_on_new_incoming` asks
  `_flares_held_by_special()` before the burst.
- `tests/test_flares_wait_for_special.py`.

## Validation

- [x] Tests: no flares with the icon READY, three with it greyed out or unread, any other
      jet unchanged, the shipped KF-21 profile carries the flag.
- [ ] Live, KF-21: `flares held` lines only while the icon is READY, `Deploying flares` while
      it is not.

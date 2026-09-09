# ADR 116 — The Loiter Config Block Was Never Read

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-04 | 1.8.8           |

## Context

ADR 115 lowered `loiter_mission.target_alt` from 7000 m to 5000 m on measured
evidence. The next session logged:

```
21:42:14  Controller: mission_loiter - holding to stay alive (target 7000 m)
21:42:14  Controller: mission_loiter - climbing (599 m below 7000 m hold)
```

The config on disk said 5000 and parsed as 5000. The controller flew 7000.

`Controller.__init__` read the block as:

```python
_lo = (config.get("loiter_mission", {}) or {}) if isinstance(config, dict) else {}
self._loiter_target_alt = float(_lo.get("target_alt", 7000))
```

`config` is a **`ControllerConfig` dataclass**, not a dict. The guard was always
False, `_lo` was always `{}`, and every value in the block — target altitude,
hysteresis, orbit interval and hold, lock timeout, ADR 111's boundary fractions,
ADR 112's deadband, ADR 114's level band and recovery hold — silently used its
hardcoded default.

**Nothing could reveal this until a config value diverged from its default.**
Every value in `config.yaml` matched the fallback in the code, so the system
behaved exactly as the YAML described while ignoring it completely. ADR 112's
deadband and ADR 114's bands appeared to work live because the numbers were
written identically in both places.

The one-line log message is what caught it, and only because ADR 115 changed a
number and the message happened to print it. A block with no logged values would
still be dead today.

## Decision

**D1. `loiter` becomes a pass-through block on `ControllerConfig`**, alongside
`telemetry`, `missile_evade`, `climb` and `fuel`. The pattern already existed;
loiter simply never joined it.

**D2. Read it as an attribute, not by duck-typing the config object.** The
`isinstance(config, dict)` branch expressed an ambiguity that no longer exists —
`config` is a `ControllerConfig`. A guard that silently yields defaults when it
fails is worse than no guard, because it cannot be observed.

**D3. Test the WIRING with values unlike any default.** The reason this survived
is that every test and every config value agreed with the fallback. The new
tests use 1234 m, 321 m, 77 m, 11 deg and 1.75 s — numbers no default could
produce.

**D4. Test the SHIPPED config too.** One test reads the real `config.yaml`
through the real `ControllerConfig` and asserts the target the hold would
actually fly. A wiring test proves the mechanism; this proves the deployed
value.

## Consequences

Every ADR 109-115 measurement taken before this fix was made against **default**
values, not configured ones. Where the two agreed the conclusions stand, and
they agreed for every value in the block. The exception is ADR 115, whose entire
live test flew the old 7000 m target — its evidence for *why* 5000 m is right is
unaffected, but it has never actually been flown, and its V1-V3 are still open.

Config edits to `loiter_mission` now take effect. That is a behaviour change in
itself: any operator who edited that block and saw no result was right, and the
next edit will do something.

This is a general hazard rather than a loiter one. Any block read through a
type guard that can silently fail has the same shape, and the same
invisibility as long as defaults match the YAML.

## Validation

- **V1.** `ControllerConfig.from_config` carries `loiter_mission` through as
  `.loiter`.
- **V2.** Values unlike any default reach the reader intact.
- **V3.** The shipped `config.yaml` yields ADR 115's 5000 m through the real
  `ControllerConfig`.
- **V4 — live.** A hold logs `target 5000 m`. Not yet observed.

## Follow-up, not done here

Other `isinstance(config, dict)` reads in `Controller.__init__`, if any, have
the same failure mode and would be equally invisible. That is a sweep with its
own evidence, not a change to make blind inside this ADR.

## References

- ADR 115 — the target change that exposed this, and whose live validation
  must now be redone
- ADR 112 / ADR 114 — measured against defaults that happened to match
- `wingman/controller_config.py` — the pass-through block
- `wingman/controller.py` — `Controller.__init__`
- `tests/test_mission_loiter.py` — V1-V3

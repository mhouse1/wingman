# ADR 079 — Weak-Tier Respawn Fallback Gated on Telemetry Liveness

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-17 | 1.8.4           |

## Context

The ADR 064 health respawn detector's **weak tier** infers death from a
confirmed-health-read gap: no confirmed reading for
`health.death_no_confirmed_s`, fired on the next dead→alive transition. It
already carries three suppression layers from prior false-fire hunts
(transition required, GAME_BATTLE only, ≤30 s mark staleness, OCR episode
ownership).

The 2026-08-17 sessions produced **four weak-tier fires, all false**,
that passed every layer. The 15:21 episode is the clearest:

```
15:21:35  telemetry handoff — aircraft spawned, HUD rendering
15:21:35–59  Altitude 1871 → 4677, Speed ~1000–1663 — flying, climbing
             (health OCR: no confirmed read the whole time — dropout)
15:21:59,321  Health: 50 | alive=True          ← health finally confirms
15:21:59,321  HEALTH RESPAWN FALLBACK firing (tier=weak, dead_for=24.9s)
15:22:00,605  HEALTH ALIVE — restarting mission immediately
15:22:00,633  HEALTH-FALLBACK RESPAWN accepted → RESPAWN DETECTED → mission cancelled
```

The consequences of each false fire: the freshly restarted mission is
cancelled ~30 ms later (restart churn), the spawn guard starts on a flying
aircraft, and the ADR 076 spawn-crash instrument books a phantom
sub-second "crash" (2 of the 13:05 session's counted spawn crashes were
this).

The premise that fails is "no confirmed health = dead". These sessions
show health OCR dropping out for 7–25 s **while the aircraft demonstrably
flies** — the same frames yield fresh, plausibility-accepted altitude and
speed readings the entire time. A dead aircraft renders no HUD: telemetry
goes silent within its 6 s freshness window on every real death and stays
silent until the respawn. Telemetry liveness is therefore direct
counter-evidence the weak tier currently ignores.

## Decision

**A weak death mark shall not form while telemetry is live.** At mark time
(`_shadow_mark_weak`, when the confirmed-read gap crosses its threshold),
the analyzer checks its own telemetry snapshot; if `altitude_fresh()` —
a plausibility-accepted altitude sample within the 6 s staleness window —
the mark is suppressed and logged at DEBUG: the HUD is rendering, so the
gap is an OCR dropout, not a death.

Mark time, not fire time, is deliberate: at fire time (the alive edge)
telemetry is fresh for **real** respawns too — the new life's HUD renders
~1.5–2 s before health confirms (the ADR 078 measurement). During a real
death, however, the mark forms mid-respawn-screen where telemetry has been
stale for seconds. Mark time is where the two cases separate.

The strong tier is untouched: an observed sub-1 health read followed by
digit loss is intrinsic evidence (ADR 061/064) and needs no corroboration.

Accepted residual risk: if telemetry OCR *also* hallucinates a fresh
accepted read during a real death's mark window, the weak mark is
suppressed and that episode relies on respawn overlay OCR (~92% recall)
or the strong tier. Four false fires in one day against zero genuine weak
catches makes that trade one-sided.

## Consequences

- The false-fire class (health dropout mid-flight) is structurally
  removed: no mark forms, so no alive edge can fire it.
- Mission restart churn from phantom respawns stops, and the ADR 076
  spawn-crash instrument loses its known false-positive source.
- The dual-mode fallback keeps its genuine catches: real deaths silence
  telemetry, so their marks form exactly as before.
- No config changes; the gate rides the existing telemetry staleness
  window (`stale_after_s`, 6 s).

## Verification

- Unit tests (`test_health_respawn.py`): weak mark suppressed when the
  telemetry snapshot is fresh; weak tier fires exactly as before when
  telemetry is stale/absent; strong tier unaffected by telemetry state.
- `make test` green.
- Live validation (2026-08-17 15:33 session, 43 min, 18 respawns): the
  gate suppressed **27 weak-mark attempts** during mid-flight health OCR
  dropouts (first at 15:39, ~6 min into the session) with **zero
  weak-tier fires** and zero phantom respawns — spawn-crash count 0, no
  mission restart churn. Every respawn was handled by overlay OCR.

## References

- ADR 064 — dual respawn detection (weak/strong tiers, prior suppression
  layers)
- ADR 061 — observed-death provenance (strong-tier evidence)
- ADR 078 — telemetry-freshness-as-liveness precedent (spawn guard
  handoff; HUD leads health confirm by ~1.5–2 s)
- 2026-08-17 13:05 and 15:02 session logs — four false weak fires, the
  15:21 episode timeline above

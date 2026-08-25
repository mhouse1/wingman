# Performance Diagnostic 001 — Why These Runs Are Excluded

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Active | 2026-08-23 | 1.8.5           |

Runs in this directory were recorded with a diagnostic enabled and **must not be
aggregated into the period or release baseline**. `_aggregate_folder` only reads
`current/` and `release/`, so keeping them here is what excludes them — do not
move them back.

## run_20260823_105523 — heap census run (ADR 091)

The session that attributed the Performance 008 leak to a per-key-event X11
connection. Recorded with `heap_census.enabled: true`, so tracemalloc was
tracing every allocation and each census blocked the tick.

Measured against the median of the 86 other runs in the period:

| crop | this run | period median | |
|------|----------|---------------|---|
| incoming | 0.429 | 0.287 | +49% |
| respawn | 0.403 | 0.244 | +65% |
| health | 0.410 | 0.265 | +55% |
| ammo_flares | 0.361 | 0.210 | +72% |
| ammo_missiles | 0.337 | 0.194 | +74% |
| telemetry | 0.575 | 0.374 | +54% |
| reaction | 0.762 | 0.282 | **+170%** |

That is instrument overhead, not a regression. The gameplay outcome in the same
session was normal — 17 missions, 100% click-to finish, 0 spawn crashes, 88%
missile-evade survival — which is the point: the timings are polluted, the
behaviour was not.

Kept rather than deleted because this is the run that solved Performance 008.

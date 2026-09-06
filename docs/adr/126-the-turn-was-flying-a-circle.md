# ADR 126 — The Turn Was Flying a Circle

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft — decision REVERTED | 2026-09-05 | 1.8.8 |

## Context

ADR 125 added a heading proxy so the question ADRs 101, 107, 120 and 122 could
not answer would finally have data behind it. A 6h16m soak — 64 missions, 182
respawns, 169 turns — answered it in one pass.

**The turn turns.** Over the 76 turns with three or more bearing samples:

| | |
|---|---:|
| median NET heading change | 136 degrees |
| median PATH (total rotation) | **294 degrees** |
| median range gain | **+0.000R** |

And the proxy is measuring the aircraft, not detector noise. Bearing sign tracks
the commanded roll:

| commanded roll | n | median net bearing | net > 0 |
|---|---:|---:|---:|
| left | 33 | **+91 deg** | 67% |
| right | 41 | **-32 deg** | 41% |

Noise would give roughly 50% either way and medians near zero for both.

So every previous hypothesis was wrong in the same direction. The keys arrive.
The aircraft rotates — hard, about 24 degrees per second. The range still does
not improve. And the reason is in the numbers themselves:

**294 degrees is very nearly a full circle, and a circle returns the aircraft to
where it started.** The turn was not failing to turn; it was turning too far.

The cycle it produced is visible too. Turns ran to their 12 s cap and
re-triggered almost immediately:

| | |
|---|---:|
| turns started | 169 |
| median gap to the next turn | **14 s** (cap is 12 s) |
| next turn within 20 s | 64% |
| EngageNav steering between turns | 0.45 per tick |

Twelve seconds of turning, two seconds of Engage steering back toward the edge,
repeat. The aircraft was held in a wagon-wheel at the boundary.

## Decision

**D1. Cap the turn at 5 s, down from 12 s.** At the measured ~24 deg/s that is
roughly 120 degrees — enough to break away from the edge, not enough to come
back to it.

**D2. Cap by TIME, not by measured rotation.** The bearing proxy is good enough
to diagnose with and, at 56% detector readability, not good enough to steer on.
ADR 125 D4 said nothing would steer on it, and that still holds: this uses the
proxy to choose a constant, which is exactly what instrumentation is for.

**D3. Keep everything else.** ADR 122's direction choice, ADR 120's release rule
and the entry gate are unchanged. This is one variable, and the soak that
follows has to be attributable to it.

## Consequences

If the circle explanation is right, range gain should become clearly positive
and the 14 s re-trigger cycle should lengthen. If it is wrong — if the turn
needed those 12 seconds — gain will fall and turns will re-trigger sooner. Both
outcomes are readable in the same two measurements, which is the point of
changing one thing.

A 5 s turn may be too short to clear the edge in one attempt, so the tactic may
now fire more often for less each time. That is acceptable if the aircraft ends
up further out; it is the failure mode to watch if it does not.

**This does not touch the upstream cause.** Engage still steers toward long-ring
contacts at the arena edge — 0.45 steering commands per tick between turns —
and ADR 107 named that on 2026-09-03. A turn that works only means the aircraft
escapes something it should not have been flying into.

## First numbers (2026-09-05 17:42-17:56, n=8 — preliminary)

| | 12 s cap (64-mission soak) | 5 s cap |
|---|---:|---:|
| median PATH rotation | 294 deg | **164 deg** |
| median abs NET heading | 136 deg | **140 deg** |
| median range gain | +0.000R | +0.010R |
| receded | ~85% | 7 of 8 |
| median gap to the next turn | 14 s | **6 s** |

**The mechanism moved as predicted.** Path rotation nearly halved while net
heading was unchanged — the wasted counter-rotation is gone and the useful part
survived, which is what cutting a circle short should look like.

**And the named risk appeared with it.** Turns re-trigger in 6 s rather than 14,
and fired 26 times in 14 minutes against 169 in 6h16m — roughly four times the
rate. Whether that is "fires more often for less each time" (the failure mode
this ADR named) or simply an aircraft that spent this session closer to the
edge cannot be told from two missions.

**Eight turns and two missions decide nothing.** The baseline it must beat is
0.094 crossings per mission over 64 missions, and this session produced zero
crossings over two — which is not evidence of anything. V2 is met directionally;
V3, V4 and V5 need a soak of comparable length.

## Result: the decision was wrong, and reverted

A 2h28m soak (25 missions, 167 turns, 43 with bearing) tested D1 against the
64-mission baseline:

| | 12 s cap (n=76) | 5 s cap (n=43) | |
|---|---:|---:|---|
| median PATH rotation | 294 deg | **320 deg** | V2 **failed** |
| median abs NET heading | 136 deg | 127 deg | |
| median range gain | +0.000R | **+0.040R** | V3 met |
| receded | 85% | 86% | |
| median gap to next turn | 14 s | **6 s** | V4 **failed** |
| turns per mission | 2.6 | **6.7** | |
| crossings per mission | **0.094** (64 missions) | **0.160** (25) | V5 **failed** |

**Capping the actuator does not cap the manoeuvre.** 167 actuator starts landed
inside 47 selection episodes — **3.6 restarts per episode** — with episodes
running a median of 16 s and up to 58 s. The CONDITION decides how long the
aircraft turns; the actuator cap only decides how often the keys are re-pressed
inside that. Shortening it produced more, shorter actuations totalling *more*
rotation, not less.

So D1 aimed at the wrong lever. The premise — that ~294 degrees of rotation is
a circle that returns the aircraft to where it started — is untouched by this
and still the best explanation for +0.000R. What is disproved is that
`boundary_turn_max_s` can do anything about it.

Range gain did improve (+0.000R to +0.040R), and that is the one result worth
keeping in mind. It is not enough to hold the change: crossings per mission,
the actual objective, went the wrong way, and turn frequency multiplied by 2.6.
Reverted to 12 s.

**The next attempt belongs in the release condition** — end the turn once the
aircraft has rotated far enough, rather than when the range ticks up or a timer
expires. ADR 125's bearing is the measurement for it, though at 56% detector
readability it is not yet trustworthy enough to steer on, which is the problem
to solve first.

## Validation

- **V1.** The shipped cap is short enough not to approach a full circle, and
  long enough not to be the inert roll ADR 101 measured.
- **V2 — live. FAILED.** Median path rotation rose to 320 degrees.
- **V3 — live. MET.** Median range gain rose to +0.040R over 43 turns.
- **V4 — live. FAILED.** The gap shortened to 6 s.
- **V5 — the metric. FAILED.** Crossings per mission rose to 0.160 over 25
  missions, against 0.094 over 64. Fewer missions, so not decisive on its own —
  but taken with V2 and V4, which are large mechanical effects rather than
  noise, the decision does not survive.

## References

- ADR 125 — the bearing instrumentation that made this visible
- ADR 107 — the +0.00R result and the long-ring pursuit still unfixed
- ADR 101 — the inert roll, which is why D1 has a floor as well as a ceiling
- ADR 122 — the direction choice, unchanged here
- ADR 106 — the 0.094 baseline this must beat
- `wingman/config.yaml` — `behavior_tree.climb.boundary_turn_max_s`
- `tests/test_minimap_bearing.py` — V1

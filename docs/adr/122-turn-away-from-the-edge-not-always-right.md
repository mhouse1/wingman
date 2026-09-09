# ADR 122 — Turn Away From the Edge, Not Always Right

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

ADR 107 measured `BoundaryTurn` as break-even: a median range gain of **+0.00R
over 61 turns**. Four sessions of tuning since have not moved ADR 106's crossing
rate out of its 0.10-0.34 band.

Three candidate explanations were tested against 91 crossings, and two were
eliminated.

**Detection is not the bottleneck.** Taking the full entry condition
(`dist <= turn_frac`, `forward > 0`, `forward >= entry_ratio * dist`) over the
30 s before each crossing:

| | |
|---|---:|
| crossings that never satisfied the entry condition | 1% |
| **median warning before the crossing** | **18.4 s** |
| under 10 s of warning | 33% |
| under 5 s | 16% |

In two thirds of crossings the turn could have armed ten seconds or more ahead.

**Engage is not fighting the turn.** A second `_actuate_engage` caller is gated
on `survival_hold` but not on the selected tactic, which looked like a live
competitor for the roll axis. Counted: **0** EngageNav steering commands on the
131 ticks with BoundaryTurn selected, against 0.42 per tick otherwise.

So the turn arms early and flies uncontested, and the aircraft still crosses.
That leaves the manoeuvre itself — and it always rolls **right**:

```python
self._climb_key(ROLL_RIGHT_KEY, press=True, action="boundary")
```

The docstring defends this:

> Direction is arbitrary and that is safe... the condition closes on the
> MEASURED range, so a turn the wrong way releases as soon as the aircraft
> starts receding. Picking wrong costs seconds, not the aircraft.

**That argument fails in its own terms.** A turn the wrong way does not recede —
it closes *faster*. The release it relies on is precisely the condition it
cannot meet, so a wrong-way turn holds while flying into the edge. At
600-1400 KPH, those "seconds" are hundreds of metres.

And a coin flip between "away" and "into" is exactly what a median gain of
+0.00R looks like.

The information needed to choose was already being computed and thrown away.
`detect_map_boundary` derived `dx` for the nearest boundary pixel and returned
only the range and the forward component:

```python
dx = xs - cx
dy = ys - cy
...
return (float(dist[i] / radius), float(-dy[i] / radius))   # dx discarded
```

## Decision

**D1. The reading carries a lateral component.** `detect_map_boundary` returns
`(dist, forward, lateral)`, lateral positive to the RIGHT of the nose. It is one
array index that was already in hand.

**D2. The turn rolls away from the side the edge is on.** Positive lateral rolls
LEFT; negative rolls RIGHT.

**D3. Unknown lateral keeps the old fixed right roll.** A stub, an older
recording or a 2-tuple from anywhere else behaves exactly as before rather than
failing.

**D4. The direction is read at the tick that selected the turn**, from the same
reading the decision was made on, not re-derived later from a reading that may
have moved.

## Consequences

Roughly half of all turns should now be in the opposite direction to before. If
the coin-flip explanation is right, ADR 107's median range gain should become
clearly positive; if it does not move, the manoeuvre itself does not turn the
aircraft, and that is a different ADR with the attitude sampler already in place
to answer it.

The turn can still pick wrong when the boundary is nearly dead ahead
(`lateral` near zero), where the choice is close to arbitrary and the aircraft
must turn through 90 degrees either way. Nothing here helps that case.

Any consumer unpacking a 2-tuple would break; the tick path tolerates both
lengths, and the only production consumer is the tick path.

## First live signal (2026-09-05, n=8)

Both directions were exercised within minutes — `rolling left (lateral=+0.218)`
and `rolling right (lateral=-0.331)` — so V2 is met live.

Range gain over the 8 turns that completed in that session:

| | ADR 107 baseline | this session |
|---|---:|---:|
| completed turns | 61 | 8 |
| median range gain | +0.00R | **+0.060R** |
| gained / lost | 13 / 15 | 5 / 1 |

The first non-zero median the turn has produced. **Eight turns is a hint, not a
result** — this project has produced 0.00, 0.10, 0.26 and 0.34 crossings per
mission on unchanged code, and a session that flatters a change just made
deserves more suspicion, not less. V4 stands until ~40 turns say the same.

**A caveat visible already.** The lateral values at turn start cluster small:
`+0.218, -0.331, -0.003, -0.041, -0.117, +0.054, -0.009, +0.136`. Half are under
0.10 — the near-dead-ahead case this ADR's Consequences names, where the choice
is close to arbitrary and the aircraft must turn through 90 degrees either way.
If that distribution holds, the ceiling on this fix is lower than the coin-flip
theory implies, and what remains is the turn's EFFECTIVENESS rather than its
direction.

Direction split was 13 right to 4 left, which is not what a uniform distribution
of approach geometry would give. Too few to interpret, and worth re-checking at
volume: a systematic bias would suggest the lateral sign is measuring something
other than what it claims.

## Validation

- **V1.** A boundary line right of centre reads positive lateral; the same line
  left of centre reads negative.
- **V2.** With the edge on the right the turn presses ROLL_LEFT; on the left,
  ROLL_RIGHT.
- **V3.** With lateral unknown the turn keeps its previous fixed direction.
- **V4 — live.** ADR 107's range-gain measurement over a comparable number of
  turns is positive rather than +0.00R. Not yet observed.
- **V5 — the metric.** ADR 106's crossings per mission falls. Needs the usual
  40 missions before it means anything.

## Also fixed, found on the way

The first version of V2's test re-derived the direction from `lateral` inside
the test and asserted its own arithmetic — it would have passed against the
unmodified code. It now drives the real `boundary_turn_mode` and reads the key
actually pressed. The stub it needs was then built by enumerating what the turn
thread touches, rather than discovering it one `AttributeError` at a time, and
the test runs with thread exceptions promoted to errors — an earlier run passed
while its own thread was raising, because the assertions were read before the
crash.

## References

- ADR 107 — the +0.00R measurement this explains, and its V9
- ADR 108 — `detect_map_boundary`, which computed `dx` and discarded it
- ADR 106 — the crossing rate this is meant to move
- ADR 120 — the release rule, changed the same day
- `wingman/analyzer.py` — `detect_map_boundary`
- `wingman/controller.py` — `boundary_turn_mode`
- `tests/test_minimap_bearing.py` — V1-V3

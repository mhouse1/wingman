# ADR 120 — Release the Turn on the Nearest Reading

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-05 | 1.8.8           |

## Context

ADR 113 median-filtered the boundary reading and fed the filtered value to
every consumer. Watching a crossing on 2026-09-05 showed what that costs at the
moment it matters:

```
07:48:32  BOUNDARY TURN — banking and pulling away from the edge
07:48:35  median-of-3 0.045 -> 0.119
07:48:37  CROSSED
```

Measured across the session, the filter is close to even overall — it reported
farther than raw on 45% of corrections and nearer on 54%. **The bias appears
only where it is dangerous.** Of the 39 raw readings inside 0.10R, the filter
reported 0.10R or more for **17 of them (44%)**, including:

```
raw 0.019 -> filtered 0.516
raw 0.051 -> filtered 0.395
raw 0.047 -> filtered 0.312
```

And the release path acts on a single reading:

```python
if dist >= (clear_at if state["active"] else turn_frac):
    _reset(); return False
```

So one filtered value above `release_frac` ends the turn — with the aircraft, in
those cases, at the edge.

The obvious objection is that a jump from ~0.6R to 0.019R in one tick is
physically impossible, so those near readings are noise and the filter is doing
its job. **Tested, and it is not so.** Of 67 raw readings inside 0.10R that had
a fresh predecessor:

| | count | share |
|---|---:|---:|
| reachable jump (<=0.20R per tick) — a real approach | 58 | **87%** |
| impossible jump — detector noise | 9 | 13% |

Near readings are overwhelmingly real. The filter is discarding true proximity
roughly six times more often than it rejects noise.

## Decision

**D1. Enter on the filtered reading; release on the nearest one.** The snapshot
carries `boundary_near`, the minimum still inside the median window.

**D2. Because the two errors do not cost the same.** A spurious near reading
buys one unnecessary turn, and turns are cheap — 83 in a session, and ADR 107
measured them as break-even, so an extra one costs almost nothing. A missed near
reading buys a crossing, which is the metric being optimised. Noise-reject where
being wrong is cheap; be conservative where it is not.

**D3. Recession reads the nearest value too.** Recession is a clearance claim in
disguise. Judged on the filtered value it can manufacture a recession the
aircraft never flew, which is the same defect wearing a different threshold.

**D4. `min_dist` tracks the nearest reading**, so the recede comparison is
against how close the aircraft actually came rather than how close the filter
admitted it came.

**D5. Absent `boundary_near` falls back to `boundary_dist`.** Any caller that
does not supply it behaves exactly as before.

## Consequences

Turns will last longer, and some will hold on a near reading that was noise —
13% of them, by the measurement above. That is the trade D2 accepts on purpose.

The turn is now harder to release, which moves it closer to a latch. The
existing guards still bound it: the blind-tick counter, the respawn reset, and
the duration cap. If turns start running to their cap routinely, that is the
signal this went too far, and the cap is where it will show.

This does not touch entry, so the false-trigger rate ADR 113 improved is
unchanged.

It also does not make the turn effective. ADR 107 measured a median range gain
of +0.00R over 61 turns; a turn that holds longer is still that turn. This
removes a way of ending it early, nothing more.

## Validation

- **V1.** A filtered reading above `release_frac` does not release the turn
  while a near reading stands in the window.
- **V2.** When every recent reading is far, the turn still releases — the guard
  must not become a latch.
- **V3.** Entry still uses the filtered reading, so a spurious near value does
  not start a turn.
- **V4.** Recession is judged on the nearest reading.
- **V5.** A snapshot without `boundary_near` behaves exactly as before.
- **V6 — live.** Turns no longer end with the aircraft inside 0.10R. Not yet
  observed.

## Also fixed, found on the way

Four tests in `test_nested_display.py` patched `display_is_up`, which ADR 119's
`start()` no longer calls. With the patch no longer intercepting, the **real**
`probe_display` ran against the machine's actual `:3` — and passed or failed
according to whether a session happened to be running. They now patch
`probe_display`. A test that reaches the live environment is not a unit test; it
is a coin toss that usually lands the same way.

## References

- ADR 113 — the median filter, whose output this stops trusting for release
- ADR 107 — BoundaryTurn, its break-even measurement and its duration cap
- ADR 106 — crossings per mission, the metric D2 optimises for
- `wingman/behavior_tree.py` — `make_boundary_condition`, `AnalyzerSnapshot`
- `wingman/tick_handlers.py` — where `boundary_near` is computed
- `tests/test_behavior_tree.py` — V1-V5

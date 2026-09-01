# ADR 101 — Boundary-Aware Climb

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-01 | 1.8.8           |

## Context

[Design 010](../hldd/010-mini-map-detection/010-mini-map-detection-hldd.md)
(HLDD 010, *Map Boundary Guard*) added map-boundary instrumentation and said so
in its scope: measurement only, no guard. The title named a guard; the document
deliberately built only the measurement that would justify one. `detect_map_boundary` returns
`(distance, forward)` in minimap radii and nothing consumes it — the word
"boundary" does not appear in `behavior_tree.py` outside an unrelated docstring.

On 2026-09-01 that instrumentation earned its keep. A crossing was confirmed by
OCR reading the banner, and the trace buffer captured the twenty ticks before
it:

| t (s) | dist (R) | fwd | tactic |
|------:|---------:|----:|--------|
| 5009.0 | 0.54 | +0.454 | Climb |
| 5015.0 | 0.54 | +0.454 | Climb |
| 5021.0 | 0.47 | +0.397 | Climb |
| 5025.5 | 0.32 | +0.265 | Climb |
| 5027.0 | 0.21 | −0.196 | Climb |
| 5030.0 | 0.03 | −0.032 | Climb |

**Climb owned the aircraft for the entire 28.5 s**, nose held near +20°, and
flew it through the edge. `forward` stayed positive for 22 s of monotonic
approach. Nothing interrupted, because nothing could.

`EngageNav: mode regroup → idle` fires at the instant of the crossing, not
before. ADR 028's Regroup steers toward friendlies — usually inward — but Climb
sits above it in the selector, so Regroup never held the aircraft.

### Two facts that shape the fix

**`climb_mode` holds `NOSE_UP` and `AFTERBURNER`. It never touches roll.** The
roll axis was free for all 22 seconds.

**Aborting a climb is dangerous.** SAF-010 and ADR 086 exist because a climb
released at +73° coasted 1500 m, stalled at 24 KPH, and hit the ground with
missiles still racked. Trading a nine-second excursion for that is a bad trade.

Together these say the obvious fix — drop the Climb selection when the boundary
is near — is the wrong one, and an unnecessary one.

## Decision

**D1. Turn without leaving the climb.** While the boundary is ahead and inside
`boundary_turn_frac`, a running climb holds `ROLL_RIGHT`. Pitch and thrust are
untouched: the climb keeps its altitude mandate and only the heading moves
underneath it. No tactic priority changes, and the SAF-010 exit path is not
involved.

**D2. Close the loop on the measured forward component.** The reading is
`(distance, forward)` with no lateral term, so which way to roll cannot be
derived from it. Any consistent roll drives the boundary off the nose, and the
turn releases as soon as `forward` goes negative — so choosing the wrong
direction costs seconds, not the aircraft. This is why an arbitrary fixed
direction is acceptable rather than a limitation to be fixed later.

**D3. Turn earlier than the approach log.** `boundary_turn_frac` is 0.50,
deliberately larger than `boundary_near_frac` (0.35). The measured crossing was
4.5 s after 0.32R — no time to turn — and still at 0.55R twenty seconds out.
Reusing the approach threshold would ask for a turn too late to make.
Setting `boundary_turn_frac` to 0 disables the turn.

**D4. The request is an intent with an expiry, not a latch.**
`Controller.set_boundary_turn(active, ttl_s)` records a deadline. The
instrumentation re-asserts it every tick while the edge is ahead; one missed
update ends the turn. A plain boolean would strand a held roll key if the
instrumentation threw or the tick stalled — a worse failure than the crossing
it prevents. Thresholds stay with the caller, which already reads the minimap
config; the controller only honours the intent.

**D5. Bound the turn, and re-arm the bound.** After `boundary_turn_max_s` (8 s)
still inside the band with the edge ahead, the roll is released and logged at
WARNING: either it is not taking effect or the aircraft is pinned, and holding
the key fixes neither while hiding the failure. The budget re-arms when the
aircraft clears the band — **not** on the next approach, or a single pinned
approach would disable the turn for the rest of the session.

**D6. Bracket the roll key for the press, never for the climb.** `ROLL_RIGHT` is
a watched maneuver key. Wingman's own press must not echo back through XRecord
and read as an operator takeover — but bracketing it for the climb's full
duration silently kills manual takeover on `l`, which is a SAF-001 violation.
Caught by `test_takeover_handler_stops_running_climb` during implementation.
The bracket now spans only the held press, at most the turn budget.

Be precise about what that leaves, because the obvious claim is wrong: a climb
ALREADY brackets `i` and `k` — they are the keys it holds — so those were never
live takeover keys during a climb, before this ADR or after. What ADR 101 costs
is `l`, for at most the turn budget. `j`, Enter and the arrow keys stay live
throughout.

**D7. Only the climb honours the intent.** Engage and AttackSupport already
write steering of their own; a second writer on the same axis would fight them.
This ADR does not give them boundary awareness.

**D8. Count crossings only after OCR confirms.** Separate from the turn, and
from the same session: the colour trigger incremented the count and logged a
WARNING immediately, then retracted on the OCR verdict. Measured 2026-09-01, 6
triggers in 84 minutes and 1 confirmed — five WARNING/retraction pairs that each
read as a real excursion. Because the count was decremented back to zero every
time, all five announced themselves as "crossing 1 this session". The count and
the warning now live with the arbiter; the trigger logs at DEBUG.

## Consequences

A climb that would have flown out of the arena now turns, and keeps climbing
while it does. The excursion the trace recorded had ~22 s of usable warning, so
the intervention has room even at a 1.5 s instrumentation tick against a 0.25 s
climb tick.

Wingman gains a second writer on the roll axis, active only during a climb and
only while the edge is ahead. During that window — bounded at 8 s — an operator
press of `l` is read as wingman's own echo and does not trigger takeover. `j`,
Enter and the arrows remain live; `i` and `k` are unavailable for the duration
of any climb, which is prior behaviour this ADR neither causes nor fixes.

The crossing figure changes meaning: it now counts confirmed crossings only, so
it is not comparable with figures logged before this ADR. The earlier argument
against building a guard rested on a rate of 0.043 crossings per mission, which
was mostly unconfirmed triggers.

Nothing about tactic priority, the climb's altitude band, or the SAF-010 exit
path changes.

## Alternatives considered

**Drop the Climb selection near the boundary.** The obvious reading of "make
Climb boundary-aware", and rejected on SAF-010: an abrupt climb release is the
documented cause of a ballistic stall and a crash. It also solves less — the
next tactic inherits a nose-high aircraft still pointed at the edge.

**A dedicated boundary tactic above Climb.** Cleaner in the tree, but it would
have to own pitch as well as roll, which puts it back into the climb-abort
hazard, and it would need a priority argued against ADR 070 and ADR 073. The
roll axis was free; nothing needed to be re-ranked.

**Derive the turn direction from the minimap.** Correct in principle and not
needed: closing the loop on `forward` makes an arbitrary direction
self-correcting. Worth revisiting only if traces show turns routinely running to
the budget.

## Validation

- **V1.** A climb with the boundary ahead inside `boundary_turn_frac` holds
  `ROLL_RIGHT`, and pitch and afterburner handling are unchanged.
- **V2.** The turn releases within one tick of `forward` going negative.
- **V3.** A boundary behind the nose never requests a turn, at any distance.
- **V4.** The turn releases at the budget, and re-arms after the aircraft
  clears the band.
- **V5.** A `None` reading stops the request rather than holding it, and the
  intent expires on its own if the caller stops updating.
- **V6.** `boundary_turn_frac: 0` disables the turn entirely.
- **V7.** An operator press of `l` during a climb with no turn in progress still
  triggers manual takeover, and `j` triggers it even mid-turn.
- **V8.** The programmatic bracket is balanced on every climb exit path,
  including one that ends mid-turn.
- **V9.** An unconfirmed colour trigger produces no WARNING and leaves the
  crossing count untouched.
- **V10 — live.** A session with a confirmed approach shows the turn requested,
  `forward` going negative, and no crossing. Not yet observed; this ADR is
  Draft until it is.

## References

- [Design 010](../hldd/010-mini-map-detection/010-mini-map-detection-hldd.md)
  — HLDD 010, *Map Boundary Guard*: the instrumentation this builds on, its
  measurement-only scope, and the guard its title anticipated. This ADR is that
  guard, in the narrow form the trace justified.
- ADR 028 — Regroup; steers inward but sits below Climb, which is why it did not
  help here
- ADR 073 — the climb tactic and its altitude band
- ADR 086 / SAF-010 — why a climb must not be released abruptly
- ADR 098 — the programmatic bracket and XRecord echo handling behind D6
- SAF-001 — manual takeover, the requirement D6 protects

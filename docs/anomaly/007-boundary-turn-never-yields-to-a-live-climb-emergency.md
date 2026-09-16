# Anomaly 007 — BoundaryTurn Never Yields to a Live Climb Emergency

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-14 | 1.8.9           |

## Summary

**Status: fixed AND live-validated the same day it was found.** A live near-miss: the aircraft
entered a fast, sustained dive (predicted time-to-ground of 6-8 seconds,
well inside the 30-second emergency threshold) while `BoundaryTurn` was
selected for banking away from a map edge. `BoundaryTurn` is documented to
yield to `Climb`'s emergency band in exactly this situation (ADR 107 D4),
but did not — it stayed selected for 9+ continuous seconds while altitude
fell from ~3843m to ~1344m (and the last raw telemetry read, moments later,
showed 470m and still descending). No `DIVE RECOVERY` warning ever fired.
The operator caught it and pressed Backspace manually.

**Root cause**: `BoundaryTurn`'s yield check reads `ClimbCondition
.emergency_active`, a flag only computed as a side effect of py-trees
actually ticking Climb's own condition. py-trees' priority Selector never
ticks a leaf a higher-priority sibling keeps beating — so while
`BoundaryTurn` kept winning, `Climb`'s condition was never invoked at all,
and `emergency_active` sat frozen at whatever it was *before* `BoundaryTurn`
took over. The yield mechanism was checking permanently stale data by
construction, for the entire duration of any `BoundaryTurn` selection.

**Fix**: split the ttg/emergency computation out of `ClimbCondition
.__call__` into a new `update_emergency(snapshot, now)`, called
unconditionally every tick from `BehaviorTreeHandler.tick()` — *before*
`tree.tick()` — regardless of what wins selection. Same "perceive before
select" pattern `BoundaryPerceptionHandler` already uses for boundary
readings. Implemented, tested (including a real end-to-end tree
reproduction of the exact incident), gate green.

**Live-validated 2026-09-14 into 2026-09-15, two sessions, ~9.5 hours of
flight time combined.** First session (~1 hour, immediately after the fix
shipped): 147 `DIVE RECOVERY` firings, 4 while `BoundaryTurn` was selected;
every one yielded within 1-2 ticks, twice in the exact same tick
(20:04:32.755→756, 20:33:15.438→439, sub-millisecond apart).

**Second session (5h48m, the same night, re-examined after an initial
overclaim — see below): 934 `DIVE RECOVERY` firings, 40 while `BoundaryTurn`
was selected. Zero recurrence of the original bug** (no case of a genuine,
sustained emergency going unanswered) **— but resolution time is not
always same-tick.** One case (01:41:19-25) took **~6 seconds**: `ttg`
dropped to 9s (inside the 15s instant-bypass, so `emergency_active` was set
True immediately, confirmed by the `DIVE RECOVERY` warning firing on the
very first reading), yet `BoundaryTurn` kept reporting itself selected for
3 more ticks before releasing. Root cause of *that* delay: `MinimumHold`
(`hold_s: 3.0` in production config) is a **separate, pre-existing,
intentional** anti-flapping mechanism — it keeps a decorated leaf's status
as RUNNING for up to `hold_s` after the underlying condition already wants
to fail, the same way it protects Disengage from chattering (ADR 107 D6).
This fix makes the underlying condition correctly *want* to yield the
instant an emergency is real; it does not, and was never scoped to, shorten
`MinimumHold`'s own bounded grace period on top of that. Practical effect:
resolution time across all 40 real occurrences ranged from the same tick up
to ~6 seconds — categorically different from the original bug (indefinite,
9+ seconds and still counting when the operator intervened, never
resolving on its own) but not literally instant in every case. The initial
report of "every case within 1-2 ticks" was accurate for the smaller
first-session sample but did not generalize — corrected here rather than
left standing, per this project's own convention.

**Third session (2026-09-15/16, 14h01m, `wingman_20260915_164836.log`,
found while reviewing this same log for an unrelated request): 2850 `DIVE
RECOVERY` firings, 73 while `BoundaryTurn` was selected.** Same measurement
discipline applied: the two apparent outliers exceeding 10s (max 21.0s) were
checked directly against the raw `ttg` trend, not trusted from the duration
proxy alone — both were the same false-alarm shape already documented above
(a single early warning that self-resolved within 1-2 ticks on its own,
`BoundaryTurn` then correctly continuing its own unrelated turn afterward,
not a stuck emergency). Median resolution 3.0s; the genuine worst case
remains the ~6s from the second session — this third session added no new
one. **Running total: n=117 across three sessions, zero recurrence of the
original bug in any of them.** See Disposition item 3.

## The incident

Session `wingman.log` (live at the time), `make rd v`, no account suffix —
launched per the operator's "attach" preference.

| time | event |
|---|---|
| 18:49:16.751 | `RespawnWait → Regroup` — fresh respawn, altitude 1100m. |
| 18:49:19.778 | `Engage → Climb` (ordinary, target alt 5000). |
| 18:49:22.766 | `Climb → BoundaryTurn` — first boundary approach (0.02R), turn guard active. |
| 18:49:28.756 | `climb complete (stopped, 9.0s)`. |
| 18:49:36.266 | `BoundaryTurn → Climb` — first turn resolved (range receded 0.02R → 0.66R). `alt=3472, alt_rate=+238m/s, ttg=n/a` — healthy climb. |
| 18:49:37.766 | `Climb → BoundaryTurn` again — second approach (0.12R) interrupts the climb almost immediately. `alt=3843, alt_rate=-162m/s, ttg=24s` — already descending, not yet in the 30s emergency band. |
| 18:49:38.272 | `climb exit — nose at -20deg (band +20) after 1 pulse(s)` — Climb's own actuator gives up; nose was already past its allowed band. |
| **18:49:40.760** | **`BoundaryTurn` selected. `alt=3472, alt_rate=-446m/s, ttg=8s`** — inside the 30s emergency window. No yield. |
| 18:49:43.772 | `BoundaryTurn` selected. `alt=2439, alt_rate=-424m/s, ttg=6s`. No yield. |
| 18:49:46.757 | `BoundaryTurn` selected. `alt=1344, alt_rate=-225m/s, ttg=6s`. No yield. |
| 18:49:49.770 | `BoundaryTurn` **still** selected. `alt=1344, ttg=6s` — same reading (telemetry not updating between ticks at this point). |
| 18:49:46.750 (INFO altitude line, not a BT snapshot) | `Altitude: 470 | Speed: 1804 | Nose: -27° (dive)` — the last raw reading before shutdown. |
| **18:49:50.259** | **Operator presses Backspace**, ending wingman. |
| 18:49:50.271 | `boundary turn complete (11.0s) — nose -48..-20deg (swing 28), speed 1687..2180, alt 1344..3843, n=4` — BoundaryTurn's own completion log, reporting 11 seconds of continuous selection through the entire dive, ending essentially simultaneously with the operator's Backspace rather than on its own. |

`ttg` (time-to-ground) was inside the 30-second emergency threshold for **at
least 9 continuous seconds** (40.760 through 49.770), never rising back
above it, with `BoundaryTurn` selected throughout. `Climb`'s own emergency
band never fired — no `BT: DIVE RECOVERY` line appears anywhere in this
window.

## Root cause

`wingman/behavior_tree.py`. `BoundaryCondition.__call__` (`_yields_to_fn`
check) reads a closure — `ctx.climb_emergency_fn`, built in
`_build_climb_slot` — that returns `ClimbCondition.emergency_active`:

```python
def _climb_emergency_fn(_e=emergency):
    return bool(getattr(_e, "emergency_active", False))
```

`emergency_active` was, before this fix, set in exactly one place:
`ClimbCondition.__call__`, the same method py-trees invokes when — and only
when — it ticks the Climb leaf. `_PRIORITY_ORDER` places `BoundaryTurn`
**above** `Climb`, and `TacticSelector` is a standard priority Selector: it
ticks children in order and stops at the first one that returns
SUCCESS/RUNNING. While `BoundaryTurn` wins, `Climb`'s leaf — and therefore
`ClimbCondition.__call__`, and therefore the ttg computation that decides
`emergency_active` — is **never ticked at all**. The flag simply stops
updating, frozen at whatever it last was before `BoundaryTurn` took over
(in this incident: `False`, since the aircraft had been climbing normally
moments earlier).

The existing code comment at the read site claimed otherwise: *"The flag is
published by the closure each tick; read it lazily so the boundary leaf
sees the CURRENT tick's verdict."* This was true only for ticks where
Climb's own leaf happened to be ticked — which is exactly the case the
yield mechanism exists to handle when it's **not** true. The one existing
test for this interaction, `test_it_yields_to_the_climb_emergency_band`,
tests `BoundaryCondition` in isolation against a hand-fed mock
`yields_to_fn` directly controlled by the test — it proves the yield
*mechanism* works when told the emergency is on, but never exercised the
real wiring's staleness, because nothing in the test suite drove the real
tree through repeated ticks with `BoundaryTurn` continuously winning. A
green suite is not coverage of the integration that actually broke.

## Why nothing recovered

| path | evidence | why it failed |
|------|----------|----------------|
| `Climb`'s emergency band (ADR 086 d2) | never fired — no `DIVE RECOVERY` line | its own condition was never ticked; see Root cause |
| `BoundaryTurn`'s yield to that emergency (ADR 107 D4) | `BoundaryTurn` selected continuously, 40.760-49.770+ | reads a flag that can only be fresh if Climb is ticked — circular, by construction |
| `EjectStuckDetector` / `LivenessGuard` | not applicable | this incident never left `GAME_BATTLE`, and OCR/FSM activity continued throughout — neither guard is scoped to catch a sustained but not-yet-fatal dive |
| Operator | caught it, pressed Backspace at 470m and falling | the only path that actually worked |

## Fix

`wingman/behavior_tree.py`:

- `ClimbCondition.update_emergency(snapshot, now=None)` — new method,
  contains exactly the ttg/emergency computation that used to live inline
  in `__call__` (respawn-discontinuity handling, `_time_to_ground`, the
  settle gate, the confirm-reads debounce, the `DIVE RECOVERY` log line).
  Sets `_emergency_active` and a new `_pending_reevaluation` flag.
- `ClimbCondition.__call__` — no longer computes the emergency verdict
  itself when something already has this tick (checks
  `_pending_reevaluation`); falls back to calling `update_emergency` inline
  when nothing did, so every existing direct-call site (all of
  `TestTimeToGroundRecovery`, 10 tests) is unaffected. The band-hysteresis
  vote (`_active`/`_streak`, altitude threshold crossing) is unchanged —
  still only runs when Climb's leaf is actually ticked, same as before;
  this fix does not touch that half.
- `_build_climb_slot` also publishes `ctx.climb_emergency_update_fn`
  (mirroring the existing `climb_emergency_fn`), exposed on the built tree
  as `tree.climb_emergency_update_fn`.

`wingman/tick_handlers.py`: `BehaviorTreeHandler.tick()` calls
`self._climb_emergency_update_fn(snap, now)` unconditionally, right after
the snapshot is assembled and **before** `self._tree.tick()` — the same
"perceive before select" ordering `BoundaryPerceptionHandler.perceive()`
already uses for boundary readings, guarded with a bare `try/except`
logged at DEBUG so a failure here degrades to the old (buggy but
non-crashing) behavior rather than taking down the tick.

**Testing**: `tests/test_behavior_tree.py` —
`test_reproduces_the_incident_without_the_pre_tick_update` (pins the bug:
using the real tree, altitude/rate matched to the incident's own readings,
`BoundaryTurn` stays selected across 5 ticks of a 6s-ttg dive when nothing
calls the pre-tick update — exactly the old production call site) and
`test_yields_to_climb_when_the_pre_tick_update_runs` (the fix: identical
snapshot, one call to `tree.climb_emergency_update_fn` before `tree.tick()`,
`Climb` wins the same tick). Both use the real `build_tree`/`ClimbCondition`
/`BoundaryCondition`, not a re-implementation. Full existing suite
(`test_behavior_tree.py`, `test_tick_handlers.py`, `test_climb_mode.py` —
355 tests) passes unmodified, confirming the split is behavior-preserving
everywhere it isn't the specific gap being closed. `make lint && make test`
green.

## Impact

- **Measured**: at least 9 continuous seconds with `ttg` inside the
  emergency band and no recovery response; the aircraft's last observed
  reading was 470m and still descending when the operator intervened.
  Whether it would have crashed, recovered on its own once `BoundaryTurn`'s
  own turn eventually resolved, or been saved by `Climb`'s ordinary
  (non-emergency) altitude band is unknown — wingman was stopped before any
  of those could play out.
- **Not an isolated fluke of this one session**: the mechanism is a
  structural gap in how the tree's priority Selector interacts with a
  cross-leaf "yield" property, present since `BoundaryTurn`'s yield-to-Climb
  design (ADR 107 D4) was built. It would reproduce identically any time a
  genuine ttg emergency develops *while* `BoundaryTurn` (or, by the same
  reasoning, any tactic above Climb in `_PRIORITY_ORDER` that reads
  Climb's emergency flag) is already selected.
- Distinct from ADR 137 D9 (which fixed Climb not noticing an emergency
  escalation *while Climb itself is already RUNNING*): this is Climb never
  being *asked* at all, a different failure mode with the same symptom
  family (a stale emergency read).

## Disposition

1. **Done.** Root cause traced to the exact code and confirmed against the
   live incident's own telemetry. See Root cause above.
2. **Done.** Fix implemented, tested against a real reproduction of the
   incident (not a mock), full suite green. See Fix above.
3. **Done.** Live-validated across three sessions spanning two nights,
   117 real occurrences total (`DIVE RECOVERY` firing while `BoundaryTurn`
   was selected). Zero recurrence of the original bug in any of them.
   Resolution time: same-tick in most cases, up to ~6s in one case where
   `MinimumHold`'s own separate 3.0s anti-flapping hold added its own
   bounded delay on top — the third session added more occurrences but no
   new worst case. See Summary above for the full measurement and the
   correction to the original, too-small-sample "1-2 ticks always" claim.
4. **Not done, and out of scope here**: the *other* half of
   `ClimbCondition` — the ordinary altitude-band hysteresis (`_active`,
   non-emergency) — has the same theoretical staleness property (it also
   only updates when Climb is ticked) but is lower-severity (a delayed
   ordinary climb, not a missed ground-collision emergency) and was not
   observed causing harm tonight. Flagged as a smaller, separate follow-up,
   not folded into this fix.
5. **Not done, separate from this fix**: whether `MinimumHold`'s 3.0s hold
   should itself be bypassed or shortened specifically for the emergency
   case (rather than treated identically to an ordinary boundary-turn
   release) is a real design question this session's data raises but this
   record does not answer — flagged, not decided. Item 4/5's own class of
   tradeoff: any such change would need the same shadow-first treatment,
   not a same-night addendum to a fix already shipped.

## What to watch

- **Confirmed across three sessions, 2026-09-14 through 16**: `Climb`
  always eventually interrupts a live `BoundaryTurn` selection when `ttg`
  drops inside the emergency window — 117/117 real occurrences, zero
  unresolved. Resolution time varies (same-tick to ~6s, see Summary) rather
  than being uniformly instant — worth tracking whether ~6s recurs often
  enough to be
  worth shortening (Disposition item 5).
- Whether the `update_emergency` pre-tick call ever measurably changes
  ordinary (non-BoundaryTurn) session behavior — it shouldn't, since
  `__call__`'s own fallback makes the two paths equivalent when Climb is
  the one being ticked, but this is exactly the kind of assumption item 3's
  live trial exists to check rather than take on faith.
- Whether the same staleness pattern exists anywhere else `climb_emergency_fn`
  (or any other cross-leaf "yield" reading) is consulted — only one call
  site currently exists (`BoundaryTurn`'s `yields_to_fn`), but a future
  tactic reading the same or a similarly-constructed flag would inherit the
  identical bug unless it also gets a pre-tick update.

## References

- ADR 107 — BoundaryTurn tactic; D4 is the yield-to-emergency design this
  incident found broken.
- ADR 086 — the ttg/time-to-ground emergency trigger itself (d2/d3/d4).
- ADR 137 D9 — the related but distinct fix for Climb's emergency flag
  going stale *while already RUNNING*; this incident is the same symptom
  family from Climb never being ticked *at all*.
- ADR 139 D1/D3 — the slot-based tree composition and the `ClimbCondition`
  class this fix extends; `_BuildContext.climb_emergency_fn` is the
  existing pattern `climb_emergency_update_fn` mirrors.
- `wingman/behavior_tree.py` — `ClimbCondition.update_emergency`,
  `BoundaryCondition.__call__` (`_yields_to_fn`).
- `wingman/tick_handlers.py` — `BehaviorTreeHandler.tick()`, the pre-tick
  call site.

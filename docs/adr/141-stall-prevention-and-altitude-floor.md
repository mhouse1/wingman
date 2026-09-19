# ADR 141 — Stall Prevention and a Hard Altitude Floor

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-19 | 1.8.10          |

## Context

Live session, 2026-09-19 02:26:38-50: a `MissileEvade` hold ran for ~6
seconds (02:26:38.957-44.939) with nothing watching the airframe underneath
it. Telemetry during that window showed `Speed: 3` (02:26:41.930) and,
moments later in a screenshot the operator captured specifically to pin the
timestamp (`screenshot_20260919_022644.png`), `27 KPH` — a near-stall, not
a dive. Under a second after MissileEvade released control, telemetry
showed a ~745m altitude loss and a jump to 679 KPH. The aircraft crashed at
02:26:50.938 with 3 missiles unused.

Neither of this project's two existing emergency mechanisms could have
caught this in time:

- **The ttg (time-to-ground) trigger** (ADR 086 d2, `recover_below_time_s`)
  needs a *negative altitude rate* to compute anything at all. A
  near-stalled aircraft can show a near-zero or even briefly positive rate
  right up until it isn't — the trigger has nothing to key off until the
  fall is already underway.
- **`MissileEvade` itself has no yield mechanism.** Unlike `BoundaryTurn`
  (ADR 107 D4, `yields_to_fn`), which was given exactly this kind of escape
  hatch to Climb's emergency verdict, `make_missile_evade_condition` never
  had one — despite outranking Climb in the priority selector. Climb's own
  emergency verdict only got a chance to matter once MissileEvade released
  the airframe on its own schedule, by which point it was too late.

Operator directive, verbatim: *"we should implement an stall prevention
mechanism, if speed below 300KPH it should deactivate break and activate
afterburner, during evade manuvers and mission_j20 if altitude below 3000
it should automatically fly up."*

This landed the same night as two other, narrower fixes to the same general
"Climb doesn't get a fair chance to act in time" failure class — ADR 086's
`_climb_exit_push` overshoot bug and ADR 107's BoundaryTurn/Climb handback
race. Those are misfires in mechanisms that already existed. This ADR is
different: it adds two mechanisms that did not exist before tonight.

## Decision

### D1 — A third, deliberately simple emergency OR-term: the altitude floor

`ClimbCondition.update_emergency` (`wingman/behavior_tree.py`) gains
`alt_floor_m`, configured via `behavior_tree.climb.alt_floor_m` (**4000** in
production `config.yaml`; see correction below). No rate, no confirm-reads
debounce beyond the already-immediate single check, no per-map tuning —
`snapshot.altitude < alt_floor_m` is the entire condition. `emergency =
ttg_emergency or terrain_ahead or alt_floor_emergency`. This is deliberately
the crudest possible backstop: a hard floor that does not care whether the
aircraft got there diving, level, or climbing too slowly — exactly the
coverage gap the ttg trigger's rate requirement leaves open.

Unset (`None`) disables it, same opt-in shape every other threshold in this
class already uses. Edge-triggered `BT: ALTITUDE FLOOR` WARNING log,
matching `DIVE RECOVERY`/`TERRAIN AHEAD`'s own logging shape.

**Corrected 3000 → 4000, same night, live, operator direct correction**:
shipped at 3000m per the operator's own verbal directive at the time ("if
altitude below 3000 it should automatically fly up"). Hours later, mid-D5-
trial, the operator corrected this directly: "during mission_j20 we're
supposed to stay above 4000 m." That number is not new — it is ADR 081 d2's
already-`Accepted` (2026-08-18) "armed sustain floor," which states the
identical doctrine in its own words: "the tactical requirement is only that
an armed aircraft stays above 4000 m," implemented there as `climb.sustain.
enter_below_alt: 4000` (with a 5000m exit, the same 1000m hysteresis this
ADR's own band uses elsewhere). The original 3000 was an imprecise
recollection of an existing, accepted number, not a deliberate new choice —
corrected to 4000 to match it. The two mechanisms remain distinct and both
kept: ADR 081's sustain band is soft (its own debounce, requires missiles
> 0 and a running mission — armed only) and this floor is a hard,
unconditional backstop (no armed-state gate) for the cases the sustain band
cannot cover — an unarmed aircraft, or needing to react faster than the
sustain band's own hysteresis allows. Both now target the same altitude
instead of two different ones. No production-logic test changes needed —
every test in `tests/test_behavior_tree.py` and `tests/test_stall_
prevention.py` passes its own explicit `alt_floor_m` fixture value; none
read the production default, which is exactly how this drifted unnoticed
for hours. **New regression test added**: `tests/test_controller_config.
py::test_mission_j20_altitude_doctrine_is_4000m_everywhere` loads the real
shipped `config.yaml` (the existing `shipped_cfg` fixture, ADR-A-02's own
pattern) and asserts `climb.alt_floor_m` and `climb.sustain.enter_below_alt`
both equal 4000 — the thing that should have caught the mismatch the first
time, per the operator's own "we keep fixing and revisiting this" question.
`make lint && make test` (full suite) green. Not yet live-validated at the
corrected value.

### D2 — `MissileEvade` gains a `yields_to_fn`, identical to BoundaryTurn's

`make_missile_evade_condition(is_running_fn=None, yields_to_fn=None)` —
checked first, unconditionally, before the incoming-detection/stickiness
logic: `if yields_to_fn is not None and yields_to_fn(): return False`.
Wired in `_build_missile_evade_slot` to `ctx.climb_emergency_fn`, the exact
same closure BoundaryTurn already reads. This required moving
`_build_missile_evade_slot`'s call in `_build_slots` to *after*
`_build_climb_slot`'s (it read `ctx.climb_emergency_fn` before Climb's slot
builder had set it) — the same "build order satisfies data dependencies,
independent of `_PRIORITY_ORDER`" pattern `_build_boundary_slot` already
established (ADR 139).

Yielding does not strand the evade actuator thread — `_run_missile_evade_hold`
has its own independent fuel/state gating (SAF-013) that does not depend on
the condition re-selecting it; yielding only stops the *selector* from
choosing it again next tick. This is the same reasoning BoundaryTurn's own
D4 decision already used.

Together, D1 and D2 are what closes "during evade maneuvers... automatically
fly up" from the operator's directive: the altitude floor gives Climb a
real emergency signal that doesn't need a rate, and the yield gives that
signal somewhere to land even while MissileEvade currently outranks Climb.
"mission_j20" (the operator's second named context) needed no extra
tactic-level wiring — every tactic ranked *below* Climb in the priority
selector already loses to it automatically the moment `emergency_active`
is true; MissileEvade and BoundaryTurn were the only two ranked *above* it,
and BoundaryTurn already had this since ADR 107 D4. `Eject` (ranked above
both) deliberately does **not** yield — seeded self-destruct dive, discussed
in Non-Goals.

### D3 — Stall prevention: `note_stall_prevention`, tree-independent

New `Controller.note_stall_prevention(game_state)`, called every tick from
`tick_handlers.py` immediately after `note_afterburner_cruise` — the exact
same tree-independent, once-per-tick shape ADR 134 D9 already established,
for the same reason: a stall is an airframe state, not a tactic, and must
not wait for the tree to select something that happens to care about
airspeed.

Below `stall_prevention.min_speed_kph` (300 in production config, raw
*last-accepted* speed reading — not the smoothed `stable_value`, same ADR
069 d6 reasoning `TelemetrySnapshot._ratio_speed` already documents: a ~9s
smoothing window lags a fast real change, and a stall is exactly that) for
`confirm_reads` consecutive ticks (2 in production, debouncing entry the
same way cruise's low-fuel exit does): release `AIRBRAKE_KEY`, hold
`AFTERBURNER_KEY`. Re-asserted every tick while active (D9's own shape —
wins the key back within one tick of anything else re-pressing airbrake
behind it). Exit is immediate on the first good reading — there is nothing
held to debounce releasing, and the airbrake release is a one-shot action
each tick, not a state to unwind.

**Deliberately overrides Climb's own emergency airbrake hold when both are
true at once.** Climb's `EMERGENCY` mode holds `AIRBRAKE_KEY` on purpose
(ADR 137) — bleeding energy out of a *fast* dive. Stall prevention releases
it on purpose — a stalled airframe needs energy *back*. These are opposite
prescriptions for opposite problems that happen to touch the same key, and
they only conflict when both are true simultaneously (a fast dive that has
also dropped below the speed floor). Stall prevention wins: a stalled
aircraft has degraded control authority regardless of what the pitch axis
is commanding, so holding drag on does not help it climb out — this is
standard unstall procedure (reduce drag, add power), not a judgment call
specific to this codebase. Routed through the existing `_may_hold_key`
arbiter (ADR 139 D4) for the afterburner side, as a new `"stall_prevention"`
requester (unconditionally `True` — the one real gate, manual takeover, is
already checked once before either key is touched); the airbrake release
is not gated through `_may_hold_key` at all, since that arbiter answers
"may I *hold*," not "must I *not release*."

Only real gate: `SAF-001` manual takeover — checked once, matching cruise's
own single real deferral.

### D4 — `Idle` gains the same `yields_to_fn`, discovered because D1 exposed it

Found the same night, directly observed by the operator ("it flew forward
horizontal after respawn without changing altitude"): `is_idle` —
`snapshot.game_state != GameState.GAME_BATTLE` — is first in
`_PRIORITY_ORDER` and had no way to cede the airframe to *anything*,
including a live Climb emergency, for the entire time the FSM sits outside
`GAME_BATTLE`. This is not new tonight — it is the tree's oldest priority
rule — but D1's altitude floor exposed it far more often than the
pre-existing ttg trigger ever did, because D1 needs no rate, only altitude.

Traced in `wingman.log`: an `eject_and_dive` sequence aborted mid-dive
(Anomaly 003 — telemetry showed the aircraft alive and flying, not
actually dead) and handed control back while `game_state` was still
`GAME_BATTLE_EJECT`, not yet `GAME_BATTLE`. D1's `ALTITUDE FLOOR` fired and
logged `climb forced` correctly — but `selected=Idle` won the very same
tick regardless, and Idle presses nothing, so the aircraft kept flying
whatever heading it already had. Confirmed twice in one session, ~3 hours
apart, with the same shape both times.

**Fixed the same way as D2 and D4's own MissileEvade precedent above, but
narrower.** `make_idle_condition(yields_to_fn=None)` steps aside only when
`game_state == GAME_BATTLE_EJECT` *and* `yields_to_fn()` (Climb's emergency
verdict) is true — `GAME_LOBBY`/`GAME_STARTING` (no aircraft exists to fly
at all) are untouched, and ordinary tactics (Engage, BoundaryTurn, ...) get
no new access to `GAME_BATTLE_EJECT` either, only Climb's emergency does.
No separate "is Eject actually still active" check is needed: `TACTIC_EJECT`
already outranks `TACTIC_CLIMB` in `_PRIORITY_ORDER`, so a genuinely still-
true Eject condition wins before the selector ever reaches Climb — this
change only matters for the gap where Eject's own condition has already
gone false but `game_state` hasn't caught up, which is exactly the live
case above. Required moving `_build_idle_slot`'s call in `_build_slots` to
after `_build_climb_slot`'s, same build-order dependency D2's MissileEvade
change already introduced.

**Separately confirmed, not fixed by D4**: the underlying `Altitude: 600`
reading itself is a real, recurring artifact — seen 8 times across this
session, always with `Speed` changing normally while `Altitude` stays
frozen at exactly 600, always during this same eject-abort/respawn-restart
window (`eject_and_dive — rotation pulse ...` appears in the surrounding
log both times checked in detail). D4 fixes what the tree does with an
emergency verdict during `GAME_BATTLE_EJECT`; it does not explain or fix
*why* altitude specifically reads a frozen, suspiciously round value during
this window while other telemetry fields keep updating. Left open — see
Open Questions.

### D5 — `BoundaryTurn` splits off a narrower `hard_emergency_active`, so it stops yielding to the altitude floor alone

Found the same night, directly observed by the operator ("it keeps flying
out of the map, this is regression") on a live session already running
D1-D4. `wingman.log` trace: D1's altitude floor correctly forced a climb
from a genuinely low reading (1799m, well under the 3000m floor). Altitude
cleared 3000m within the climb, but the pre-existing terrain-ahead trigger
(ADR 073/ADR 140's OR-term on the same `emergency` flag) kept re-firing
roughly every 12 seconds for 45 seconds straight — three separate `TERRAIN
AHEAD` log lines inside one `Controller: climb complete (altitude_recovered,
45.0s)` window. `BoundaryTurn`'s `yields_to_fn` (ADR 107 D4) reads that same
combined `emergency_active` flag and has no way to distinguish which OR-term
is driving it — so it stayed locked out of selection for the entire 45s
window while the aircraft flew straight through the map boundary. Confirmed
via two separate `MAP BOUNDARY: crossed` trace logs in the same window,
`tactic: "Climb"` continuously selected while `dist` fell steadily to
~0.07-0.1 over 20+ ticks.

**The gap: D1 quietly widened what BoundaryTurn yields to.** ADR 107 D4's
own stated reasoning for the yield was "hitting the ground is certain" —
true of the ttg and terrain-ahead triggers, which both require a specific,
currently-measured danger signature. The altitude floor is deliberately the
opposite: a flat, rate-independent, preventive backstop (D1's own framing)
that can stay true for a long climb entirely by design, with no requirement
that anything dangerous is still happening once the climb is underway.
BoundaryTurn had no way to know the difference and yielded to all three
identically.

**Fix: split the emergency signal at the source, in `ClimbCondition`
itself**, rather than teaching `BoundaryTurn` to re-derive the distinction
from the outside. `update_emergency` now captures a second flag,
`hard_emergency_active`, immediately after the ttg/terrain OR chain and
*before* the altitude floor is folded in — so it is true exactly when ttg or
terrain is, and never true on the floor alone. The existing broad
`emergency_active` is unchanged (still ttg or terrain or floor) and stays
the signal `MissileEvade` (D2) and `Idle` (D4) read — the operator's
original directive was explicit that MissileEvade should respect the
altitude floor, and D4's fix targets the same "aircraft does nothing at
all" failure mode the floor is meant to prevent, so narrowing those two
would reopen the bug D2/D4 exist to close. Only `BoundaryTurn` — the one
tactic whose own job is normal, non-emergency flight-path correction near
the map edge — moves to the narrower signal, via a new `ctx.
climb_hard_emergency_fn` exposed by `_build_climb_slot` alongside the
existing `ctx.climb_emergency_fn`, and a matching `tree.
climb_hard_emergency_fn` exposure on the built tree.

**Every pre-existing yield test in `tests/test_behavior_tree.py` passed
unmodified** — `test_boundary_turn_yields_to_a_terrain_emergency`,
`test_yields_to_climb_when_the_pre_tick_update_runs`, and the rest all
still pass because ttg and terrain remain in the hard tier exactly as
before; this confirms the split is additive, not a behavior change to the
two triggers ADR 107/ADR 140 already validated live.

**Live trial, 2026-09-19, first ~13 minutes on the fixed code**: the old
session (5h21m, pre-D5) was let finish its round normally, archived, and a
fresh session started on the fix. `BoundaryTurn` visibly regained selection
within one tick of a floor-only emergency clearing — e.g. at 08:24:11 Climb
logged `climb complete (altitude_recovered, 12.0s)` with altitude still
under the 3000m floor's own hysteresis band, and the very next tick
selected `BoundaryTurn`, exactly the behavior D5 exists to restore. One
boundary crossing still occurred shortly after (08:24:16, "confirmed
crossing 1"), but the trace shows a different mechanism than the fixed
regression: a genuine terrain-ahead emergency (confirmed sky-fraction
reading, not the floor) had put Climb back in control moments earlier while
the aircraft was already drifting toward the edge with boundary readings
unavailable (`dist: null` for several ticks around a death/respawn in the
same window); by the time `BoundaryTurn` got control back, `dist` was
already 0.22 and closing, leaving roughly 3 seconds before it crossed —
not enough turn radius, not a failure to yield. `hard_emergency_active`
correctly excluded the floor and correctly included the fresh terrain
re-firing throughout (confirmed by re-reading `update_emergency`'s source
against the trace, not inferred). Also visible in the same trace: `MinimumHold`'s `hold_s: 3.0` anti-flapping
window on `BoundaryTurn` (pre-existing, ADR 024) would independently mask a
hard emergency re-firing for up to 3s after `BoundaryTurn` starts,
regardless of D5 — not confirmed as causal in this specific crossing (the
turn-radius explanation above accounts for it without invoking the hold),
but worth keeping in mind if a future trace shows `BoundaryTurn` holding
well past a hard emergency's return with `dist` still recoverable.

**Second crossing, same session, 5.5 minutes later (08:29:54, "confirmed
crossing 2")**, same signature: `Climb` held selection through an altitude-
floor recovery (1944m → 3176m), handed off to `BoundaryTurn` the instant
altitude cleared 3000m, and the aircraft crossed 3 seconds later. This time
with a clear cause visible in `wingman.log` itself, not just inferred from
`dist`: `Afterburner fuel: 49%` → `climb — fuel recovered to 20% —
afterburner re-engaged` → speed telemetry `992 → 867 → 945 → 2167 KPH`
across the same ~12 seconds, peaking right as `climb complete (stopped,
28.5s)` handed off to `BoundaryTurn`. The climb-recovery's own fuel-gated
afterburner logic (pre-existing, ADR 134 D9 lineage, untouched by tonight's
work) had the aircraft near a local top speed at the exact moment
`BoundaryTurn` regained control — a turn takes a roughly fixed amount of
time to bite regardless of speed, so the faster the handoff, the less real
distance is left to use it in.

**Is this new, or was it always there?** Checked directly rather than
guessed: grepped all 37 `MAP BOUNDARY trace` lines from the archived
pre-D5 session. **28 of 37 (76%)** show `Climb` selected for the entire
20-tick trace window with `BoundaryTurn` never appearing — the exact
"locked out, no chance to react" signature D5 targets. **9 of 37 (24%)**
already show this same `Climb`-hands-off-to-`BoundaryTurn`-then-crosses-
quickly signature, on code that predates every change made tonight. Both
of the two post-fix crossings observed so far are this second type, and
neither is the first type — consistent with what should happen if D5
correctly removed the dominant mechanism and left the pre-existing,
independent one exactly as it was. **Rate context, held to a low
confidence label deliberately**: the "locked out" mechanism ran ~5.2/hr
pre-fix and has not recurred at all in the post-fix trial so far (measured,
small sample). The "fast handoff" mechanism ran ~1.7/hr pre-fix; two
occurrences in the first ~15 minutes of the post-fix trial is higher than
that rate would predict, but a Poisson process at 1.7/hr has roughly a 7%
chance of producing 2+ events in any given 15-minute window by chance
alone — not enough to call an increase yet. **Not fixed tonight, not
proposed as part of D5**: this is a distinct, pre-existing interaction
(fast post-climb speed colliding with a close boundary at the moment of
handoff) that deserves its own dedicated look — likely candidates for a
future decision include capping speed during the tail of an altitude-floor
climb-out, or having `BoundaryTurn`'s engagement account for closing speed
rather than static distance alone — but proposing that now, in the same
sitting as D5 and without more live data, would repeat exactly the
"one change at a time" mistake ADR 106 warns against. Left as an open
item; see Open Questions.

**Third and fourth crossings, same trial, 08:34:49 and 08:35:36** (only
47s apart). Both same mechanism as the second; the fourth's evidence
screenshot (`rtb_20260919_083536_crossing4.png`) shows why directly:
1302 KPH, squeezed between two steep ice-mountain faces with the map
boundary running along the same ridge line, a large furball (20+ friendly
icons alone in the trace's `friendly` field) at this exact map edge. All
four crossings this trial are on `mission_j20` — the same mission the
operator's original directive named specifically because of its terrain,
which is what motivated D1's altitude floor in the first place. This
reframes the finding: the tension isn't purely a software timing gap, it
is `mission_j20`'s own geography — a mountain range that IS the map edge,
where the correct terrain response (climb, often toward the only open sky,
which is over the boundary) and the boundary response pull in different
directions by the map's own design, not by a logic error. Session health
otherwise unaffected: 12 respawns and all 4 crossings in ~21 minutes of
an unusually intense, high-respawn battle right at this edge, zero deaths
attributable to any crossing, aircraft actively re-engaging between each
one. Rate context updated: 4 in ~21 min is well above both the pre-fix
"handoff" baseline (~1.7/hr) and the pre-fix combined rate (~6.9/hr) — no
longer dismissible as small-sample noise, but the mountainous-mission_j20
explanation means the right next step is a dedicated design pass weighing
terrain-safety-vs-boundary-safety trade-offs specifically for this kind of
terrain, not a quick patch tonight. Recommending this as the next distinct
work item, separate from D5, which itself remains confirmed correct (all
four crossings show the pre-existing handoff signature, none show the
fixed lockout signature).

**Fifth and sixth crossings, same trial, 08:41:08 and 08:48:57** — same
trial, same mission_j20 edge, same mechanism confirmed again on direct
inspection (crossing 6's trace shows two genuine `TERRAIN AHEAD`
confirmations holding `Climb` right up to 1.1s before the natural handoff
at 3000m, `BoundaryTurn` winning the very next tick — not a lockout, just
a photo finish). Six for six now: every crossing this trial shows the
handoff signature, zero show the fixed lockout signature. `friendly`
counts up to 55 in crossing 6's trace — this stretch of the trial appears
to be an unusually large, sustained team fight compressed against this
one mission's boundary-adjacent terrain, which likely explains the
elevated rate more than any change in the underlying mechanism's own
per-opportunity odds.

**Seventh crossing, 08:51:02 — confirms the mechanism once more, and
clarifies the distinction that actually matters.** All 20 trace ticks show
`Climb`, no `BoundaryTurn` at all — superficially the old lockout
signature. Checked directly against `wingman.log`: two genuine `TERRAIN
AHEAD` confirmations 15 seconds apart (08:50:39, 08:50:54) — the same
repeated-re-firing cadence the original bug report described — kept
`hard_emergency_active` legitimately true across the whole window; the
emergency cleared within the same second as the crossing, essentially a
photo finish rather than a lockout. The distinction that matters: the
original bug held `BoundaryTurn` out on the floor alone, with no real,
currently-confirmed hazard. Every crossing this trial, including this one,
carries a genuine, freshly-confirmed hard emergency driving the hold,
exactly matching ADR 107 D4's own "hitting the ground is certain"
justification for the yield. Seven for seven now: every crossing carries a
confirmed hard-emergency cause, none is the floor-alone lockout D5 fixed.
Not re-litigating each further crossing at this depth going forward — the
mechanism is established; continuing to watch for any crossing that does
NOT fit this pattern, which would be the actual signal something is still
wrong.

**Eleventh crossing, 09:58:50 — a third, distinct, unrelated mechanism,
confirmed benign.** Trace shows `MissileEvade`, not `BoundaryTurn`, and
several `outside: true` ticks scattered through the window with real
missile-ring counts present (up to 4 in the short/mid rings) — a genuine
evasive maneuver against live inbound missiles that happened to carry the
aircraft across the boundary more than once while dodging. `MissileEvade`
ranks *above* `BoundaryTurn` in `_PRIORITY_ORDER` (original ADR 070/107
design) and has no yield relationship to it at all in either direction —
untouched by D5 or any of tonight's other changes. Expected: staying alive
against a real missile outranks staying in bounds, same reasoning as the
terrain-priority crossings above. Noted for completeness, not treated as a
new pattern to track.

**Fifteenth crossing, 11:09:47 — same theme, reversed direction.**
`BoundaryTurn` held selection and was actively, successfully maneuvering
(dist oscillating 0.04-0.41 for ~28 seconds, well inside the boundary the
whole time) when `Climb` interrupted it on the very last tick and the
aircraft crossed one tick later. No fresh `TERRAIN AHEAD`/`ALTITUDE
FLOOR`/`DIVE RECOVERY` logged at that transition, so the cause wasn't
immediately obvious from a single log grep; not chased further given the
session's overall pattern is already well established and this remains
non-fatal. Filed under the same general class as the rest of this
section — Climb-side priority (whatever specifically drove it here)
costing `BoundaryTurn` margin at a bad moment — not a new mechanism
requiring its own write-up tonight.

## Non-Goals

1. **Not touching `Eject`'s priority or its own dive.** `eject_and_dive` is
   a deliberate, controlled self-destruct (ADR 136/137) — a low-speed or
   low-altitude reading during an intentional dive is not a stall or a
   terrain emergency, it is the dive working as designed. Neither D1's
   floor nor D2's yield reaches `Eject`; D3's stall prevention is not
   tactic-gated at all and *does* still apply during `GAME_BATTLE_EJECT`
   (a real stall can happen mid-eject too, independent of the intentional
   descent `eject_and_dive` itself controls), but does not touch
   `eject_and_dive`'s own nose-down/afterburner sequencing directly — it
   only ever touches `AIRBRAKE_KEY`, which `eject_and_dive` does not use.
2. **Not resolving the still-open "stale telemetry during critical
   recovery" pattern** flagged the same night across at least four separate
   crash traces (documented in ADR 086 and ADR 107's live-trial sections).
   That is a suspected telemetry/OCR-cadence issue, not something either of
   tonight's three fixes (this ADR, ADR 086's exit-push fix, ADR 107's
   handback-race fix) directly addresses — it may turn out to be partly
   mitigated by D1's floor (a hard altitude check doesn't need a fresh
   *rate* the way ttg does) but that is a hypothesis for the next live
   trial to test, not a claim made here. **Recurred during the D5 trial,
   09:11:01-09:11:04**: a genuine `TERRAIN AHEAD` firing (sky fraction
   0.00) with `ttg=3s` — as urgent as this session got — froze `alt`,
   `alt_rate`, and `ttg` at identical values (674.67m, -250m/s, 3s) across
   three consecutive ticks (~3 seconds) while `Health OCR` simultaneously
   returned no digits, then the aircraft died. D1's floor did not get a
   chance to show whether it mitigates this, since a confirmed hard
   emergency was already driving Climb throughout. Another data point for
   the existing tracked issue, not a new one. **Recurred again 9 minutes
   later, 09:13:20-09:13:23**, same session, same shape (`alt`/`alt_rate`/
   `ttg` frozen at 1112.0m/-332m/s/3s across 3 ticks, `Health OCR` failing
   simultaneously, aircraft dead by the next tick) — two occurrences in
   one session reinforces this is a real, recurring rate, not a rare
   fluke, though still not something to react to mid-trial per the
   reasoning above. **Third occurrence, 09:29:29-09:29:32, same session**:
   identical shape (`alt`/`alt_rate`/`ttg` frozen at 184.33m/-49m/s/4s
   across 3 ticks, `Health OCR` failing simultaneously), this time ending
   in `alt=0` at the crash detector — an apparent genuine ground impact,
   not just a combat loss. Three occurrences in ~30 minutes of one session,
   all fatal, all with a confirmed `TERRAIN AHEAD` shortly before the
   freeze — this is frequent and severe enough to warrant flagging to the
   operator directly rather than only recording it here, even though it
   remains out of scope for tonight's fixes.

   **Substantially reframed, 2026-09-19, later the same night — likely not
   a pipeline bug at all, for the subset that ends in a confirmed death.**
   The operator raised this a third time live ("same issue just happened").
   Rather than log a fifth data point, pulled the actual `crash_with_
   missiles` capture frames for three fresh occurrences
   (`crash_20260919_153831_0.png`, `_153901_1.png`, `_153927_2.png`) and
   looked directly at what the game was rendering in the second or two
   before each confirmed death. All three: the normal altitude/speed HUD
   block is simply absent from the frame — one is fully engulfed in a
   fireball, one shows a HUD-free wide shot with no cockpit overlay at all,
   one has a clear view of real terrain and sky with the HUD block still
   missing. This is consistent with the game switching to a damage/death
   cinematic camera that does not render the pilot's instrument HUD during
   that sequence — not a wingman OCR or telemetry-pipeline defect. Wingman
   correctly reports "no digits" because, for those frames, there are none
   to read. This does not explain every historical occurrence of this
   pattern — earlier sessions logged some freezes that did not end in an
   immediate death and would need their own frame-level check before being
   folded into this explanation — but for the specific, most common shape
   tracked in this ADR (freeze then death within 1-2 ticks), this is a
   direct, three-for-three visual confirmation, not a hypothesis. No code
   change proposed: there is nothing in the OCR/telemetry pipeline to fix
   when the underlying frame genuinely contains no HUD to read. Left open
   only as: whether the non-fatal freezes seen earlier in the night share
   this same explanation (a brief damage-reaction camera cut that does not
   end in death) or are a genuinely different mechanism — a question for
   whoever picks this up next, with a much narrower scope than before.
3. **Not a general MissileEvade redesign.** D2 adds exactly one escape
   hatch (the climb emergency verdict) — MissileEvade's own fuel gating,
   manoeuvre cap, and pitch-down option (ADR 070) are all unchanged.
4. **Not consolidating stall prevention into `_run_climb_hold` itself.**
   D3 lives as an independent, tree-external check specifically so it
   applies regardless of which tactic (or none) currently holds the
   airframe — folding it into Climb's own hold loop would only cover the
   case where Climb is already selected, missing exactly the MissileEvade
   case that motivated this ADR.

## Testing plan

- `tests/test_behavior_tree.py`: `TestAltitudeFloor` (5 tests — fires below
  the floor regardless of rate, does not fire above it, disabled when
  unconfigured, the exact live near-stall shape ttg cannot catch, missing
  altitude draws no conclusion); `test_missile_evade_yields_to_the_climb_
  emergency` / `test_missile_evade_stays_sticky_when_not_yielding` (unit
  level, mirroring `test_it_yields_to_the_climb_emergency_band`);
  `test_missile_evade_yields_to_climb_through_the_real_tree` /
  `test_missile_evade_still_wins_without_an_emergency` (full-tree
  integration, mirroring the Anomaly 007 BoundaryTurn pair — proves the
  `_build_slots` reorder actually wires end to end, not just the isolated
  condition).
- `tests/test_stall_prevention.py` (new file, 11 tests, mirroring
  `tests/test_afterburner_cruise.py`'s exact shape): speed above the floor
  does nothing; a single low reading does not trigger (debounce); confirm-
  reads consecutive readings do; the exact live 27 KPH shape; recovery
  releases immediately; a stale reading does nothing; manual takeover
  blocks it entirely; outside battle states does nothing; `GAME_BATTLE_
  EJECT` still applies; disabled does nothing; re-asserts every tick while
  active.
- `tests/test_behavior_tree.py`, `TestIdleYieldsToClimbEmergency` (7 tests):
  no yield when unwired (pre-fix behaviour preserved); yields during
  `GAME_BATTLE_EJECT` with an active emergency; does not yield during
  `GAME_BATTLE_EJECT` without one; `GAME_LOBBY`/`GAME_STARTING` unaffected
  by an active emergency; `GAME_BATTLE` unaffected; full-tree integration
  proving Climb wins when Eject has aborted (the exact live shape); full-
  tree integration proving Eject still outranks Climb when Eject's own
  condition is genuinely still true (the control case — this must not
  regress).
- `tests/test_behavior_tree.py`, `TestHardEmergencyExcludesTheAltitudeFloor`
  (4 tests): the altitude floor alone is not a hard emergency (while still
  being a broad one); ttg alone is; terrain alone is; ttg alongside the
  floor is still hard (the split is an OR narrowing, not a subtraction).
  Plus two full-tree integration tests recreating the live shape directly:
  `test_boundary_turn_does_not_yield_to_the_altitude_floor_alone` (the
  regression this pins — BoundaryTurn must keep selection near the map edge
  on the floor alone) and `test_boundary_turn_still_yields_to_ttg_when_the_
  floor_is_also_active` (control case — a genuine ttg emergency riding
  alongside the floor must still win).
- Full gate (`make lint && make test`) green, zero changes needed to any
  pre-existing test — D5 is confirmed additive by the same evidence D1-D4
  used: the whole prior suite passing unmodified.
- **First live trial, 2026-09-19, ~5 hours (D1-D3 only — D4 lands after
  this trial, not yet re-validated with it live).** `ALTITUDE FLOOR` and
  `STALL PREVENTION` both fired dozens of times, all recovering cleanly —
  including one genuine near-total stall (3 KPH) at 6302m with a large
  safety margin, and the anticipated D1/D3 interaction (an altitude-floor-
  triggered `EMERGENCY` climb bleeding its own speed, caught and corrected
  by D3) confirmed live exactly as designed. This same trial is what
  surfaced D4's gap (`is_idle` blocking a live emergency) and the still-
  open `Altitude: 600` artifact — see D4 above and Open Questions. No
  crash directly attributed to D1/D2/D3 across the whole session.
- **D4 not yet live-validated** — needs its own trial watching specifically
  for the `ALTITUDE FLOOR` + `GAME_BATTLE_EJECT` combination recurring and
  confirming Climb now wins selection where it previously didn't.
- **D5 live trial in progress.** First ~13 minutes on the fixed code (see
  D5's own "Live trial" paragraph above): `BoundaryTurn` regains selection
  within one tick of a floor-only emergency clearing, confirming the core
  fix; one boundary crossing still occurred, attributed to turn-radius/
  timing around a legitimate terrain emergency rather than a failure to
  yield. Continuing to watch for the specific pre-fix signature (a
  `MAP BOUNDARY: crossed` trace showing 30+ continuous seconds of `Climb`
  selected with no intervening `BoundaryTurn`) to confirm it does not
  recur, and for `TERRAIN AHEAD`/ttg emergencies continuing to hold
  `BoundaryTurn` out exactly as before.
- **D1 exposed a separate, more serious defect during this same trial**,
  documented in full in ADR 086 (its own "exit-push-vs-immediate-restart
  oscillation" section, 2026-09-19): D1's 3000m floor sits well above this
  ADR's own climb hold's `exit_above_alt` (1000m config default), so a hold
  can complete and run ADR 086's exit push while the floor is still
  unsatisfied — the tree immediately restarts a fresh hold, and the push's
  nose-down fights the new hold's nose-up. Operator directly observed the
  aircraft "tilting up and down until it crashed into ground"
  (`screenshot_20260919_143545.png`); traced to a 24-second, never-settling
  pitch oscillation ending in a crash near 0 KPH. Fixed the same night in
  `controller.py::_run_climb_hold`, with tests in `tests/test_climb_mode.py`
  — see ADR 086 for the full trace and fix. Not yet live-validated.
- **D1 exposed a second, distinct oscillation defect, same night, same
  trial**, documented in full in ADR 137 D10: a fresh respawn landing
  below the (now 4000m) floor forces an EMERGENCY climb immediately, and
  that hold's first pulses can land entirely inside the telemetry-handoff
  blind window — before any angle sample exists to check against the
  pitch ceiling. Operator directly observed a second, separate instance
  of "tilting up and down until it crashed into ground"
  (`screenshot_20260919_155640.png`, crash 16:15:56), traced via the
  hold's own pitch-pulse log lines to two consecutive fully-blind NOSE_UP
  pulses fired back to back (ADR 137 D9's zero-gap rule, with nothing yet
  to check it against) right after respawn. Fixed the same night in
  `controller.py::_run_climb_hold` — see ADR 137 D10 for the full trace
  and fix. Not yet live-validated. Distinct from the exit-push defect
  above: that one was two holds fighting each other across a restart;
  this one is a single hold's own first pulses running blind.

## Open Questions

1. **Is 300 KPH / 3000m the right pair of thresholds?** Both are the
   operator's own directive, not derived from a live-measured false-positive
   rate the way e.g. ADR 086's `recover_below_time_s` was tuned. Watch the
   first live session for `ALTITUDE FLOOR`/`STALL PREVENTION` firing during
   ordinary, non-dangerous flight (e.g. a deliberate low pass, or normal
   post-respawn climb-out before altitude has built up) — if either fires
   on genuinely safe flight, the thresholds need revisiting, not the
   mechanism.
2. **Does stall prevention's airbrake override ever fight a genuinely
   necessary emergency airbrake hold?** **Partially answered by the first
   live trial**: D1's altitude-floor trigger routinely produces exactly
   this combination (an `EMERGENCY` climb from level flight, not a dive,
   bleeding its own speed) — observed repeatedly, D3 caught and corrected
   every occurrence cleanly, no crash resulted. Not yet observed: the
   inverse case (a genuinely fast, still-dangerous dive that ALSO drops
   below the speed floor) — D3's reasoning that it should still win in
   that case is unchanged, just not yet exercised live.
3. **Should `_run_climb_hold`'s own airbrake-hold logic become aware of
   the stall condition directly**, rather than relying on D3's external
   override to win the race every tick? The current design accepts the
   same "may oscillate slightly, net-correct" tradeoff ADR 134 D9 already
   accepts for cruise-afterburner vs. other tactics. First live trial
   showed this oscillation is real and frequent (D1's altitude floor
   triggers it on nearly every firing) but consistently harmless — not
   revisiting unless a live session shows otherwise.
4. **RESOLVED, 2026-09-19, live.** What was producing the `Altitude: 600`
   reading? Operator authorized an automated capture (real `v` keypress on
   the live game) the moment this recurred, armed via a log-watching
   Monitor. It fired twice in one minute (09:09:42, 09:10:43) and both
   times `wingman.log` shows the same unambiguous shape: an aircraft death
   moments earlier, then `Controller: spawn guard complete
   (telemetry_handoff, N.Ns)`, then this as the very first post-respawn
   telemetry reading. The captured screenshot for the first occurrence
   shows the real in-HUD altitude as **631m** — OCR read it as 600, an
   ordinary few-percent misread, not a frozen value — immediately followed
   by legitimate readings climbing normally (600 → 800 → 700 → climbing at
   +79m/s). **Not a stale-telemetry or OCR-freeze bug at all**: `mission_
   j20`'s respawn point sits at a genuinely low altitude (roughly
   600-650m), so the first post-respawn OCR read is, correctly, a number
   near 600 — occasionally held one extra tick when that cycle's Telemetry
   OCR pass is skipped (`Telemetry OCR: 0.00s` in the timing log),
   producing the "speed updates, altitude doesn't, for one tick" look this
   was originally reported with. D4's handling of this reading was already
   correct regardless of cause; this closes the "why" with direct evidence
   instead of leaving it open. The auto-capture Monitor is being stood
   down now that the question is answered — no further need to inject a
   keypress into the live session for an already-understood, benign
   pattern.
5. **Does the fast Climb-to-BoundaryTurn handoff need its own fix?**
   (D5's "Second crossing" finding.) A pre-existing, D5-independent
   mechanism — confirmed present in 9 of 37 pre-fix boundary crossings —
   where an altitude-floor climb-out's own fuel-gated afterburner logic
   leaves the aircraft near top speed right as `BoundaryTurn` regains
   control, giving a turn maneuver too little real distance to bite before
   crossing. Both crossings observed in the first 15 minutes of the D5
   trial are this type. Candidates if a future trial confirms this is
   worth fixing on its own: cap speed during the tail of a climb-out, or
   make `BoundaryTurn`'s engagement account for closing speed rather than
   static `dist` alone — neither implemented, both deliberately deferred
   to keep this decision to one change.

## References

- ADR 086 — the ttg emergency trigger this adds a third OR-term alongside,
  and the exit-push overshoot fix landing the same night.
- ADR 107 D4 — `BoundaryTurn`'s `yields_to_fn`, the exact pattern D2 copies
  for MissileEvade, and the handback-race fix landing the same night.
- ADR 134 D9 — `note_afterburner_cruise`, the tree-independent every-tick
  shape D3 copies, and the `_may_hold_key` arbiter (ADR 139 D4) D3 extends
  with a new requester.
- ADR 070 — MissileEvade's own design; D2 adds one escape hatch without
  otherwise changing it.
- ADR 136/137 — `eject_and_dive` and the emergency climb airbrake hold,
  both referenced in Non-Goals/D3's precedence reasoning.
- `screenshot_20260919_022644.png`, `test_screenshots/crash_with_missiles/
  crash_20260919_022650_14.png` — the live evidence this ADR is written
  from.
- `wingman.log`, 2026-09-19 ~00:57-08:00 session — the two `MAP BOUNDARY:
  crossed` trace logs and the three `TERRAIN AHEAD` lines inside one 45s
  `climb complete (altitude_recovered, ...)` window that D5 is written
  from.

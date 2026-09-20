# ADR 142 — Gate the Terrain-Ahead Trigger on Padlock State

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-20 | 1.8.9           |

## Implementation status (2026-09-20)

D1 implemented in `wingman/tick_handlers.py::BehaviorTreeHandler.tick()`:
the `note_padlock_center_dot` call was moved to run immediately before the
terrain-ahead read (so `padlock_state()` reflects the current tick, not the
previous one), and the terrain read is now additionally gated on
`self._ctrl.padlock_state() is False`. No change to `wingman/behavior_tree.py`
— confirmed unnecessary, per D1's own reasoning about `None` already
resetting the confirm-reads streak.

Tested structurally (`tests/test_tick_handlers.py`,
`test_terrain_detection_is_gated_to_a_confirmed_off_padlock_state`,
`test_padlock_center_dot_runs_before_the_terrain_gate_consults_it`), matching
this exact function's own established precedent
(`test_terrain_detection_is_gated_to_battle_states`) for a code path with no
full `BehaviorTreeHandler` construction fixture. Full gate green
(`make lint && make test`): 1667 passed, 2 skipped.

D2 unchanged: `terrain_avoidance.shadow` stays `true` in `config.yaml` — this
change is zero additional actuation risk.

**Live-validated the same day** — see "First live trial" below. 285/286
terrain-ahead firings correlated directly with a confirmed-`False` padlock
state; the one apparent exception traced to a same-tick logging artifact,
not a gate error. Zero exceptions from either consumer. Open Question 1
answered with real numbers. Not yet done: comparing the resulting
false-positive taxonomy against HLDD 001's own baseline (a separate,
later analysis of firing *rate*, not required to confirm the gate itself
is functioning).

## Context

HLDD 001's forward sky-occlusion terrain-ahead trigger
(`docs/hldd/001-terrain-avoidance-hldd.md`) has shipped `shadow: true` since
2026-09-17 — computed and logged every tick, never allowed to actuate —
specifically because the padlock camera re-points the capture away from
forward-looking on a roughly 6-second cadence during ordinary
search-and-destroy operation (`Controller._padlock_loop`, ADR 136), which
can corrupt the sky-occlusion read in either direction: a false "terrain
ahead" while padlocked onto an enemy against a background that happens to
read as non-sky, or a missed real terrain-ahead while the camera happens to
be looking somewhere safe. HLDD 001's own Open Question 6 named the fix as
"a padlock-engaged detector that exists and is itself validated," and
deferred re-graduation until one did.

[ADR 140](140-padlock-camera-state-detection.md) is that detector.
`Controller.padlock_state()` (tri-state: `True` engaged, `False` confirmed
forward-looking, `None` unconfirmed) has D1-D6 implemented and has been
live-validated across several sessions — most recently throughout ADR 141's
full 1h 36m soak on 2026-09-20, where it logged correctly (`padlock=` in
every `BT[active]: selected=...` debug line) the entire run. ADR 140 is
explicit, in its own words, that this remains "purely observational" (its
Non-Goals 2-3): nothing anywhere in the codebase reads `padlock_state()` for
gating or actuation yet. **This ADR is the first proposed consumer.**

Confirmed directly by reading `wingman/tick_handlers.py::BehaviorTreeHandler.tick()`
(current code, both call sites unchanged since ADR 140 landed):

```python
_terrain_sky_frac = None
if current_game_state in _BATTLE_STATES:
    try:
        _terrain_sky_frac = self._analyzer.detect_terrain_ahead(frame)
    ...
if current_game_state in _BATTLE_STATES:
    try:
        self._ctrl.note_padlock_center_dot(frame, is_respawning=..., now=now)
    ...
```

These are two entirely independent calls against the same frame, in the same
tick. The terrain reading is taken and fed into `ClimbCondition.update_emergency`
regardless of what `padlock_state()` says at that instant — exactly the gap
Open Question 6 describes, unresolved by ADR 140 landing alone.

## Decision

**D1. Gate at the perception layer, not the condition layer.** When
`self._ctrl.padlock_state()` is not `False` (i.e. `True` — confirmed
engaged — or `None` — unconfirmed) at the moment the terrain reading would
be taken, skip the `detect_terrain_ahead` call for that tick and pass
`terrain_sky_frac=None` into the snapshot instead, the same way the
existing `current_game_state in _BATTLE_STATES` check already suppresses it
entirely outside battle. No change needed inside `wingman/behavior_tree.py`:
`ClimbCondition.update_emergency`'s terrain block already treats a `None`
reading as "reset the confirm-reads streak to zero" (`TestTerrainAheadTrigger
.test_missing_reading_resets_the_streak`), the same conservative handling a
dropped OCR reading already gets — a padlock-confused tick becomes
indistinguishable from an ordinary perception gap, for free.

Deliberately gates on `padlock_state() is not False`, not `is True`:
`None` (unconfirmed) is common — ADR 140's own live trials show frequent
True/None cycling around each ~6s engagement — and treating an unconfirmed
camera state as "assume forward-looking, proceed" would let exactly the
corrupted reads this ADR exists to remove straight through. The conservative
direction (per `padlock_state()`'s own documented contract: callers needing
"confirmed forward" must check `is False` explicitly, never just falsy) is
to suppress on anything less than a confirmed-off reading, matching how
every other trigger in this codebase already treats missing evidence as a
reason to hold rather than to guess.

**D2. `terrain_avoidance.shadow: true` is unchanged by this ADR.** This
decision only removes one known corruption source from the (still
shadow-gated) trigger's input. It does not itself answer HLDD 001 Open
Question 6's re-graduation question — that stays a separate, later operator
decision, made only after a live shadow session confirms this gate actually
moves the false-positive taxonomy HLDD 001's "Live trial results" section
already measured (gathered, by that section's own admission, without
controlling for padlock state at all). Flipping `shadow: false` in the same
change as this gate would conflate two separate, independently-falsifiable
claims — "the gate reduces corruption" and "the trigger is now safe to
actuate" — the same mistake this project's shadow-first discipline exists to
prevent elsewhere (ADR 073, HLDD 001's own original graduation).

### First live trial (2026-09-20, 1h 56m session, run immediately after implementation)

`wingman.log` 10:08:34-12:04:12, ended cleanly (Session Summary printed, not
a crash-loop). Measured directly from the log, not inferred:

- **Zero exceptions** from either `note_padlock_center_dot` or
  `detect_terrain_ahead` the entire run.
- **286 "TERRAIN AHEAD" triggers.** Correlated each against the same-tick
  `padlock=` field on the following `BT[active]: selected=...` debug line:
  **285/286 show `padlock=False`** — the gate only ever let the read
  through when padlock was confirmed off.
- **The one apparent exception is not a contradiction.** Traced line-by-line
  (10:10:00.864-.954): the gate read a valid confirmed-`False` state and
  allowed the trigger at .876; `padlock_camera()`'s own routine ~6s press
  (ADR 136's `_padlock_loop`, unrelated to this gate) fired at .898 — 22ms
  later, same tick — and reset `padlock_state()` to `None` per ADR 140's own
  "any-press resets to Unknown" rule, before that tick's own end-of-tick
  debug line printed at .954. The gate's decision was correct at the moment
  it was made; the log field this analysis compares it against changed
  afterward, for a reason unrelated to the gate.
- **Open Question 1, answered.** Across 4690 in-battle ticks,
  `padlock_state()` was `False` on 3725 (79%) and `None` on 965 (21%).
  `True` never appeared even once this session — confirmed by code read
  that nothing in the codebase ever assigns it; in practice the tri-state is
  effectively binary (`False`/`None`) today. The gate suppresses roughly a
  fifth of ticks, not the majority-starving scenario the question worried
  about — `terrain_confirm_reads` (2) has ample room to accumulate on a
  real hazard.

This satisfies the Testing plan's live-validation step for the gate
mechanism itself. Still not done: comparing the resulting false-positive
*taxonomy* against HLDD 001's "Live trial results" baseline — a judgment
call about whether the firing rate changed in a way attributable to fewer
padlock-confused reads, which is a separate, later analysis, not required
to confirm D1/D2 are functioning as designed.

## Open Questions

1. **How often does `padlock_state() is not False` actually hold during
   ordinary play?** If the ~6s engagement cadence plus the unconfirmed
   window around each transition means this gate suppresses the terrain
   reading most of the time, `terrain_confirm_reads` (currently 2) may
   rarely accumulate even during a genuine hazard, and the trigger could
   end up structurally starved of clean reads rather than merely protected
   from corrupted ones. Worth measuring directly in the live trial below,
   not assumed either way. **Measured by the first live trial below**: 21%
   of in-battle ticks (`False` 79%, `None` 21%, `True` never observed) —
   not the majority-starving case this question worried about.
2. **ADR 140 Open Question 1 (padlock-ON ground truth) is "quantified but
   not closed."** This decision only needs `padlock_state()` to be a
   reasonable *coarse* signal for "don't trust the forward view right now,"
   not a precisely-calibrated one — the conservative gating direction (D1)
   tolerates some false suppression. But if that ground-truth gap is later
   closed and shows the detector tracking something other than intended,
   this gate's effectiveness needs re-checking too, not just ADR 140's own
   consumers.

## Testing plan

- **Unit** (done): `BehaviorTreeHandler.tick()`'s terrain-read call site
  now gates on `padlock_state() is False`, and `note_padlock_center_dot`
  runs first so the gate reads the current tick's value — see
  "Implementation status" above.
- **Live** (gate mechanism confirmed, taxonomy comparison still open): a
  shadow-mode session ran immediately after implementation — zero
  additional actuation risk, since the trigger still never actuates under
  D2 — see "First live trial" above for the gate-correctness measurement
  and Open Question 1's answer. Still open: comparing the resulting
  false-positive taxonomy against HLDD 001's existing "Live trial results"
  baseline.

## References

- [ADR 140](140-padlock-camera-state-detection.md) — the padlock-state
  detector this decision proposes as the first consumer of.
- `docs/hldd/001-terrain-avoidance-hldd.md` Open Question 6 — the gap this
  decision closes the wiring half of (not the re-graduation half; see D2).
- [ADR 141](141-phase1-altitude-floor-stall-prevention-and-emergency-yields.md)
  D2 — the same "don't let one trigger's corruption reach a different
  trigger's decision" reasoning, applied there at the emergency-signal
  layer (hard vs. broad), applied here at the perception layer instead.

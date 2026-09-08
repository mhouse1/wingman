# ADR 134 — Cruise Afterburner Above a Fuel Floor

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-07 | 1.8.8           |

## Context

Afterburner is currently only pressed inside three narrow windows: the
missile-evade alert hold (ADR 128), the climb tactic's fuel-floor hold (ADR
073/075), and the eject/dive descent. Outside those windows — the great
majority of a battle — the J20 flies at normal throttle even when fuel is
plentiful.

Operator observation: afterburner is mostly unused, and it should not be.
Sustained higher speed makes the aircraft harder for enemies to hit and more
responsive in a turn (a slow aircraft stalls and bleeds energy turning; a fast
one does not), and fuel spent keeping speed up in the meantime is fuel spent
usefully rather than sitting unused for the rest of the life.

The one constraint on "just hold it more": fuel must never be driven to zero
by this. There is no separate boost/reheat meter — fuel is the only resource
(`Analyzer.get_afterburner_fuel_pct()`), and climb/evade both already depend
on having some left when they need it (`climb.fuel_reserve_pct`,
`fuel.rearm_margin_pct`).

## Decision

**D1. Hold the afterburner whenever fuel is above `min_fuel_pct` (40%) during
battle**, provided nothing higher-priority already owns the key.

**D2. Tree-independent, ticked every cycle — not a Selector leaf.** The
behavior tree's priority order ends `... → Engage → AttackSupport`, and
`AttackSupport` is the fallback that is normally RUNNING whenever nothing else
preempts it; a leaf inserted below it would almost never tick. Cruise doesn't
need exclusive control of the tree anyway — it only touches one key, not
pitch/roll — so it runs the same way ADR 128's alert hold does:
`Controller.note_afterburner_cruise()`, called unconditionally every tick from
`tick_handlers.py`, next to `note_incoming()`.

**D3. Skip, don't preempt, when eject, missile evade or climb already owns the
key.** `is_ejecting()`, `is_missile_evading()`, `is_climbing()` and
`is_afterburner_evading()` are checked before pressing; if any is true, cruise
does nothing that tick rather than fighting that tactic's own press/release
schedule. The tick immediately after that tactic finishes, cruise picks the
key back up on its own if fuel still supports it — no coordination needed
beyond "don't act while someone else is."

*(Superseded by D9, 2026-09-07: deferring to "is that tactic active" turned
out to defer to more than intended — see D9.)*

**D4. Release at the floor, re-arm once fuel recovers above it — no separate
margin needed.** Revised after live observation contradicted the original
draft here. The draft assumed fuel only decreases in-mission and so a release
would never re-arm before the next life; the live log disproved that within
the first battle (14:38-14:47): fuel dropped to 31%, released, recovered to
44% while the key was up, and cruise re-engaged — ADR 075's own mechanic
("the game recharges fuel only while the key is UP") applying to this hold
exactly as it already does to climb's. The code never encoded the wrong
assumption — `note_afterburner_cruise` just re-checks `fuel > min_fuel_pct`
each tick with no latch — so the observed re-arming is the code behaving
correctly despite the ADR's stated reasoning being wrong. The "never hits 0%"
property still holds, but from the threshold check itself, evaluated fresh
every tick: cruise never presses while fuel is at or below the floor, so it
cannot be the thing that drives fuel through it, however many times a life
crosses back and forth.

**D5. Debounce the release with `confirm_reads` (2), not the engage.** A
single noisy low OCR read must not release the hold; `confirm_reads`
consecutive readings at or below the floor are required. No debounce is
needed going up — the fuel gauge only ever falls, so a false engage above the
floor corrects itself on the very next real reading.

**D6. A stale/missing fuel reading holds the current state rather than acting
on it.** `_read_fuel_pct()` returning `None` (OCR miss, staleness) neither
presses nor releases — flapping on absent data is worse than doing nothing
for one tick.

**D7. Manual takeover is already covered generically, no new wiring
needed.** `release_for_manual_takeover()` blind-releases every key in
`INJECTABLE_KEYS` on `GameEvent.MANUAL_TAKEOVER`, and `AFTERBURNER_KEY` is
already a member. `note_afterburner_cruise()` only needs to gate its own
*pressing* on `not self._manual_takeover_active()`, so it does not re-press
into the gap between the operator's key press and the FSM event.

**D8. Re-arm at a real recharge level (`rearm_fuel_pct`, 90%), not just above
the floor.** *(Added 2026-09-07, corrects D4.)* D4 re-checked one shared
threshold in both directions: release at 40%, and — per D4's own live-fix —
re-arm as soon as fuel next read above 40%. Live logs from the first full soak
showed exactly what that produces: release at ~30%, re-engage at ~44-51%
minutes later, a rapid shallow oscillation that barely leaves the floor before
diving back through it. That is not the design the operator asked for. The
explicit spec: *"use afterburner until 40% fuel remains, let it recharge to
90% then activate again."* Two thresholds, not one — a real hysteresis band.
`min_fuel_pct` (40%) still gates the release, debounced by `confirm_reads`
(D5), unchanged. `rearm_fuel_pct` (90%) now gates the *engage* check when not
already active, replacing D4's `fuel > min_fuel_pct`. At battle start or
straight after a respawn, fuel is 100% — already above 90% — so the first
engagement each life is immediate, same as before; the difference only shows
up on re-arm, where the aircraft now has to actually recharge most of the way
back up before afterburner resumes, rather than grabbing it back the moment
it clears the floor. This is also what actually delivers D1's original intent
— fuel should be *spent*, not idling near-full with the burner available and
unused, which is what a low re-arm threshold was silently allowing.

**D9. Override climb, missile evade and eject — stop deferring to them.**
*(Added 2026-09-07, supersedes D3.)* D3's "skip while that tactic is active"
assumed the tactic itself was holding the key whenever it was selected. Live
log, 2026-09-07 18:14-18:26: fuel sat at 100% for roughly a third of the
session because Climb kept re-selecting near the ground (repeated respawns at
low altitude) and blocking cruise even during Climb's OWN non-afterburner
phases — Climb has its own fuel floor/rearm logic and doesn't hold the key
continuously either, so "Climb is selected" and "Climb is holding the key"
are different facts, and D3 conflated them. The gap this left was exactly the
"fuel idling near-full unused" problem D1 exists to fix.

The operator's call, put plainly: *speed should stay up regardless of what
tactic is active, eject included* — deliberately overriding eject's own
dive-descent afterburner gating (ADR 069), not just climb and evade's. This is
a real trade-off, stated for the record rather than smoothed over: an eject
sequence's controlled descent may now fight a cruise re-press within one tick
of the descent controller releasing the key for its own reasons. Accepted
knowingly, not overlooked.

Mechanically this means cruise can no longer rely on becoming active exactly
once per hold (D1-D8's model: press on the false-to-true edge, release on the
true-to-false edge). It now re-asserts the press every tick it may hold —
harmless when the key is already down, and the only way to win it back within
one tick when climb, evade or eject releases the same key for their own
reasons. The four `is_*_active` checks D3 introduced are gone from the
`may_hold` gate entirely.

**D10. `GAME_BATTLE_EJECT` and `mission_running` needed the same exception as
D9, or the override was cosmetic.** *(Added 2026-09-07.)* Removing
`is_ejecting()` did not actually let cruise run during an eject: the FSM
transitions to a distinct `GAME_BATTLE_EJECT` state on `eject_started` (not
`GAME_BATTLE`), and `mission_running` reads False throughout — the mission
thread isn't running during eject, which is not new or eject-specific,
`fire_eject`'s own actuator wiring was never gated on `mission_running`
either. Live-observed the same session: `may_hold` still evaluated false
during eject, cruise still logged "yielded control" right as eject started,
D9's own removal of the four tactic checks notwithstanding. The gate is now
`not manual_takeover and ((mission_running and GAME_BATTLE) or
GAME_BATTLE_EJECT)` — `GAME_BATTLE_MANUAL` stays excluded (that one is
`_manual_takeover_active()`'s job, checked independently, so widening the
state check does not weaken SAF-001).

## Consequences

Fuel burns faster overall — that is the intended trade, spending a resource
that was otherwise sitting unused for most of a life. The reserve below 40%
is left for climb and evade, so a life that spends down to the cruise floor
should still be able to climb and evade near the ground.

Cruise, climb-hold, evade-hold and eject's descent controller now all drive
`AFTERBURNER_KEY` on independent schedules with no deference between them
(D9) — a stronger version of the interaction ADR 128 already accepted (D6
there: "climb ending mid-alert would cut the burn silently", fixed by
re-pressing). Cruise re-presses every tick it may hold, so it is now the one
most likely to win a given tick's contest for the key, not the one that backs
off. The specific, named cost: eject's ADR 069 dive-descent control cuts the
burner deliberately as part of managing descent rate, and cruise's re-press
can put it back within a tick. First evidence (see V-D9/D10-live): 267 eject
sequences in one overnight soak, all terminating in an expected way
(respawn_detected, rearmed, established, or no_telemetry — no anomalous or
stuck outcome), 0 manual takeovers, 0 errors. That is reassuring, not
conclusive — the descent-control code doesn't distinguish "released cleanly"
from "released, then cruise put it back and something compensated," so a
subtler interaction (a slightly shallower dive angle, say) would not show up
as a distinct log line. Worth a closer look if eject behavior ever looks off,
since eject failures are the highest-consequence failure mode this codebase
has (SAF-004 territory) — but nothing in this soak points at one.

**Whether it helps is an open question**, the same way ADR 128's does:
whether the survivability gain from sustained speed outweighs the wider turn
radius a faster aircraft flies, and now also whether it costs anything at
eject time. Live observation decides both.

## Validation

- **V1.** Fuel above the floor, in battle, nothing else active — the key is
  pressed.
- **V2.** Fuel at or below the floor for `confirm_reads` consecutive readings
  — the key is released, and stays released for as long as fuel stays at or
  below the floor.
- **V2-live.** Confirmed 2026-09-07 14:38-14:47: multiple engage/release
  cycles within single lives as fuel crossed the floor both ways (recharging
  while released, per ADR 075), e.g. released at 31%, re-engaged at 44%,
  released again at 26%. Re-arming within a life is expected, not a defect —
  see D4.
- **V2-live undershoot.** 14:48:57-14:49:09: engaged at fuel 74% while the
  aircraft coasted nose-up (+90°, residual attitude after Climb had already
  handed off) and did not release until fuel read 2% — the floor still held
  (never reached 0%, recovered to 47% 4s later) but with a much thinner
  margin than the 26-44% band seen elsewhere. `confirm_reads` debounces
  against OCR noise by design (D5), and under a fast real drain that same
  debounce plus the fuel-OCR read cadence can let cruise ride well past the
  floor before releasing. One occurrence in one session — not enough to
  change `min_fuel_pct` or `confirm_reads` on this evidence alone, but worth
  watching: if this recurs, the fix is likely tightening the debounce or
  widening the floor's margin, not removing the debounce (a single noisy read
  falsely releasing the hold is the failure D5 exists to prevent).
- **V2-D8.** Fuel between `min_fuel_pct` and `rearm_fuel_pct` (the hysteresis
  gap) does not engage when not already active. Fuel at or above
  `rearm_fuel_pct` engages. A release followed by fuel recovering only into
  the gap stays released; recovering to `rearm_fuel_pct` re-engages.
- **V3.** A single low reading does not release the hold.
- **V4 — superseded by D9.** Originally: `is_ejecting()` / `is_climbing()` /
  `is_missile_evading()` / `is_afterburner_evading()` true meant cruise did
  not press, and released if already holding. D9 reverses this: none of
  those four are checked anymore, and cruise presses (and re-presses every
  tick) regardless of them.
- **V4-D9.** With `is_ejecting()` / `is_climbing()` / `is_missile_evading()` /
  `is_afterburner_evading()` each true in turn, fuel above the rearm
  threshold, cruise still engages and keeps pressing every tick.
- **V4-D10.** `game_state=GAME_BATTLE_EJECT`, `mission_running=False`, fuel
  above the rearm threshold — cruise still engages. D9 alone did not achieve
  this; D10 was needed too (live-observed 2026-09-07).
- **V5.** Outside `GAME_BATTLE`, or `mission_running` false — the key is
  released and stays released. Manual takeover releases an existing hold and
  blocks a new one — the one deferral D9 keeps, since it is not tactic state.
- **V6.** A `None` fuel reading leaves the current hold/release state
  unchanged, but still re-asserts an existing press.
- **V9-press-every-tick.** While holding, `_climb_key(AFTERBURNER_KEY,
  press=True)` is called once per tick, not only on the engage transition.
- **V-D9/D10-live.** Overnight soak, 2026-09-07 19:33 to 2026-09-08 04:31
  (8h57m), 92 missions, 301 respawns, **267 eject sequences**, 0 manual
  takeovers. Zero `[ERROR]`/`Traceback` lines. 916 cruise engages / 916
  releases across the session — exactly balanced, no key left stuck. Sampled
  eject windows directly: cruise both engages and correctly floor-releases
  *during* `GAME_BATTLE_EJECT` (e.g. one eject at 19:38:34 shows fuel burning
  100→92→83→...→30 with a correct floor release at 30%, entirely inside the
  eject window) — confirming D10 actually fixed the gate, not just D9's
  intent. One OCR-quality observation, not a cruise defect: fuel readings
  during a fast dive were seen swinging implausibly (e.g. 32→45→57→70→2→96 in
  under 6 seconds) — almost certainly OCR noise under motion blur, not real
  fuel — and a spurious high read did trigger one re-engage on bad data. Fuel
  OCR reliability during violent attitude changes is a pre-existing telemetry
  concern (see the plausibility-filter precedent in ADR 069), not something
  D8's engage-side lacking a debounce (D5) caused — flagged for awareness,
  not treated as a fix owed here.
- **V7 — live.** Afterburner usage rate rises from its currently-observed
  near-zero baseline during normal (non-tactic) flight, without fuel-related
  eject/evade failures increasing.
  **First soak, 2026-09-07, 1h09m, 12 missions, 43 respawns, pre-D9 code:** 98
  engage/release cycles (98 held, 98 released — balanced, no key left stuck),
  75 released by yielding to a higher-priority tactic, 23 by hitting the fuel
  floor. Zero `[ERROR]`/`Traceback` lines in the session; ended by a normal
  operator Backspace stop, not a crash. Confirmed the mechanism ran cleanly at
  the frequency intended, but the 75/23 split is D3's old deferral behavior —
  not representative after D9, which removed the "yielded" path entirely.
  **Second soak, 2026-09-07 18:14-18:26, 12m10s, 5 respawns, still on D8
  (pre-D9) code:** fuel read 100% for roughly a third of all ticks, because
  Climb kept re-selecting near the ground after each respawn and blocked
  cruise even during Climb's own non-afterburner phases — the measurement
  that motivated D9. One clean cycle in this same log (100% to 27% to a
  correct wait-for-92%-before-re-arming) confirmed D8's hysteresis itself was
  already working; the gap was entirely D3's deferral.
  **D9 — not yet soaked.** The override behavior above (V4-D9) is unit-tested
  but has not yet had a live session. What to watch for: whether fuel-at-100%
  time drops as expected now that Climb no longer blocks cruise, and whether
  eject sequences show any descent-control anomaly from cruise's re-press
  fighting the dive controller (see Consequences).

## References

- ADR 128 — the direct architectural precedent (tree-independent, per-tick
  afterburner hold)
- ADR 070 — missile-evade manoeuvre
- ADR 073 / ADR 086 — climb tactic and its own afterburner fuel floor
- ADR 075 — afterburner fuel discipline (rearm margin, recharge-while-up)
- ADR 069 — eject dive-descent afterburner gating, overridden by D9
- SAF-001 / SAF-004 — manual takeover and eject-related safety requirements
- `wingman/controller.py` — `note_afterburner_cruise`, `is_afterburner_cruising`
- `tests/test_afterburner_cruise.py` — V1-V6, V2-D8, V4-D9, V4-D10, V9-press-every-tick

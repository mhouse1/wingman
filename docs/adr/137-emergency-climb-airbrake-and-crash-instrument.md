# ADR 137 — Emergency Climb Airbrake, Tightened Pulse Cadence, and a Crash-While-Armed Instrument

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-09 | 1.8.9           |

## Context

Live log review the same session (`wingman.log`, 17:09:53–17:10:28) found a
respawn caused by a genuine, uncontrolled ground impact — not ADR 136's
eject/heatdive path, which never engaged (`missiles=3` held constant, no
`MISSILES EMPTY` anywhere near it). Measured directly from `BT[active]`
lines:

| Time | Tactic | Altitude | Descent rate | Time-to-ground |
|---|---|---|---|---|
| 17:10:03 | Engage | 9547m | -80 m/s | 120s |
| 17:10:12 | Engage | 8152m | -280 m/s | 29s |
| 17:10:18 | Engage | 6276m | -419 m/s | **15s** |
| 17:10:20 | **Climb** | 6276m | -419 m/s | 15s ← forced climb takes over |
| 17:10:24 | Climb | 2901m | -807 m/s | **4s** |
| 17:10:28 | — | — | — | crash → respawn |

The ADR 086 emergency time-to-ground trigger (`recover_below_time_s: 20.0`,
`confirm_reads: 2`) fired exactly on schedule — `ttg` crossed 20s, confirmed
over two reads, and forced `Climb` at `ttg=15s`. That part is not a bug. The
dive kept steepening anyway: descent rate nearly doubled again (-419 → -807
m/s) in the ~4 seconds after `Climb` took over, and the aircraft hit the
ground roughly 9 seconds after the forced climb engaged, despite it pulling
up. Ground truth for what `Climb`'s actuator (`_run_climb_hold`,
`controller.py:3169-`) was actually doing in that window: the routine
pulse-and-observe pitch cadence (`NOSE_UP` held for `climb_pulse_s`, default
1.5s, then released and not reapplied for `climb_observe_s`, default 2.5s)
and no braking of any kind — `AIRBRAKE_KEY` is never referenced anywhere in
the Climb actuator, emergency or not.

This is the same class of failure ADR 086 was written for (a prior 560 m/s
dive that outran the altitude-band trigger before the time-to-ground patch
existed) — this dive peaked even faster (-807 m/s) and evidently still
outran the recovery margin even with that patch in place, because the
*actuation* itself (routine cadence, no braking) was identical whether the
emergency trigger fired or not. The `emergency` verdict has existed since
ADR 086 (`climb.emergency_active`, `behavior_tree.py:496`) and is already
read by `BoundaryTurn`'s `yields_to_fn` — but nothing on the actuation side
has ever consumed it. Operator direction: when the emergency case takes
over, apply `AIRBRAKE_KEY` and get nose-up authority applying sooner, rather
than reacting with the same cadence as a routine altitude-band recovery.

Separately: this could easily have gone unnoticed — nothing today counts
"the aircraft crashed into terrain while it still had missiles" as its own
statistic, distinguishable from a deliberate eject-and-respawn (ADR
069/106/109's intentional trade). Operator direction: track this number in
the `Wingman Session Summary`, toward zero.

## Decision

**D1. `climb_mode`/`_run_climb_hold` gain an `emergency: bool` parameter**,
read once per `Climb` selection from the same closure `BoundaryTurn` already
reads (`AnalyzerSnapshot`-adjacent `climb.emergency_active`, via a new
`tree.climb_emergency_fn` attribute `build_tree` exposes and
`BehaviorTreeHandler._start_climb` (`tick_handlers.py`) now calls). No new
actuator-contract parameter was needed — `start_fn` still takes no
arguments; `_start_climb` just reads the flag itself before calling
`climb_mode(..., emergency=emergency)`.

When `emergency` is true, `_run_climb_hold` (`controller.py`) does two
additional things, both scoped to the emergency case only — the routine
altitude-band and sustain-band climbs are byte-for-byte unchanged:

1. **Holds `AIRBRAKE_KEY` instead of `AFTERBURNER_KEY`.** Pressed once at
   thread start, released unconditionally in the `finally` block (mirroring
   how `AFTERBURNER_KEY` is already released there). Revised same day
   (operator observation, 2026-09-09): airbrake and afterburner cancel each
   other out — drag fighting thrust — so the emergency case now suppresses
   afterburner **entirely**, not just at the initial press. The fuel-floor
   release/re-engage block and the ADR 088 incoming-missile override
   (`controller.py`, inside `_run_climb_hold`'s poll loop) are both skipped
   for the whole hold whenever `emergency` is true — `ab_held` never
   becomes `True`, so the later ADR 083 d3 above-target cut (which only
   acts `if ab_held`) is inert automatically, no extra guard needed there.
   This means an incoming missile during an emergency ground-collision dive
   no longer gets the ADR 088 afterburner-outrun response — accepted
   deliberately: outrunning a missile is moot if the ground arrives first,
   and holding afterburner would cancel the airbrake trying to prevent
   exactly that.
2. **Removes the `climb_observe_s` gap between pulses.** Normally a pulse
   ends and then nothing re-applies for `climb_observe_s` (2.5s default);
   in the emergency case, the next 0.25s poll tick immediately reassesses
   rate/ceiling and re-pulses. This is **not** the continuously-held nose-up
   the codebase has direct evidence against — ADR 069's own comment: "60s
   held, altitude oscillated 1650-2400 with zero net gain." Each pulse still
   ends and the rate/pitch-ceiling checks still run before the next one
   starts, so a recovered climb rate or a hit pitch ceiling still stops or
   reverses it exactly as before; only the idle waiting between pulses is
   removed.

**D3. Cruise-afterburner (ADR 134 D9) gets one narrow exception: an
emergency climb's airbrake hold.** First live trial of D1 (2026-09-09,
6.5h session) found D1 alone was not enough: `note_afterburner_cruise` is a
tick-level mechanism explicitly designed to override climb/evade/eject with
"no exceptions" (its own docstring), and it doesn't know about `emergency`
at all — measured directly in the log, it re-pressed `AFTERBURNER_KEY`
inside **10 of 18 (56%)** emergency-climb windows that session (e.g.
`CRUISE — afterburner held (fuel 100% >= 90%)` fired mid-dive at 18:22:46,
inside a window D1's own fix had already confirmed was airbrake-only from
Climb's side). Per the same physics D1 was built on, that press cancels the
airbrake's deceleration exactly as before — just through a path outside the
Climb actuator entirely, one this ADR's first draft never audited.

A new `Controller._climb_emergency_active` flag, set for the exact duration
of the airbrake hold (alongside the `AIRBRAKE_KEY` press/release in
`_run_climb_hold`'s start and `finally` block), is added to
`note_afterburner_cruise`'s existing `may_hold` gate — the same gate that
already yields to manual takeover. No new yield mechanism: an emergency
climb in progress makes `may_hold` false, and the function's existing
release-if-active-then-return path (unchanged) does the rest. This is a
**deliberate, narrow reversal of part of D9** — operator's explicit call,
weighing the two directives against each other rather than either being
silently overridden: D9's "no exceptions" now has exactly one, scoped to
the specific interaction this ADR exists to fix. D9's other overrides
(climb's own fuel-floor logic, missile evade, eject) are untouched.

**D2. A new session-summary counter: crashes while armed.**
`RespawnHandler.tick_detect` (`tick_handlers.py`) now captures
`ctrl.is_ejecting()` *before* `ctrl.stop_eject_sequence()` runs (which would
otherwise interrupt and clear it first) — a death mid-eject is the
deliberate ADR 069/106/109 trade, not a crash to count. If the aircraft was
**not** ejecting and `Analyzer.get_ammo_missiles()` reads `None` or `> 0`
at the moment the respawn is confirmed, a new event —
`"crash_with_missiles"` — is emitted through the existing
`_emit_capture_event` funnel (confirmed harmless for consumers that don't
recognize a name, the same way `"missiles_empty"` already flows through it;
no new constructor parameter needed). `MissionStatsTracker` counts it in
`_total_crashes_with_missiles`, folds it into `finalize()`'s summary dict,
and `print_summary()` prints it unconditionally (matching `total_respawns`/
`total_manual_takeovers`'s always-shown style, not the conditional
`spawn_crashes` block) — the number should be visible even at zero, since
zero is the target this instrument exists to confirm.

Ammo unreadable (`None`) counts as "still armed" — a fail-open choice for
this specific metric: the cost of a false positive here (one extra count on
an OCR miss) is far lower than the cost of a false negative (a real crash
silently not counted because the read happened to fail at exactly the wrong
moment).

## Non-Goals

1. **Not a fix to the ADR 086 trigger threshold itself**
   (`recover_below_time_s: 20.0`). The trigger fired on schedule; this ADR
   changes what happens *after* it fires, not when it fires. Whether 20s is
   still the right threshold given dives that can reach -807 m/s is a
   separate, unaddressed question.
2. **Not a change to the routine (non-emergency) Climb cadence.** The
   pulse-and-observe pattern and its documented anti-oscillation rationale
   are preserved exactly for the ordinary altitude-band and sustain-band
   cases — only `emergency=True` gets the tightened cadence and airbrake.
3. **Not a change to non-emergency afterburner behavior.** The routine
   altitude-band/sustain-climb fuel-floor and ADR 088 incoming-missile
   afterburner logic is completely untouched — the `elif`/skip only takes
   effect when `emergency` is true.
4. **Not a retroactive re-classification of prior sessions' respawns** —
   `total_crashes_with_missiles` starts counting from the session this
   ships in; there is no way to recover the classification for past runs
   from their saved JSON.
5. **Not every D9 override.** D3 removes exactly one — climb-emergency.
   Evade and eject still win the key from cruise unconditionally, and
   climb's own fuel-floor logic outside the emergency case is untouched.

## First live trial (2026-09-09, 6.5h session, D1 only — D3 not yet run)

`Crash w/ missiles` printed **40** (of 183 total respawns) — the first real
baseline for the instrument D2 built. Read carefully: this is a
*crashed-while-armed* count, not a *climb-recovery-failure* count — it
counts every respawn where the aircraft wasn't ejecting and still had
missiles, which includes ordinary combat deaths (shot down while flying
level) alongside any climb/ground-collision failures. It should not be read
as "40 climb failures" — see Open Question 3.

Separately, of the 18 emergency climbs that session, none ended with the
aircraft crashing while Climb was still actively running (exit reasons:
4 `altitude_recovered`, 7 `eject_preempt`, 4 `evade_preempt`, the rest other
housekeeping exits) — so this session doesn't show direct evidence of D1
itself failing to prevent a crash. It did surface D3's gap (cruise
interference in 10/18 windows), which is now fixed above but has no live
data of its own yet. The next session is the actual test of D1+D3 together.

## Open Questions

1. ~~Does holding `AIRBRAKE_KEY` while `AFTERBURNER_KEY` is also held
   actually reduce descent rate, or do they cancel out?~~ **Resolved same
   day** (operator observation, 2026-09-09): they cancel out. D1 now
   suppresses afterburner entirely during the emergency hold — see D1 and
   Non-Goal 3. Still open: whether airbrake *alone* (no afterburner at all,
   and now with cruise-afterburner also yielding per D3) measurably
   improves outcomes over the pre-ADR-137 baseline — that's the live trial
   this ADR still needs, now that D3 closes the gap the first trial found.
2. Is `recover_below_time_s: 20.0` still enough lead time given a dive can
   accelerate from -419 to -807 m/s in about 4 seconds? Worth revisiting
   once this ADR's own change has live data of its own to compare against.
3. Should `crash_with_missiles` also gate on flight state (e.g. exclude a
   death from enemy fire while level, which isn't a "dove into terrain"
   event at all)? Today's classifier only excludes deliberate ejects — a
   respawn from being shot down while flying level and armed would also
   currently count, even though it isn't a Climb/ground-collision failure.
   Left as specified (operator's own framing: "crashed while descending
   with missiles still available") rather than narrowed further without
   being asked to.

## Related Documents

- `docs/adr/073-*-climb-tactic*.md`, `docs/adr/075-*.md`,
  `docs/adr/076-*.md`, `docs/adr/081-*.md`, `docs/adr/083-*.md` — Climb
  tactic history this ADR extends.
- `docs/adr/086-*.md` — the time-to-ground emergency trigger this ADR's D1
  reacts to, not replaces.
- `docs/adr/069-eject-impulse-rotation-and-ballistic-descent.md`,
  `docs/adr/106-return-to-battle-rate-tracking.md`,
  `docs/adr/109-eject-yields-to-the-survival-hold.md` — the deliberate-eject
  framing D2's classifier excludes.
- `docs/adr/107-boundary-turn-tactic.md` — source of the
  `climb.emergency_active` / `yields_to_fn` pattern D1 reuses to read the
  emergency verdict outside the condition closure.

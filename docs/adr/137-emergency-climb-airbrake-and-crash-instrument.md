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

**D4. `recover_below_time_s` raised 20.0 → 30.0, `confirm_bypass_time_s`
scaled with it, 10.0 → 15.0.** Non-Goal 1 below said this was a separate,
unaddressed question — the second live trial (below) answered it with a
clean single-cause example, so it's addressed here rather than left for a
third pass. Measured 2026-09-10 07:24:42: the emergency fired correctly on
the very first qualifying read (19s to ground, well within the old 20s
band), airbrake engaged within 1.5s, nose-up pulses fired every ~1.5-1.8s
thereafter with no idle gap (D1's own mechanism working as designed) — and
the descent rate still **worsened**, -333 → -526 → -602 m/s over the next
4.5s, before the aircraft hit the ground about 10.5s after the first read,
1 missile still aboard. This is the same telemetry-lag mechanism
`recover_below_time_s: 20.0`'s own comment already documents (the original
12s→20s change, ADR 086 d2/d3/d4: "the smoothed altitude lags ~1500m in a
560 m/s dive, so 12s of PREDICTED margin was only ~4s of real margin") —
still present at 20s, just milder (roughly 1.8x compression here vs the
original 3x case), and still not enough real margin for airbrake+nose-up to
arrest a dive that has already reached -600 m/s by the time it's caught.
`confirm_bypass_time_s` moved from 10.0 to 15.0 to hold the same 50% share
of the window rather than silently changing the confirm-vs-bypass balance.

Known tradeoff, not yet measured: a wider trigger band means more ordinary
combat dives will cross it and force a Climb selection that wasn't needed —
the original 12s→20s change was itself a deliberate balance against this
same risk, and this ADR does not have live data yet on whether 30s shifts
that balance too far. Next session's `total_manual_takeovers`-style tactic
transition counts (Engage→Climb frequency) are the thing to watch.

**D4 live results (2026-09-10, same session as the third live trial
below)**: every emergency-climb trigger recovered cleanly except one —
2026-09-10 11:08:21-46, ttg 19s at first read (well inside the new 30s
band), airbrake+nose-up engaged immediately and correctly, rate improved
from -217 to -134 m/s but never reached positive, then the aircraft was
lost. This is **not cleanly attributable to D4** either way: from
11:08:42 the same total-OCR-blackout signature covered in the Third Live
Trial section below (every channel "no digits", including telemetry)
starts and runs through to the respawn — the "stuck" -134 m/s reading in
the BT debug log is the last cached value repeating while telemetry itself
went blind, not necessarily the real descent rate at the moment of impact.
Whether the emergency genuinely failed to arrest this dive, or whether it
was working and the blackout (of unknown cause, see Third Live Trial)
simply hid the outcome, cannot be told apart from logs alone — another
point the frame-capture instrument would resolve directly.

A second instance of the same overlap, 2026-09-10 13:03:07-21: ttg 11s at
first read, rate improving steadily under emergency control (-443 to -410
to -225 m/s) while altitude kept falling (4790m to 1097m), then the same
total blackout began at 1097m/-225m/-5s-ttg and ran through to the
respawn. Same ambiguity as the first — rate was trending toward recovery,
not away from it, when the blackout cut the log's visibility.

A third, 2026-09-10 14:11:12-21: this life had already recovered once from
a -998 m/s dive (the steepest of the session) and a follow-up -466 m/s dip,
both clean, before a third dip to -521 m/s recovered to +206 m/s — climbing
— and then telemetry itself went blind (`alt=None`) and the aircraft was
lost moments later. Same shape: blackout arriving while the trend was
positive, not negative. Net for the session by this point: well over a
dozen emergency-climb triggers recovered unambiguously (including several
from -400 to -998 m/s dives), 3 ended ambiguously inside the universal
blackout while rate was improving or already positive, 0 ended in an
unambiguous D4 failure. The pattern across all three ambiguous cases is
consistent enough to be worth stating plainly: nothing in this session's
data indicates D4 itself failing to arrest a dive — every case where the
outcome is legible ends in recovery, and every inconclusive case is
inconclusive because of the same blackout, not because of a falling rate
at the moment visibility was lost.

**Correction, same day, after D5 shipped and started capturing frames**:
the claim above — "0 ended in an unambiguous D4 failure" — was true only
because visibility was the limit, not because the cases were actually
clean. D5 (below) resolved one of them by inspection. 2026-09-10
17:41:45-56: `DIVE RECOVERY` fired promptly at ttg 22s (well inside the
30s band), airbrake held, nose-up pulses fired every ~1.7s with no gap —
D1's mechanism working exactly as designed — and the rate still worsened
the entire time: -287 → -586 → -742 m/s, angle pinned at -90° throughout,
the same signature as the original case D4 was built to fix. Telemetry
went blind at the critical moment, same as the three cases above, but this
time `crash_20260910_174156.png` (D5) shows the aftermath directly:
wreckage and smoke on the canyon floor, no kill-feed banner — a genuine
terrain impact, not survived combat. This is now **1 confirmed D4
failure**, not zero, and it strongly suggests at least some of the three
earlier "ambiguous, trending positive" cases might not generalize the way
the confident reading above assumed — they were reported honestly as
ambiguous, and should have stayed read that way rather than being rolled
into a reassuring summary sentence. The corrected picture: airbrake and
tightened cadence measurably help (most triggers now recover, including
several very steep ones), but they do not reliably arrest a dive that has
already reached the -580 to -740 m/s / -90°-saturated regime, even when
caught at 22s rather than 19s. Whether a still-higher threshold would
catch these before they reach that regime, or whether the mechanism
itself (airbrake-only, no afterburner) has a hard ceiling regardless of
when it engages, is the open question D4 leaves for a future pass — not
resolved by this ADR.

Also added: a per-occurrence log line at the `crash_with_missiles` emit
site (`tick_handlers.py`) — missile count and the same altitude/rate the
Climb tactic reads, at `WARNING` so it survives the default log level. The
stats counter alone is not enough to diagnose a recurrence; the second live
trial required manually correlating `RESPAWN DETECTED` lines against
`eject_and_dive`/`Climb` log evidence by hand to find this one example, in
a session that only reached 26 respawns. That does not scale, and the
`crash_with_missiles` metric only earns its keep as an instrument if the
*next* occurrence is a five-minute log read instead of another hour of
archaeology.

**D5. `RespawnHandler._capture_crash_frame` saves the tick's frame to disk
at every `crash_with_missiles` occurrence**, capped per session
(`mission.crash_capture.max_per_session`, default 20) rather than per
episode — unlike `UnknownAnomalyRecorder`, there is no recurring "episode"
concept to bound captures within, each occurrence is its own event. Built
because the Third Live Trial's own log-only evidence hit a wall it
couldn't get past: every OCR channel reads "no digits" identically whether
the aircraft is genuinely blind mid-flight or looking at its own
explosion/death screen, and that ambiguity showed up independently across
three different preceding tactics (missile evade, a stalled climb,
ordinary combat) and even overlapped an active D4 emergency-climb recovery
twice, making it impossible to say from logs alone whether D4 itself had
failed in those two cases or simply lost visibility of a recovery already
in progress. A saved frame settles this by inspection — a cockpit view and
an explosion effect are not ambiguous to look at, even though they are
identical to OCR.

Reuses the frame already passed into `RespawnHandler.tick_detect(frame, ...)`
— no new capture call, no new capture path, just persisting what the tick
already had in hand at the exact instant the instrument fires. Directory
(`test_screenshots/crash_with_missiles/`) falls under the existing
`test_screenshots/*` gitignore pattern, so it is local-only like
`unknown_anomalies/`, never committed. A write failure is caught and
logged, never allowed to break respawn handling — the same fail-safe
posture as every other instrument added this ADR.

**D6 (2026-09-11): `crash_with_missiles` also excludes a death while ADR 136
heatdive had switched to the secondary loadout.** Operator report: the
classifier was still counting some of these. Root cause — once heatdive
presses SWITCH_WEAPON, `AMMO_MISSILE` reads the secondary (heatseeker)
rack, not the primary one, so `missiles > 0` at the crash tick could mean
"2 heatseekers left," not "unused primary ordnance." `was_ejecting` alone
didn't catch every case: `Controller._eject_weapon_switched` (the flag
that already gates the ADR 088 rearm-abort check for the same reason) only
reset at the *start* of the next `trigger_eject_and_dive()` call, not on
respawn — so a life with no further eject in it could carry a stale `True`
from a previous dive for its whole duration, and conversely a dive whose
eject thread had already exited (`is_ejecting()` False) by the time of
death still had the flag correctly `True` and was *not* previously being
excluded.

Two changes, `controller.py`: a new public `is_secondary_weapon_active()`
accessor (mirrors `is_ejecting()`); `stop_eject_sequence()` — called on
every respawn and on match end, both moments the game itself restores the
primary loadout — now also resets the flag, closing the scoping gap rather
than only special-casing this one caller. `RespawnHandler.tick_detect`
reads it in the same spot and under the same ordering constraint as
`was_ejecting` (before `stop_eject_sequence()` clears it), and gates the
count on `not was_ejecting and not had_secondary_weapon_active`.

**D7 (2026-09-11): `RespawnHealthStallRecorder` — a frame captured when
health stays unconfirmed after a respawn long enough that `mission_j20`
never restarts.** Operator report of the plane flying straight after
respawn led to a log investigation (this session, separate from D4/D5):
9 instances found across today's sessions where the gap between `SPAWN
GUARD` and the mission restart ran 60-153s instead of the normal 3-6s.
All 9 shared two things — every one followed an ADR 136 heatdive death
(`eject_and_dive — cancelled during descent` / `eject heatdive loop
stopped` right at the respawn), and in every one health OCR read *zero*
digits for the rest of that life, not slowly but completely, until the
match itself ended (`GAME_BATTLE → GAME_END_B`) and rescued it. The
"flying straight" itself turned out not to be `SPAWN GUARD` (which
released normally within 1-3s via telemetry handoff in all 9 cases — its
90s cap never fired today) but the `AttackSupport` fallback tactic having
no enemy *or* friendly icon to steer toward right after a fresh respawn
(`rings=0/0/0`), so it issued no control input at all.

Per-tick OCR timing during the stalls (`Health OCR: 0.29-0.35s`,
`Total: 0.3-0.5s`, every tick) rules out thread-pool contention as the
cause — each health scan completed quickly and simply found nothing to
read. That points at the health crop's pixels themselves, not OCR
throughput, which is why the fix isn't "narrow OCR to health-only during
this window" (a reasonable-sounding idea, already precedented by
`GAME_STARTING`'s health-armed probe scanning HEALTH only — but the
timing evidence says it wouldn't shorten these particular stalls) — it's
first finding out *what the health crop actually shows* during one.

`HealthDropoutRecorder` (ADR 080 d2) already captures health OCR gaps,
but its `telemetry_hud_live()` gate explicitly excludes this exact window
as "a death/menu gap, not a dropout" — by design, not oversight, since
its purpose is in-flight dropouts during otherwise-normal play. D7 is the
complementary recorder for the window that gate excludes: fires when
`current_game_state == GAME_BATTLE` and `not ctrl.is_mission_running()`
and `analyzer.health_confirmed_gap_s()` has passed `capture_after_s`
(8.0s — comfortably past the normal 1-3s restart, well before the 60s+
extremes found). Capped and recaptured per session
(`max_per_session: 12`, `recapture_interval_s: 15.0`) the same way
`HealthDropoutRecorder`/`UnknownAnomalyRecorder` already are, and built
applying every D5-code-review lesson from the start: checked
`cv2.imwrite` return value, sequence/gap-suffixed filenames, `pathlib`
for path construction. Config: `health.respawn_stall_capture` in
`config_schema.py`/`config.yaml`, mirroring `dropout_capture`'s shape
exactly. Wired into `main.py`'s tick loop right next to
`health_dropout.tick(...)`.

**Settled within 20 minutes of deploying, 2026-09-11 04:37-04:54**: the
mystery isn't a detection bug at all. Three captures reviewed
(`stall_20260911_043702_gap8s.png`, `_044918_gap8s.png`,
`_045259_gap8s.png`), all identical in kind — the screen shows a live,
actively-filling **"RESPAWN N" countdown bar**, and two of the three show
an objective-capture banner (`B SECURED`, `C SECURED`) with `A`/`B`/`C`
capture points on the minimap. **The aircraft genuinely has not respawned
yet.** Health OCR was never failing or slow, and `is_alive` was never
stuck — both were correctly reporting "not alive" because there is, at
that moment, no living aircraft to read health from. This game mode
(objective/conquest-style, not plain deathmatch) has a materially longer
in-game respawn timer than the ~1.5-3s the codebase's existing timing
constants (`respawn_clear_stability_s: 1.5` and others) were tuned
around, and D7's own 8s `capture_after_s` threshold — chosen to sit
"comfortably past the normal 1-3s restart" — turns out to be *inside*
this mode's ordinary respawn countdown, not past it, which is exactly why
it fired 10 times in the first 20 minutes of the session that found this.

This reframes the original operator report: the aircraft flying straight
after respawn was not a wingman defect to fix — it was an unavoidable
consequence of a real, multi-second wait imposed by the game itself, with
nothing for the `AttackSupport` fallback tactic to do in the meantime
(Open Question 3 below is the closer-fitting home for *that* half of the
question, now that "why didn't mission_j20 restart" has a clean answer:
because the aircraft was not yet alive to restart it for). No code fix
follows from D7 itself — the instrument did its job, which was to stop a
plausible-sounding guess (OCR contention, a stuck detector) from being
chased. Whether `AttackSupport` should do something more useful than coast
during a known-long respawn wait is a separate, legitimate design question
this ADR does not resolve.

**Correction and retuning, same day**: the "objective-mode matches have a
longer respawn timer" theory above was built from only 3 screenshots that
all happened to be objective/conquest-mode matches — there was no
deathmatch-mode counterexample to confirm the game mode was the actual
differentiator rather than coincidence. Re-reading
[ADR 062](062-health-signal-respawn-detection-retiring-respawn-ocr.md)'s
own Phase A data (2026-08-01/02) found this was already measured, months
earlier and apparently mode-independently: *"OCR fires at overlay start
while health cannot return until the overlay clears ~8s later — live
matches landed at +4 to +8s."* The `Shadow respawn detector (ADR 062
Phase A)` block every session still prints at shutdown corroborates this
live, tonight: `fire_deltas_s` across 18-283 samples per session clusters
mostly in the same 2-13s range documented in 2026-08. **D7's 8.0s
threshold, chosen without checking this, sat at the documented normal
baseline rather than past it** — which is why it filled its 12-per-session
cap in the first 24 minutes, on ordinary respawns, not anomalies. This is
also why yesterday's `stop_eject_sequence` fix (D6) needed to check
`is_secondary_weapon_active` so carefully: the ~8s window is normal and
frequent enough that any logic gating on "respawn took a while" without a
tighter threshold will trip on it constantly.

The genuinely unexplained finding is unchanged and separate: the 9 extreme
outliers found earlier (60-153s, 8-20x the documented ~8s baseline, all 9
following an ADR 136 heatdive death) are not explained by ADR 062's normal
range. `capture_after_s` raised 8.0 → 30.0 (comfortably above the
documented normal range, well below the extreme outliers) so D7 only fires
on those going forward, instead of drowning in baseline noise.
`recapture_interval_s` widened 15.0 → 20.0 to spread the session cap
across more of a long episode's duration rather than exhausting it on one.
No frame from an actual extreme-range stall has been captured yet — that
is what this retuning is for.

**D8 (2026-09-11): the crash frame itself was captured too late to show
anything.** The fourth live trial's own analysis (above) found the cause
directly: `_capture_crash_frame` fires from `tick_detect` on respawn-
DETECTED, which is after the death/`KILLED BY` overlay has already replaced
the in-flight HUD. Measured, not inferred — every one of that session's 11
`crash_with_missiles` log lines read `None` for missiles, altitude, AND
rate, with no exception. The operator's own suspicion, stated directly:
"we are not actually capturing a crash."

`RespawnHandler` now keeps a small rolling buffer — `(frame, missiles, alt,
rate, timestamp)` sampled every tick while `GAME_BATTLE` and a mission is
running, evicted once older than `pre_crash_buffer_s` (default 8.0, chosen
to comfortably outlast the ~3s total-OCR-blackout-before-death signature
Open Questions 4/5 already document). A tick where neither missiles nor
altitude is readable is not buffered at all — which is exactly what
naturally happens once the death overlay takes over, so the buffer's
newest entry is, by construction, the last tick where the HUD was still
live. On a `crash_with_missiles` detection, both the saved frame AND the
logged missiles/alt/rate now come from that buffered sample instead of the
current (death-screen) tick — falling back to the old behavior only if the
buffer is empty (e.g. death on the very first tick after a mission starts,
before anything was ever buffered).

Config: `mission.crash_capture.pre_crash_buffer_s` (new key, default 8.0).
Memory cost is bounded and small — a handful of frames at most, evicted by
age, only while `crash_capture.enabled` and a mission is actually running.

Covered by `tests/test_tick_handlers.py::TestPreCrashBuffer` (13 tests):
buffering gated correctly on state/mission/enabled, skips unreadable ticks,
prunes by age, and — the core behavior — a crash uses the buffered frame
and numbers, not the death-screen ones, with a clean fallback when the
buffer is empty.

### Validation

- V1-V6 — unit, satisfied (see test list above).
- **V7 — live, MET AND FAILED, same day.** A 15:14-16:22 session ran the
  fix. `alt`/`rate` were real numbers in all 9 `crash_with_missiles` lines
  (`pre-crash frame, 0.0s old` on every one) — the buffer itself worked, and
  the age tag proved it. But `missiles` still read `None` in all 9, and both
  frames spot-checked were still `KILLED BY` panels, not the in-flight HUD
  this was built to capture. The buffer had reproduced the exact bug it was
  meant to fix, just with better bookkeeping.

**Root cause, found by checking `wingman/telemetry.py` rather than guessing:**
`TelemetrySignal.value` is a held-over last-known reading by design —
`TelemetrySnapshot.stale_after_s` defaults to 6.0s, so slower consumers
(the Climb tactic among them) can tolerate a brief OCR gap without treating
the aircraft as suddenly blind. `alt is not None` therefore does not mean
"the HUD shows this right now" — it means "an altitude was read at some
point in roughly the last 6 seconds," which is comfortably long enough to
span the moment a `KILLED BY` panel takes over the screen. `missiles`, read
straight from `get_ammo_missiles()` with no such holdover, correctly went
`None` the instant the HUD element it depends on was covered — which is
exactly why the two disagreed: the buffer kept a several-seconds-stale
altitude reading as "current" while missiles told the truth.

This is the same class of bug this project's own `iterate` skill already
carries a war story for — a `.fresh` check that has to be called, not just
tested for `None` — found here by reading `TelemetrySignal.age_s`/`is_fresh`
directly rather than assuming non-None meant live.

**Fix, same day.** `_sample_pre_crash_buffer` now computes
`snap.altitude.age_s(snap.taken_at_s)` and requires it under a new,
deliberately tight `pre_crash_freshness_s` (default 2.0s — about one tick,
nowhere near the general-purpose 6.0s tolerance) before accepting the
altitude reading at all. `missiles` needed no change — the data already
showed it behaving correctly. Two new regression tests pin the exact bug:
a stale-but-non-None altitude reading must not be buffered
(`test_does_not_buffer_a_stale_altitude_reading`), and a session where
altitude goes stale but an earlier missiles-only tick was genuinely live
must still save THAT frame, not the death screen
(`test_crash_capture_skips_the_stale_reading_and_prefers_missiles`).

### Validation (D8 freshness fix)

- V8-V9 — unit, satisfied (the two regression tests above).
- V10 — live, open. Still needs a session with the freshness gate live to
  confirm the saved frame is finally an in-flight HUD, not a death overlay.

## Non-Goals

1. ~~**Not a fix to the ADR 086 trigger threshold itself**
   (`recover_below_time_s: 20.0`)~~. **Superseded by D4** — the second live
   trial found a clean, single-cause example (below), so the threshold is
   now in scope for this ADR after all rather than deferred to a third pass.
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

## Second live trial (2026-09-10, two sessions, D1+D3 both live)

Two full sessions with D1+D3 live and no crash from an emergency-climb
window *during the emergency itself* (matching the first trial's finding —
see Open Question 1's status below): session A (05:00-06:19, 12 missions,
39 respawns) printed **7** `crash_with_missiles`; session B (06:48-07:42,
9 missions, 26 respawns) also printed **7**. 14 across 21 missions is a
consistent rate across two independent sessions (~0.67/mission), not a
single-session fluke — though still **measured**, not **inferred**, only
for session B, which was fully reconstructed below; session A's 7 were not
individually re-derived and are reported as the counter's own total only.

Session B's 7 were isolated by matching each `RESPAWN DETECTED` line
against whether an `eject_and_dive — cancelled during descent
(reason=respawn_detected)` line landed at the *same timestamp* (proof
`is_ejecting()` was still `True` at the instant `was_ejecting` was
captured, so D2's classifier correctly excluded it). The 19 respawns with
that adjacent line are deliberate ADR 069 ejects, correctly not counted.
The remaining 7 had no eject active at all — not a classifier gap, a real
crash — and split into three distinct patterns, not one:

- **1 dive-recovery failure** (07:24:42-53) — the D4 case above: emergency
  fired on time, descent rate worsened anyway, aircraft hit the ground with
  1 missile still aboard.
- **2 low-altitude stalled-climb deaths** (07:16:24, 07:23:43) — no
  emergency triggered at all (`alt_rate` +4 m/s and +0 m/s — not
  descending), Climb tactic selected at 554m/618m altitude and effectively
  static for several seconds (nose-up pulses logged `angle≈0°`, not
  climbing), then a sudden death with no boundary/incoming warning
  beforehand. Reads as an aircraft caught low and slow, unable to gain
  altitude, shot down while an easy target — not a ground-collision or
  dive-recovery failure at all. These two landed 68s apart in the same
  short stretch, suggesting a possible bad respawn/spawn-point interaction
  rather than two independent events, but that's **inferred**, not
  measured — no spawn-location signal exists to confirm it.
- **2 deaths immediately after a missile-evade release** (07:11:56,
  07:31:17) — both show `[Afterburner evade released]` followed by
  `MissileEvade`/`AttackSupport → RespawnWait` within 1-1.5s. Consistent
  with a second missile connecting right as evade ends, or a terrain/
  boundary issue during the evade maneuver itself — **not diagnosed**, the
  telemetry checked (boundary distance, incoming-region OCR) didn't show
  an obvious cause in either case.
- **2 deaths during a BoundaryTurn** (06:52:09, 07:25:30) — one at a
  moderate -61 m/s / 31s-to-ground (not urgent by any current threshold),
  one with a very high nearby-enemy ring count (16) on the same tick.
  Neither matches the ADR 138 boundary/mission-restart gap this session
  also validated clean — **not diagnosed** further here.

Only the first pattern is addressed by this ADR (D4). The other three are
recorded as open items below rather than guessed at — per the standing
"one change at a time" discipline, tuning a threshold for a pattern that
was never measured (e.g. weakening BoundaryTurn or reworking evade release)
would be applying force to an unmeasured lever.

## Third live trial (2026-09-10, D4 live, same session)

Watching D4 deploy live surfaced a gap D4 itself doesn't touch: **the
emergency flag is only read once, at `_start_climb` — the moment Climb is
newly selected while not already running (`ctrl.is_climbing()` false, the
ADR 070 `is_running_fn` gate).** If Climb is *already* selected and running
in the ordinary (non-emergency) band and the situation then deteriorates
into a `recover_below_time_s` crossing mid-hold, the BT condition still
logs `DIVE RECOVERY` and flips `climb.emergency_active` — but the
already-running actuator thread never rereads it, because nothing calls
`_start_climb` again while the same tactic stays selected. It keeps running
on whatever `emergency` value it started with.

Measured twice in one ~15-minute stretch of this same session:

- 08:15:00 — Climb selected non-emergency at 699m/+78 m/s since 08:14:59;
  `DIVE RECOVERY — 2s to ground` fired at 08:15:00.888 mid-hold (raw
  `Altitude: 11` logged the same second — the aircraft nearly touched
  terrain); no `CLIMB — EMERGENCY` line, the afterburner-fuel-floor log
  fired instead (afterburner-path-only, proof the hold was still
  non-emergency). Recovered anyway — altitude 11 → 1830m by 08:15:03.880,
  climbing at +608 m/s two seconds later.
- 08:16:14 — same shape: Climb already selected continuously since
  08:16:10 (600m, climbing), `DIVE RECOVERY — 2s to ground` fired
  08:16:14.939 mid-hold, no emergency escalation. This one ended in
  `climb complete (state_exit, 26.0s)` — a game-state exit, not a crash or
  a recovery either way.

Neither produced a `crash_with_missiles` count, so **this gap has not yet
been shown to cause a crash** — flagged as a real architectural hole, not
as the explanation for any of the second trial's 7. Left unfixed this pass:
the two live instances both resolved without incident, and the correct fix
needs its own design pass — either the actuator's own poll loop (already
running every 0.25-1s, already reading telemetry) also rereads
`self._climb_emergency_fn()` each tick and escalates a live hold in place,
or `_start_climb`'s dispatch gate changes to re-fire when emergency status
changes even while `is_climbing()` is true — the second option risks
restarting a healthy climb thread on every rate wobble near the threshold
and needs the same hysteresis discipline `make_climb_condition` already has
for the selection edge itself. Both need validation this session's data
doesn't provide, since neither trigger produced a failure to compare
against the D4 case (which by contrast *did* get the emergency treatment
correctly — a fresh Engage → Climb selection, not a continuation, so the
`_start_climb` gate wasn't in play there).

## Fourth live trial (2026-09-11, 3h41m, D4+D6+D7 all live)

07:37:55-11:19:19. Clean session throughout: zero `[ERROR]` lines, zero
tracebacks, normal shutdown sequence (game closed, nested display closed, no
watchdog needed — unrelated ADR 121 work landed the same session but never
had cause to fire here).

**39 missions, 113 respawns, 11 `crash_with_missiles`** (9.7% of respawns) —
in line with the last validated baseline (10.5%), not a clear move either
way on its own.

**Visual review of 5 of the 11 D5 crash-frame captures** (not all 11 —
labelled accordingly): 3 were unambiguous combat kills, each a full-screen
`KILLED BY [enemy name]` panel naming the aircraft and weapon, each at full
health at the moment of death (280/280, 286/286, 312/312 — an instant kill,
not attrition):

- 07:43:23 — `[RiCo] ShadowHawk`, Mirage 2000, Super 530D
- 10:09:45 — `[A∀²] Ringo`, J35 Draken, RB 27 Super Falcon
- 11:08:54 — `[FXB¹] Everest`, F-14 Tomcat, AIM-54 Phoenix

One (10:34:56) shows an `EJECTED` banner with no killer named, immediately
after a `LOST THE LEAD` banner — and the log around it rules out a wingman-
initiated eject as the cause: the mission's *actual* `eject_and_dive`
happened a full 79s earlier (10:33:37, missiles-empty triggered, completed
and cancelled cleanly by 10:33:58 with no `crash_with_missiles` count — D6's
exclusion worked correctly for that one), the aircraft respawned and
restarted its mission normally at 10:34:09, then died again 47s later with
no second eject anywhere in the log. An unattributed `EJECTED` death with no
enemy named and no wingman eject in progress is the closest thing in this
sample to the instrument's own namesake — plausibly a genuine unintended
terrain/self-destruction event, though "plausibly" is as far as one frame
plus a log trace can go without an altitude reading at the moment (see next
paragraph for why that reading is missing).

One (11:07:41) is not a death screen at all — it is a live, active in-flight
HUD frame: 854m, 1097 kph, missiles visibly 4/4 and 2/2 in the HUD, nose
pointed near a mountain peak close ahead. The `crash_with_missiles` log line
for this same timestamp reads `None missile(s), alt=None rate=None` despite
missiles clearly being loaded on screen — the frame capture and the
telemetry/ammo reads it logs alongside are not the same sample: **all 11 of
this session's instances logged `None` for missiles, altitude, and rate,
with no exception**, which is consistent with the capture firing at the
instant a `KILLED BY`/`EJECTED` overlay takes over the screen — exactly when
the in-flight HUD elements the OCR reads depend on have already been
replaced. This is very likely why every reading came back `None` rather than
a new regression: not "the sensors broke," but "there is nothing left to
read once the death screen is up," for 10 of 11 instances — the 11:07:41
frame is the outlier precisely because it was captured *before* that
overlay appeared.

**D7 fired once** (08:52:28, gap 31s, frame 1/12) — and the captured frame
is a post-match results screen (`2ND`/`MVP`/`3RD` leaderboard, "Click to
Continue..."), not an in-flight stall. `current_game_state` was apparently
still read as `GAME_BATTLE` during the results screen for D7's gate to have
fired at all. This is a false positive for the specific failure D7 was built
to catch (the earlier "flew straight after respawn without restarting
mission_j20" bug) — recorded as a new open question below rather than fixed
here, since narrowing D7's gate needs its own look at why the FSM state read
stale through a results screen.

## Open Questions

1. ~~Does holding `AIRBRAKE_KEY` while `AFTERBURNER_KEY` is also held
   actually reduce descent rate, or do they cancel out?~~ **Resolved same
   day** (operator observation, 2026-09-09): they cancel out. D1 now
   suppresses afterburner entirely during the emergency hold — see D1 and
   Non-Goal 3. Still open: whether airbrake *alone* (no afterburner at all,
   and now with cruise-afterburner also yielding per D3) measurably
   improves outcomes over the pre-ADR-137 baseline — that's the live trial
   this ADR still needs, now that D3 closes the gap the first trial found.
   **Partially resolved by the second trial**: across both sessions, no
   crash landed *during* an active emergency-climb window (the one dive-
   recovery failure found instead ended when the emergency's own hold
   phase reached its natural exit and the aircraft was already committed —
   see D4). Airbrake-alone isn't shown to be ineffective by this data; the
   open question narrows to D4's own new tradeoff (below).
2. ~~Is `recover_below_time_s: 20.0` still enough lead time given a dive can
   accelerate from -419 to -807 m/s in about 4 seconds?~~ **Resolved by the
   second trial**: no, raised to 30.0 in D4. New open question in its
   place: does 30.0 hold, or does the same lag mechanism reassert itself at
   the new threshold on a steeper dive? And does the wider band increase
   false-positive Climb selections during ordinary combat dives — flagged
   as an explicit unmeasured tradeoff in D4, watch `Engage → Climb`
   transition frequency next session.
3. Should `crash_with_missiles` also gate on flight state (e.g. exclude a
   death from enemy fire while level, which isn't a "dove into terrain"
   event at all)? Today's classifier only excludes deliberate ejects — a
   respawn from being shot down while flying level and armed would also
   currently count, even though it isn't a Climb/ground-collision failure.
   Left as specified (operator's own framing: "crashed while descending
   with missiles still available") rather than narrowed further without
   being asked to. **Sharpened by the second trial**: the low-altitude
   stalled-climb pattern (2 of session B's 7) is exactly this case —
   Climb tactic engaged, not descending, shot down while slow and low. It
   is real signal (an aircraft that can't get away is also a problem worth
   tracking) but a genuinely different failure than a dive the tactic layer
   should have recovered from, and D4 does not address it. **A third
   instance, live, 2026-09-10 10:47:28-34**, same D4-deploy session: Climb
   plateaued at 1306m/+139 m/s (roll suppressed by the ADR 132 turn guard)
   and held that exact reading for 3+ seconds while every OCR channel went
   blind, then respawned — same total-blackout signature as the evade
   pattern below, but with no `Afterburner evade released` line anywhere
   nearby, so it is not the same mechanism.

   **A fourth context, live, 2026-09-10 10:52:01-04**, same session: plain
   combat, no evade, no stall — `Engage → AttackSupport` transition during
   an ordinary roll-and-fire exchange, then the identical ~3s total OCR
   blackout, then respawn. Three preceding contexts now (evade-release,
   stalled-climb, ordinary combat) share one identical signature with no
   common tactical trigger between them. The simplest explanation
   consistent with all of it: **the blackout is not diagnostic of any one
   tactic-layer condition — it is what a death/explosion sequence looks
   like to every OCR channel, appearing identically no matter what killed
   the aircraft.** If that holds, "what causes the OCR blackout" is the
   wrong question — the blackout is the symptom of dying, not a cause of
   it — and the real, still-open question per instance is just "what
   in-game event ended this life" (a missile connecting despite the evade
   burn, enemy fire while stalled and slow, enemy fire during ordinary
   combat), which is exactly what a frame captured at the blackout's start
   would show directly instead of requiring more log inference. This
   reframes Open Question 4 and this item as **one** instrument need, not
   three separate mysteries — recommending it once, here, rather than
   under each. **Built as D5** (same session, same deploy) — captures on
   the `crash_with_missiles` tick itself rather than at evade-release
   specifically, so it covers all three preceding contexts uniformly.

   **Settled by the first three captured frames, 2026-09-10 16:12-16:13**
   (deployed minutes earlier, all three landing within 90 seconds of each
   other): every one shows the ordinary death sequence, not a blind
   cockpit — **the "OCR blackout" is what dying looks like on screen**,
   not a wingman perception defect. Every HUD element (ammo, fuel, health)
   reads "no digits" during it for the mundane reason that none of them
   are on screen at that moment; an explosion or a respawn overlay is.
   This resolves the "perception gap vs. ordinary death screen" question
   for good — it is the latter.

   But the three frames are not all the *same* kind of death, which
   matters for what comes next: `crash_20260910_161241.png` and
   `_161316.png` show a fresh explosion fireball and (in the second) an
   explicit `DESTROYED <enemy name>` kill-feed banner — a combat loss, shot
   down by another aircraft. `_161333.png` shows no kill-feed banner and a
   smaller, already-fading smoke plume framed directly against a canyon
   wall — consistent with a genuine terrain strike, not an enemy kill. So
   `crash_with_missiles` is a **mix**: some fraction is ordinary combat
   losses (not a wingman defect, not further pursued here), and some
   fraction is real "dove into terrain while armed" events — the exact
   thing this instrument was built to track toward zero (D2). The kill-feed
   banner's presence or absence is a fast, human-readable way to sort
   future captures into these two buckets without needing OCR: worth
   reviewing as frames accumulate (up to 20/session, D5) to find what
   fraction is actually terrain, and whether the terrain ones cluster
   around a common cause the way the dive-recovery pattern did.

   A fourth frame, `_161930.png`, minutes later: the game's own
   `KILLED BY` recap panel, naming `[USN] BOB`, an F/A-18 Hornet, weapon
   `AIM-7 Sparrow` — a fully attributed combat kill, as unambiguous as this
   gets.

   Two more spot-checked out of the accumulating set: `_165159.png` —
   `Dauntless`, F-14 Tomcat, AIM-54 Phoenix, full health (336/336) in one
   hit; `_171347.png` — `[IAF] VariableK`, F-4 Phantom, AIM-7E Sparrow,
   plus an assist credit. Both fully attributed combat kills. Running
   tally across seven spot-checked frames: 5 unambiguous combat kills (all
   with a full `KILLED BY` panel naming the enemy pilot, aircraft, and
   exact weapon), 1 clear terrain strike (`_161333.png`, no kill-feed,
   smoke against a canyon wall), 1 ambiguous canyon shot with no kill-feed
   visible (`_162856.png` — could be either). Consistent with combat
   losses being the clear majority, not a rare exception, though the true
   terrain fraction needs a full pass over all captures (up to 20/session)
   rather than this spot check to pin down precisely. Not narrating every
   further capture here — the picture is established; a full tally is
   future work, not a per-frame ADR update.

   **Five more spot-checked from the fourth live trial session (2026-09-11,
   see above)**: 3 unambiguous combat kills, same signature as every prior
   one (full `KILLED BY` panel, full health at death, named enemy/aircraft/
   weapon) — `07:43:23`, `10:09:45`, `11:08:54`. 1 likely terrain/self-
   destruction event with no enemy attribution (`10:34:56` — `EJECTED`
   banner, no killer named, no wingman-initiated eject anywhere in the
   preceding minute of log — see the trial section above for the full
   trace). 1 not a death frame at all — a live in-flight HUD shot near a
   mountain (`11:07:41`), ambiguous in a new way: it shows the moment
   *before* whatever killed the aircraft, not the aftermath, so it cannot
   itself be classified as combat or terrain. Running tally across twelve
   spot-checked frames: 8 combat kills, 2 terrain/unattributed, 2 ambiguous
   (one canyon shot with no kill-feed, one pre-death in-flight frame).
   Combat losses remain the clear majority. New in this batch: **every
   `crash_with_missiles` log line across all 11 of this session's instances
   read `None` for missiles, altitude, and rate** — the capture fires at
   the instant a death/results overlay has already replaced the in-flight
   HUD the OCR reads depend on, which is very likely why the instrument's
   own log line carries no usable telemetry alongside the frame it saves.
   That is a property of *when* the capture fires relative to the overlay,
   not evidence of a new sensor regression.
4. What causes a death within 1-1.5s of a missile-evade release ending
   (2 of session B's 7)? Not diagnosed — boundary distance and incoming-
   region OCR showed nothing unusual in either case. Needs either a wider
   telemetry window around evade-release specifically, or a dedicated
   instrument (e.g. logging incoming-detector state across the release
   moment) before a fix is more than a guess. **A third instance, live,
   2026-09-10 08:25:50-53** (same D4-deploy session), adds a concrete
   candidate mechanism: every OCR channel (ammo, fuel, health, telemetry)
   read "no digits" for the full ~3s spanning `MissileEvade → Climb` up to
   the respawn — the aircraft was perceptually blind through exactly the
   window it needed to notice a threat. The new D4 crash-instrument log
   line itself confirms this: it printed `None missile(s), alt=None
   rate=None` — not a bug in the log line, direct evidence the analyzer had
   nothing to read at the moment of death. Still not a confirmed root
   cause (why the OCR blackout happens at that specific moment — camera
   motion, an on-screen effect, something else — is unexamined), but it
   reframes the question from "what in the tactic layer is wrong" to
   "why does perception go blind right as evade hands off," which is a
   narrower, more tractable thing to instrument next.

   **A fourth instance, live, 2026-09-10 08:36:10-13**, same session,
   ~10 minutes after the third: identical shape down to the OCR channel —
   `MissileEvade → AttackSupport`, every OCR field ("no digits") through
   the whole gap, `Afterburner evade released after 5.5s` at 08:36:12.959,
   respawn confirmed 0.46s later.

   **A fifth instance, live, 2026-09-10 09:49:27-30**, ~74 minutes later,
   same session: `MissileEvade → Climb` this time (not `AttackSupport`),
   same OCR-blind gap, `Afterburner evade released after 7.0s` at
   09:49:29,840, respawn confirmed 0.49s later. Three independent live
   occurrences in one ~4.5h session plus the two from session B's
   archaeology makes this the best-corroborated of the three undiagnosed
   patterns (5 instances, one session) — clearly more frequent than the
   low-altitude-stall or BoundaryTurn patterns (2 each, no live repeat) and,
   on this session's count alone, now the single largest identified
   contributor to `crash_with_missiles` — larger than the dive-recovery
   pattern D4 fixed. Worth prioritizing next over any further D4 tuning.
   The shared signature across all five (evade release → total OCR
   blackout → death within ~1-2s, regardless of which tactic evade hands
   off to) is specific enough that a targeted instrument (e.g. saving the
   frame at evade-release time, the way
   `unknown_anomalies/` already does for boundary crossings) should be
   able to show what's actually on screen during the blackout, rather
   than guessing.

   **A sixth instance, live, 2026-09-10 10:05:37-40**, ~16 minutes after
   the fifth: same shape again, `Afterburner evade released after 7.0s`,
   respawn ~2s later. Checking the actual mechanism
   (`Controller._start_afterburner_evade`, `controller.py:2787`, ADR 128)
   changes the read on all six: **this evade is a pure `AFTERBURNER_KEY`
   hold — it does not touch pitch or roll at all.** It is a speed boost,
   not a dodge, so "missile evade" surviving is never guaranteed; the game
   is free to land the hit regardless. That makes a second explanation at
   least as likely as the perception-gap framing above: the missile simply
   connects, and what reads as an "OCR blackout" is the ordinary
   explosion/death sequence — which has no ammo/fuel/health digits to find
   by design, the same way a normal respawn screen doesn't. Log evidence
   alone cannot tell these apart — a blind HUD during marginal-but-alive
   flight and a blind HUD during a death animation look identical to every
   OCR channel. This is exactly the "add instrumentation instead of
   tuning" case: a frame saved at the evade-release instant would settle
   it directly (a cockpit view vs. an explosion effect is visually
   obvious), and no fix should be attempted before that distinction is
   made — a perception-layer bug and "the missile occasionally wins" call
   for entirely different responses, and guessing which one this is would
   be applying force to an unmeasured lever.

   **A seventh instance, live, 2026-09-10 10:11:00-02**, only 5.5 minutes
   after the sixth — `MissileEvade → BoundaryTurn` this time, same
   signature. Seven instances now (two archived, five live in this one
   session), spanning every tactic evade has been observed handing off to
   (`AttackSupport`, `Climb`, `BoundaryTurn`) — the pattern is tactic-
   agnostic, further evidence it belongs to the evade/perception boundary
   itself rather than to whatever runs after it. This is unambiguously the
   top priority for the next session: build the frame-capture instrument
   described above (mirroring `UnknownAnomalyRecorder`'s existing
   rate-limited capture-to-`test_screenshots/unknown_anomalies/` pattern),
   triggered off `_start_afterburner_evade`'s release, before attempting
   any behavioral fix.
5. What causes a death mid-BoundaryTurn with no urgent descent-rate reading
   (2 of session B's 7, one with a very high nearby-enemy ring count)? Not
   diagnosed. Possibly enemy fire during the turn rather than anything the
   turn itself does wrong — the ring-count field (rings=16) in one case is
   suggestive but not confirmed as causal.
6. Does `RespawnHealthStallRecorder` (D7) fire on a post-match results
   screen? Its one fire in the fourth live trial (2026-09-11 08:52:28,
   gap=31s) captured a `2ND`/`MVP`/`3RD` leaderboard, not an in-flight
   stall — `current_game_state == GAME_BATTLE` was apparently still true
   during a results screen for D7's gate (`GAME_BATTLE and not
   is_mission_running()`) to trigger at all. Not diagnosed further this
   pass: either the FSM's state read is genuinely stale through the
   results-screen transition (worth checking against the FSM's own
   transition log around that timestamp), or the results screen is shown
   while the FSM is still nominally in `GAME_BATTLE` by design and D7
   simply needs its own exclusion for it, mirroring how it already excludes
   `is_mission_running()`. Either way this is a false positive for the
   specific failure D7 was built to catch (a stuck-after-respawn mission
   that never restarts) — the 30s threshold and 12-per-session cap mean
   this is cheap to leave as-is for now, but it means D7's future fires
   need the same "is this really a stall, or a mundane state" visual check
   this one got, not an assumption that every fire is the target failure.

## D5 code review (2026-09-11)

A multi-angle code review of D4/D5 surfaced 14 findings, applied here
without redeploying the running wingman session (fixes land for the next
restart, not the current one):

- `cv2.imwrite`'s return value was never checked in `_capture_crash_frame`
  — a silent write failure would still increment the counter and log
  "saved". Now checked, matching the three sibling capture sites.
- The capture filename had only 1-second resolution, so two crashes in the
  same wall-clock second silently overwrote each other while the counter
  still counted both. Now includes the per-session sequence number.
- The `crash_with_missiles` diagnostic read `analyzer.get_telemetry()` and
  `snap.altitude.*` with no guard, unlike the very next line
  (`_capture_crash_frame`, deliberately wrapped so a capture failure can't
  break respawn handling) and unlike a sibling pattern elsewhere in the
  file. Now wrapped the same way, and reports `.value` (the raw last
  reading) instead of `.stable_value` (the smoothed one) — the smoothed
  value's lag is the exact mechanism D4 exists to work around, so the
  diagnostic line reporting it was undermining its own purpose.
- `TestDiveRecoveryTrigger`'s fixtures in `tests/test_behavior_tree.py`
  still hardcoded the pre-D4 `recover_below_time_s=20.0`/
  `confirm_bypass_time_s=10.0` — no test exercised the 30.0/15.0 actually
  shipped. Updated the fixture defaults and the three boundary-specific
  tests' altitude/rate values to probe the real 30s/15s edges instead of
  the retired ones.
- `max_climb_s` (the emergency actuator's unconditional duration cap)
  stayed at 15.0 while `recover_below_time_s` widened 20→30, so shallower
  dives newly admitted by the wider trigger got the same cap as before.
  Raised 15.0→22.5, the same 1.5x scaling D4 already applied to
  `confirm_bypass_time_s` — proportional, not measured; no live session has
  hit this cap before a dive resolved one way or the other, so it needs
  watching next time, not assuming fixed.
- `crash_capture.enabled` defaulted to `True` whenever the whole config
  section was absent, not just when `enabled` itself was unset — the
  opposite of the test helper's own explicit safety default. Flipped the
  in-code default to `False`; `config.yaml`'s explicit `enabled: true` is
  unaffected since it always supplies the key.
- `make tp`/`make tp-full` run `rr-path1-gate` and the OCR replay
  integration tests against the *real* `config.yaml` (crash_capture
  enabled), with no mock and no tmp-dir redirect, so the release gate
  itself could write real PNGs to `test_screenshots/crash_with_missiles/`
  as a side effect. Fixed at the source: `RespawnHandler` now takes a
  `replay_mode` flag (threaded from `main.py`'s existing `replay_mode`
  local, the same signal that already suppresses other live-only behavior)
  and forces capture off whenever a run is a replay, regardless of what
  the config says.
- The comment "matching UnknownAnomalyRecorder" was wrong —
  `UnknownAnomalyRecorder`/`HealthDropoutRecorder` import cv2 once in
  `__init__`; `_capture_crash_frame` imports it per-call, which actually
  matches `_capture_boundary_frame`. Corrected.
- Hitting the session cap returned silently with no log line, unlike
  `_capture_boundary_frame`'s equivalent path — reintroducing, for the
  capture instrument itself, the log-only ambiguity D5 exists to remove
  elsewhere. Added a debug-level line.
- `tests/test_replay_integration_make_y.py`'s `FakeAnalyzer` had no
  `get_telemetry()` — dormant today (that smoke path never sets
  `is_respawning=True`) but would raise `AttributeError` the moment it did.
  Added the method, returning `None`.

Deliberately **not** fixed this pass, with reasoning:
- **Four independent hand-copied capture implementations** in
  `tick_handlers.py` (`_capture_boundary_frame`, `UnknownAnomalyRecorder`,
  `HealthDropoutRecorder`, now `_capture_crash_frame`) share the same
  skeleton with no extracted helper. Consolidating them touches three
  existing, already-live, already-tested capture paths for a
  maintainability win, not a bug fix — a separate, deliberately-scoped
  pass, not something to fold into this one.
- **The captured frame may postdate the actual death moment** — the code's
  own adjacent comment says the tick's frame has already advanced past the
  respawn overlay by the time `is_respawning` surfaces from the OCR cache.
  Structurally real, but every one of the 7+ frames manually reviewed this
  session showed relevant content (an explosion, a kill-feed banner) —
  contradicted in practice so far. No safe fix identified without a deeper
  look at the capture pipeline's frame timing; left as a documented risk.
- **Synchronous disk I/O and a telemetry lock acquisition on the main tick
  thread** — bounded by the 20-per-session cap, and offloading the write to
  a background thread would have to increment the success counter before
  knowing whether the write actually succeeded, directly undoing the
  imwrite-check fix above. Correctness won over this bounded, low-severity
  performance concern.

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

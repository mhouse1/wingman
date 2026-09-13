# Anomaly 003 — Eject No-Telemetry False Death Leaves the Aircraft Unflown

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Summary

**Revision (2026-09-13, later same day):** the original theory below —
"the aircraft never died, so nothing was ever going to end the wait" — turned
out to be built on an incomplete check. Re-examined against the exact
telemetry timeline (prompted by a direct question about a better detector
signal), the better-supported explanation is the opposite: **a real
death-and-respawn most likely did happen**, during a roughly 12-second
screen transition in which *every* OCR channel — respawn overlay, health,
telemetry — went dark at once. `_eject_descent_control`'s own no-telemetry
timeout (`telemetry.stale_after_s`, 6.0s) is shorter than that blackout, so
it gave up and declared `no_telemetry` while a normal transition was likely
still resolving on its own. See "Was there a death?" below for the evidence
and why the original conclusion was wrong, kept here rather than silently
rewritten (this project's own convention: say what the measurement said,
even when it contradicts what was written first).

The operator-visible consequence is unchanged either way: a transient
telemetry read gap during an eject dive got classified as death, and the
behavior tree sat on `Idle` (zero actuation) for at least 64 measured
seconds with nothing correcting heading, until the operator intervened to
stop it flying toward the map boundary — before the existing 120s bound
(`eject_max_s`) would have resolved the FSM on its own (see "Why nothing
recovered"). **No existing mechanism detects this class of stall** — see
"Detection" below.

This is the same `no_telemetry` misclassification documented as the trigger
for the 2026-09-12 incident fixed by ADR 136 D5 — but D5 only stopped a
*different* downstream symptom (the heatdive roll/fire loop continuing to
twitch the aircraft). It did not address this one.

## The incident

Log: `logs/wingman_20260913_080229.log` (archived; live at the time as
`wingman.log`), session ending 08:02:29.

| time (07:5x/07:59) | event |
|---|---|
| 58:47.751 | Missiles confirmed empty. FSM `GAME_BATTLE → GAME_BATTLE_EJECT`. `eject_and_dive` engages descent control. |
| 58:47.802 | `Controller: eject_and_dive — descent control engaged (impulse rotation, target 100 m/s)`. Eject heatdive loop started. |
| 58:49.302 | `BT[active]: tactic Eject → Idle` — the Eject leaf's own `is_running_fn` window closed; `Idle` becomes and stays the selection. |
| 58:59.762 | Last genuinely fresh altitude read before the gap: `alt=5058.0` (the value visibly changes here, not a repeat of the previous tick). |
| 59:04 – 59:09 | Health OCR reading no digits (`"health OCR found no digits — raw: []"`, repeating), grace timer running. Altitude reads `None` in the BT snapshot from `59:04.271`. |
| **59:09.423** | **`Controller: eject_and_dive — descent control ended (no_telemetry) — holding until respawn`** — `_eject_descent_control`'s own `telemetry.stale_after_s` (6.0s, `config.yaml:637`) elapses with no fresh sample; it gives up. ADR 136 D5's fix correctly stops the heatdive thread here (no roll/twitch this time — see "Relationship to ADR 136 D5" below). |
| **59:11.752** | Altitude, fuel, and missile count **all reset in the same tick**: `alt=898.0 (rate +593m/s)`, `fuel=100%`, `missiles=4`. Three independent readings changing together is the signature of a fresh spawn — **~12 seconds** after the last fresh read at `58:59.762`. |
| 59:11 – 59:52 | Altitude climbs steadily to `1684.0` (**+20 m/s**), a normal controlled-flight profile. `Respawn OCR results: []` on every sampled tick throughout — the overlay itself was never read. `BT[active]: selected=Idle`, `mission=False`, on every tick — nothing below `Idle` in `_PRIORITY_ORDER` (including `BoundaryTurn`) ever gets evaluated. |
| **59:52.630** | Operator presses Backspace: `"Controller: Backspace — ending wingman; MetalStorm stays up for manual control."` — the interrupt this record exists to explain. |
| 59:53.736 | FSM finally re-enters `GAME_BATTLE` — **after** the operator's interrupt, as part of shutdown, not as a recovery the system produced on its own. |

### Was there a death?

The original version of this record said no, "proven" by a third signal —
`58:49.227`'s `"HEALTH ALIVE consumed — spurious eject-start transition (no
observed death)"`, from `_alive_transition_disposition` (ADR 061 / SAF-002,
`main.py:152-170`). **That check does not cover the moment that matters.**
It fired at `58:49`, right at eject start — two full seconds before the
`59:04-59:09` window where health OCR was itself reading no digits at all.
A signal that can't observe anything during the exact interval in question
proves nothing about that interval; the earlier write-up treated "not
observed" as "observed not to happen," which is a different claim.

Weighed against the corrected timeline, the better-supported read is: a real
death and respawn most likely occurred, inside a roughly 12-second screen
transition (a killcam, blackout, or similar) during which respawn-overlay
OCR, health OCR, and telemetry all went dark **simultaneously** — consistent
with all three sharing a capture/rendering source, not three independent
failures. `_eject_descent_control` gave up at its own 6-second mark, partway
through a transition that was, on this reading, already resolving normally.
This is not re-provable from the log alone — no frame capture exists for
this window (see "Detection" and Design 012 below) — but it is the
explanation that requires the fewest independent coincidences.

## Why nothing recovered

Three things were true at once, and each depended on the others not being
true:

| mechanism | why it didn't help (in time) |
|---|---|
| Respawn OCR | Never fired — `Respawn OCR results: []` on every sampled tick, including through and after the likely respawn moment. The overlay's on-screen window was, on the corrected theory, simply shorter than (or misaligned with) wingman's OCR sampling — a miss, not evidence nothing happened. |
| ADR 061 / SAF-002 health fast path (`terminate_eject`) | **Blind at the critical moment, not inapplicable.** This path exists precisely to catch "respawn happened but the overlay was missed" — exactly this incident's likely shape. But it needs health OCR to have *observed* a reading below 1, and health OCR was reading no digits at all from `59:04` through `59:09` — the same blackout that took out telemetry and the respawn overlay. The one rescue path built for this exact case couldn't see far enough into the blackout to use it. |
| `eject_and_dive`'s own hold + `eject_complete` (the 120s bound) | Bounded, not absent. `fire_eject` (`tick_handlers.py:916-930`) passes `on_complete=lambda: analyzer.trigger_event("eject_complete")` to `eject_and_dive`; `eject_complete` is a real FSM transition (`GAME_BATTLE_EJECT → GAME_BATTLE`, `analyzer.py:807`) fired once the thread's `_run()` returns — regardless of whether a respawn was ever OCR-confirmed. The hold is capped at `eject_max_s` (120.0, `config.yaml:646`). The operator interrupted at the 43s mark, **well before** that 120s bound would have resolved the FSM on its own — so this incident does not show the recovery failing, only that it hadn't fired yet. |
| Behavior tree `Idle` slot | Highest priority (`_PRIORITY_ORDER[0]`), condition is "not in `GAME_BATTLE`." `GAME_BATTLE_EJECT` satisfies that unconditionally, so nothing lower — including `BoundaryTurn`, the one tactic whose entire job is preventing exactly the failure the operator was worried about — ever got a chance to run, for as long as the state persists. |

Three independent recovery paths exist for "stuck in `GAME_BATTLE_EJECT`," and
this incident took out two of them **with the same blackout that caused the
problem** — the respawn-overlay OCR miss and the health-OCR blindness likely
share one root cause (a shared capture/rendering source going dark), not two
unrelated failures. Only the blunt 120s timeout was structurally guaranteed
to survive it.

So the real gap is narrower than "stuck forever": the FSM **would** recover
within 120s of the false-death declaration regardless of respawn OCR. The
actual problems are (1) up to 120s of uncorrected flight is still a real
window to fly out of bounds in, entirely silently, and (2) nothing surfaces
that this is happening while it's in progress — see "Detection" below. Once
`eject_complete` fires, whether the behavior tree actually resumes normal
actuation (in particular whether `mission_running` gets re-armed) is not
established by this log and is the next thing to check, not assumed.

## Relationship to ADR 136 D5

Same trigger (`no_telemetry` inside `_eject_descent_control`, `controller.py`
~line 2058), two different consequences:

- **2026-09-12** (documented in ADR 136 D5): the heatdive roll/fire loop kept
  running for 31s after the false death, visibly rolling and twitching the
  aircraft. Fixed: the loop is now stopped at the same point the "holding
  until respawn" log line fires.
- **2026-09-13** (this record): with the heatdive loop correctly stopped, the
  *next* layer out — FSM state and the behavior tree — is still left with no
  path back to normal control. The aircraft doesn't twitch anymore; it just
  flies straight, unflown, for as long as respawn goes unconfirmed.

D5 fixed the symptom one layer in. This anomaly is the layer D5's own code
comment didn't claim to address (it fixes the heatdive thread specifically,
not the FSM/BT consequence of the false-death verdict itself).

## Log signature

```
Controller: eject_and_dive — descent control ended (no_telemetry) — holding until respawn
```
followed by, for longer than a few ticks:
```
Respawn OCR results: []
BT[active]: selected=Idle ... mission=False
```
with altitude readings that are present, non-`None`, and changing smoothly
(not the erratic/frozen pattern a real post-respawn or crashed state would
show). The FSM state at the time (visible via `Game state: X → GAME_BATTLE_EJECT`
with no further transition) confirms it.

## Detection

**No existing mechanism would have caught this while it was happening.** Checked
directly against the three watchdogs this codebase already has for adjacent
"stuck" shapes, and none cover it:

| mechanism | why it doesn't apply |
|---|---|
| `LivenessGuard` (ADR 093, `wingman/liveness_guard.py`) | Defines progress as an FSM state change **or any OCR activity at all** (`main.py:1207-1214`: "OCR happening at all is progress"). Fuel/altitude/telemetry OCR kept succeeding throughout the stall — only the respawn crop came back empty — so the guard saw continuous progress and its 300s soft / 900s hard limits never got close to firing. It cannot distinguish "busy and flying correctly" from "busy and flying uncorrected." |
| Stall-recovery action gate (ADR 084/087, `analyzer.py:_stall_recovery_targets`) | Gated on `STALL_ACTION_STATES = (GAME_UNKNOWN, GAME_STARTING_STALLED)` plus a lobby-blackout special case. `GAME_BATTLE_EJECT` is a validly-classified state, not an unclassifiable one, so this mechanism never engages regardless of how long the state persists. |
| `unknown_anomaly` capture | Fires on unclassified/black-screen frames. `GAME_BATTLE_EJECT` is a recognized, expected state during a normal eject — there is nothing "unknown" for this mechanism to notice. |

The gap these three share: each watches for the game becoming *unresponsive
or unrecognizable*. This incident is the opposite shape — everything stayed
responsive and recognized, and the aircraft was genuinely, healthily flying.
What went missing was silent: the behavior tree simply stopped being allowed
to act. Nothing in the codebase currently asks "is the tree actuating
anything, given that a mission is nominally in progress and the aircraft is
airborne" — that would be the generic version of a detector for this, the way
the liveness guard is the generic version of a detector for total stalls.

## Recommended design — auto-detect and terminate for review

**Goal.** Not a general safety feature (that is a separate, larger decision
requiring this house's own shadow-first/live-trial discipline before it could
gate a default unattended run). This is a **diagnostic workflow**: run
`make rd v` (Design 012 recording on), and have wingman recognize this
specific anomaly and end the session promptly and gracefully, so the operator
gets a short, reviewable recording targeted at the failure instead of having
to watch a long session live or guess at the right timestamp afterward. Scope
accordingly — **gated on `--record-session` being active**, a no-op otherwise.

### Signal — revised twice

**First revision** (documented above under "Was there a death?"): moved from
`GAME_BATTLE_EJECT` dwell + `alive_after_observed_death` + sustained climb to
watching the telemetry-freshness gap directly, on the theory that the gap
causing the false-death declaration was the more direct signal.

**Second revision, after a self-review of implementation-readiness, found
the first revision doesn't work:**

- **It would not have fired on the incident it was built from.** The actual
  telemetry gap, measured precisely, was **11.99s** — under the proposed
  13.0s threshold. A detector built from one incident that fails to detect
  that incident is disqualified on its own terms.
- **It watches the wrong window.** Telemetry recovered at `59:11.752` and
  stayed fresh (climbing steadily) for the rest of the session. A gap-based
  check resets and goes quiet the moment freshness returns. But the actual
  harm — `BT[active]: selected=Idle`, zero actuation — ran continuously from
  `58:49.302` to the operator's interrupt at `59:52.630`: **63.3 seconds**,
  more than five times the length of the gap, and almost entirely *after*
  the gap had already closed. A telemetry-freshness signal structurally
  cannot see a window where telemetry is, by definition, fresh.
- **The wiring assumed plumbing that doesn't exist.** `main.py`'s main loop
  has zero references to `altitude` or `analyzer.get_telemetry()` today —
  confirmed by checking, not assumed. The `altitude_fresh` local the
  original wiring snippet used was never established anywhere in that
  scope.

**Corrected signal: dwell time on `GAME_BATTLE_EJECT` itself.** It is the one
thing that was true for the *entire* 63.3-second harmful window, not just
the first 12 seconds of it — and it needs no new telemetry plumbing at all,
only `analyzer.game_state`, already read throughout `main.py`.

```
analyzer.game_state == GameState.GAME_BATTLE_EJECT, continuously, for at
least eject_stuck_after_s (proposed default 40.0)
```

Why 40s: the real incident's harmful window ran 63.3s: a 40s threshold fires
with the anomaly still live and ~23s of margin before the operator's actual
interrupt, and comfortably below the 120s `eject_max_s` hard bound so a
`make rd v` session still ends itself well before that bound would anyway.
**This number is not well-calibrated** — it comes from one incident, the
same mistake the first two revisions made in different ways. Before relying
on it, check dwell times across a corpus of *healthy* ejects (a real death,
quickly respawn-OCR-confirmed) to confirm 40s has real margin above normal
resolution time, not just above this one anomalous case. The telemetry-gap
measurement from the first revision retains real value here, downgraded from
primary signal to **supporting log context**: when the dwell-based detector
fires, log the telemetry-gap length too (still worth computing from
`analyzer.get_telemetry()`), which is useful evidence either way and is what
directly supports Disposition (1)'s `stale_after_s` recommendation.

### Sketch (same shape as `LivenessGuard`)

```python
class EjectStuckDetector:
    """Anomaly 003. Diagnostic-only: caller must gate this on
    --record-session — see docs/anomaly/003-....md. Not a general
    safety mechanism; no live-trial has validated it as one, and its
    40s default threshold is calibrated from a single incident — see
    "Signal — revised twice" before trusting it uncalibrated.
    """

    def __init__(self, cfg, clock=time.time):
        cfg = cfg or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._stuck_after_s = float(cfg.get("eject_stuck_after_s", 40.0))
        self._clock = clock
        self._eject_since = None      # set on entering GAME_BATTLE_EJECT
        self._fired = False

    def check(self, game_state) -> bool:
        if not self._enabled or self._fired:
            return False
        if game_state != GameState.GAME_BATTLE_EJECT:
            self._eject_since = None
            return False
        if self._eject_since is None:
            self._eject_since = self._clock()
            return False
        dwell = self._clock() - self._eject_since
        if dwell >= self._stuck_after_s:
            self._fired = True
            logger.error(
                "ANOMALY 003 DETECTED: GAME_BATTLE_EJECT for %.0fs with no "
                "resolution (threshold %.0fs) — ending session for review "
                "(docs/anomaly/003-...)", dwell, self._stuck_after_s)
            return True
        return False
```

### Termination mechanism — corrected

**Not** `exit_requested.set()` (that pattern is for paths with no other way
into the loop — external signal handlers). Checked directly against
`main.py`'s existing in-loop guard cluster (`liveness.should_stop()`,
`resource_sampler.should_stop()`, `game_watch.game_has_gone()`,
`main.py:1268-1277`): every one of them uses a plain `break` from inside
`while True:`, since the check already runs in-loop and `break` is both
simpler and immediate (no need to wait for the top of the next iteration to
notice a flag). This detector should match that exact convention, not
introduce a second style.

Also **not** the `z` (`FINISH_ROUND_THEN_EXIT`) path, and — unlike
`liveness`/`resource_sampler` — **not gated on `_safe`**
(`GAME_LOBBY` and no mission running). `_safe` exists so those guards wait
for a clean restart point rather than abandoning an aircraft in flight; here
the aircraft is already unflown (that is the anomaly), so waiting for
`GAME_LOBBY` would mean waiting for the same respawn confirmation that is
never coming. `break` still runs the full `finally:` cleanup every other
loop-exit path shares — `video_recorder.stop()`/`bt_trace_writer.close()`
flush properly, keys release, session summary/stats still get written.

### Wiring — corrected

Lives in a new small module `wingman/eject_stuck_detector.py`, same shape as
`wingman/liveness_guard.py` (constructor takes `cfg`/`clock`, one `check()`
method, no other dependencies). Constructed in `main.py` alongside
`liveness`, and checked once per tick **inside the existing guard cluster**
(`main.py:1268-1277`, immediately after the `resource_sampler.should_stop`
check), gated on recording being active:

```python
# near: liveness = LivenessGuard(cfg.get("liveness_guard", {}))
eject_stuck = EjectStuckDetector(cfg.get("eject_stuck_detector", {}))

# in the main loop, alongside the liveness/resource_sampler guard checks:
if args.record_session and eject_stuck.check(analyzer.game_state):
    snap = analyzer.get_telemetry()   # supporting context only, not the trigger
    logger.warning(
        "\033[93m🛑 ANOMALY 003: ending session — GAME_BATTLE_EJECT stuck "
        "with no resolution (telemetry fresh=%s at termination); recording "
        "in progress for review\033[0m",
        snap.altitude_fresh() if snap else "unavailable")
    break
```

New config block, same shape as `liveness_guard`:

```yaml
eject_stuck_detector: {enabled: true, eject_stuck_after_s: 40.0}
```

### Testing plan

- A unit test replaying this incident's actual `game_state` sequence (via a
  fake clock: `GAME_BATTLE_EJECT` entered at `58:49.302`, held continuously)
  and asserting the detector fires once `eject_stuck_after_s` (40.0) is
  crossed, well before the operator's real `59:52.630` interrupt — built
  from the real `EjectStuckDetector`/`GameState` objects, not a hand-rolled
  re-implementation (this project's own stated failure mode: "a hand-rolled
  copy... said a frame passed every gate; the real one returned None").
- A negative test: `GAME_BATTLE_EJECT` entered and exited (back to
  `GAME_BATTLE`) well under 40s — matching a healthy, quickly-confirmed
  respawn — must never fire, and must reset cleanly on re-entering
  `GAME_BATTLE_EJECT` a second time later in the same session (a second
  eject must not inherit the first eject's clock).
- A test confirming `enabled: false` (or `--record-session` absent, at the
  `main.py` call-site level) is a hard no-op.
- Before trusting the 40s default outside this one incident: gather
  `GAME_BATTLE_EJECT` dwell times from a corpus of healthy ejects (a real
  death, promptly respawn-OCR-confirmed) and confirm 40s has genuine margin
  above normal resolution time — the default here is a first guess from one
  data point, the same mistake the first two design revisions made in
  different ways.

**Implemented 2026-09-13.** `wingman/eject_stuck_detector.py`
(`EjectStuckDetector`), wired into `main.py`'s existing in-loop guard
cluster (alongside `liveness`/`resource_sampler`, `main.py:~1268`), config
in `eject_stuck_detector:` (`config.yaml`, schema in `config_schema.py`).

**Live-tested the same day — and it false-positived on the first run.**
`make r1 v` was launched to validate it. It fired within 4 minutes
(`ANOMALY 003 DETECTED: GAME_BATTLE_EJECT for 41s with no resolution`), but
the recorded video (Design 012) and log both show this was **not** a
recurrence: the aircraft was critically damaged and actively diving —
visibly banking hard with a exhaust/flare trail in the extracted frames,
altitude falling smoothly and continuously the whole time (9052m → 4808m,
`ttg=10s` right before termination), no telemetry gap anywhere. A
same-session comparison made the gap in the design obvious: an *earlier*
eject in the same session resolved in 7.6s via
`"cancelled during descent (reason=respawn_detected)"` — the game's own
respawn signal arrived mid-dive and cancelled it early. The second eject
never got that signal and was still legitimately, actively diving toward
impact when the 40s raw-dwell clock cut it off.

**Corrected**: the detector was gating on entry into `GAME_BATTLE_EJECT`,
which is also true for every ordinary, healthy, still-in-progress dive —
indistinguishable from the true anomaly by that signal alone. Added
`Controller.eject_descent_active()` (`controller.py`, new), True only while
`_eject_descent_control` has not yet exited for any reason — the actual
moment nothing is flying the aircraft. `EjectStuckDetector.check()` now
takes this as a required second argument and only starts its clock once it
is False; a still-active dive of any length is silent. 3 new tests pin the
false positive directly (`test_quiet_while_descent_control_is_still_active`,
a 200-tick active dive that must never fire) alongside the corrected true-
incident case. `make lint && make test` green (14 detector-related tests
total). Re-running `make r1 v` to validate the correction live.

This is exactly what `/iterate`'s Run/Watch steps are for — stopping at the
gate, as the first implementation pass did, would have shipped a broken
detector behind a fully green test suite; only actually running it live
surfaced the false positive. See `.claude/skills/iterate/SKILL.md` step 5
for the standing rule this incident reinforces (a green gate is not the end
of the cycle).

## Impact

- At least 64 seconds of measured uncorrected flight; bounded at no more than
  120s by the existing `eject_complete` timeout (see "Why nothing recovered,"
  corrected above) — not indefinite, but long enough to reach a boundary.
- Operator had to manually intervene rather than trust the 120s bound —
  reasonably so, since nothing in the running session said "this will resolve
  itself in N more seconds" versus "this is genuinely stuck."
- Occurred twice in two days (2026-09-12 heatdive symptom, 2026-09-13 this
  symptom) from the same root trigger — `no_telemetry` during eject descent
  control is not a rare event.

## Disposition

**Partially addressed.** Diagnosed via `/check` against
`logs/wingman_20260913_080229.log`. Detection (item 3 below) is
implemented; the two fix candidates (1, 2) remain open. Three candidate
directions were identified, not mutually exclusive:

1. **Now the leading candidate**, given the corrected timeline: raise
   `telemetry.stale_after_s` (currently 6.0s) — or give
   `_eject_descent_control` its own, longer, eject-specific allowance —
   so it stops declaring `no_telemetry` partway through an ordinary
   respawn transition. This incident's own numbers make the case directly:
   a ~7s normal transition against a 6s timeout leaves essentially no
   margin, and this incident's actual gap (~12s) suggests transitions can
   legitimately run longer than 7s. Needs more occurrences (or a corpus of
   healthy respawn-during-eject transitions) to calibrate precisely rather
   than picking a number from one data point.
2. Independent of (1): `eject_max_s` (120s) already guarantees FSM recovery
   via `eject_complete` regardless of respawn OCR (see "Why nothing
   recovered") — whether 120s of uncorrected flight is an acceptable bound
   on its own, and whether `mission_running`/BT actuation actually resume
   correctly once `eject_complete` fires, are not established by this log.
3. **Detection — implemented.** `EjectStuckDetector`
   (`wingman/eject_stuck_detector.py`), gated on `--record-session`,
   watching how long `GAME_BATTLE_EJECT` itself persists — the one
   condition that held for the entire 63.3s harmful window, not just the
   initial ~12s telemetry gap (an earlier revision of this design keyed on
   the gap directly and would not have fired on this incident; see "Signal
   — revised twice"). Ends the session via the same `break` the
   `liveness`/`resource_sampler` guards already use, deliberately not
   gated on their `_safe` (GAME_LOBBY) requirement. Useful on its own even
   once (1) is fixed — a detector that only fires on genuinely abnormal
   dwell has ongoing diagnostic value, not just one-time investigation
   value.

Design 012 (session video + BT trace recording, `make rd v`) is available,
verified working, and now paired with the detector above: a future
recurrence terminates itself with a recording already captured, settling
whether this really is a missed respawn overlay during a real transition
(supporting (1)) — without needing an operator to notice and interrupt
manually. **Still needs a live `make rd v` session to actually exercise this
against a real recurrence** — nothing here has been validated live yet.

## What to watch

- Any recurrence of the log signature above.
- Whether a future occurrence's recorded video (Design 012) actually shows
  a respawn transition (killcam, blackout, spawn animation) during the gap —
  the single fact that would confirm the corrected theory over the original
  one.
- `GAME_BATTLE_EJECT` dwell times and telemetry-gap durations across future
  eject-during-respawn cases in general, healthy or not — to calibrate
  `stale_after_s` (currently 6.0s) and `eject_stuck_after_s` (proposed
  40.0s) from data rather than one incident and one estimate each.
- Whether `eject_max_s` (120s) is ever actually reached in a future
  occurrence — this record only observed the first 43s of that window.

## References

- ADR 136 — heatseeker-dive invokable mode; D5 is the related, insufficient
  fix for the same trigger.
- ADR 061 (SAF-002) — the observed-death health signal and `terminate_eject`
  fast path; checked and found blind during this incident's critical window,
  not structurally inapplicable to its likely cause.
- ADR 056 — the `GAME_BATTLE_EJECT` FSM state and its `eject_complete`
  transition (the 120s-bounded recovery this incident actually hit).
- ADR 093 — the liveness guard; checked and found not to cover this shape.
- `wingman/controller.py` — `_eject_descent_control` (`no_telemetry` exit
  reason, `telemetry.stale_after_s` threshold), `eject_and_dive`'s "holding
  until respawn" wait (~line 2058).
- `wingman/liveness_guard.py` — the structural template the recommended
  `EjectStuckDetector` design follows.
- `.claude/skills/check` — the read-only diagnostic that produced this
  record.
- Design 012 (`docs/hldd/012-session-recording-and-bt-trace-hldd.md`) — the
  recording tool the recommended design terminates a session to preserve.

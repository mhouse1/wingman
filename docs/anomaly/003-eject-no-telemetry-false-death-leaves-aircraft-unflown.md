# Anomaly 003 — Eject No-Telemetry False Death Leaves the Aircraft Unflown

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-14 | 1.8.9           |

## Summary

**Status as of 2026-09-14: the harmful consequence is fixed, heavily
validated, and operator-confirmed resolved — reviewed via `/check` and
agreed 2026-09-14.** Kept as `Draft` per this project's own convention for
this doc series (every anomaly doc here stays `Draft` in the table
regardless of resolution; see Anomaly 001/005 for precedent) — resolution
is recorded here in prose, not in the status table. Two smaller items
remain genuinely open (see below), tracked but not blocking.
`EjectStuckDetector` catches the stuck condition and ends the session
(with a recording) if nothing else resolves it — never had to fire across
any session on 2026-09-13 or 2026-09-14. Separately, `eject_and_dive`'s
post-descent hold now also resumes control on telemetry confirmation alone
(Disposition item 2) — not gated behind `--record-session`; it changes
default eject behavior for everyone.

**As of 2026-09-14 06:38 (session still in progress — this count will
still grow): n=62 real occurrences across six sessions (2026-09-13 x4,
2026-09-14 x2), recovery time (declaration to resolution) min 1.29s /
median ~7-8s / max 18.77s — zero exceeded 30s, zero reached the
120s backstop, zero triggered the detector, across every session
measured.** Compare to 63.3s/41.8s uncorrected before the fix existed. This
is no longer "one clean trial" — it's a large, repeated, measured sample
with a consistent ceiling well under the original harmful window, now
spanning two calendar days and confirmed independently via `/check`
against the newest session rather than just the original implementation
run. See Disposition item 2 for the full occurrence log and the "Full-day
measurement" write-up.

**What's still genuinely open, not just unclosed paperwork:** (a) the
residual gap where mission/combat resumption depends on health being known
at the moment of recovery — flight safety (BoundaryTurn/Climb) is confirmed
to resume regardless, but full combat resumption in the health-unknown case
has not been observed in any trial yet; (b) the root cause of the
underlying telemetry blackout itself is still not understood — this fix is
a robust mitigation, not a diagnosis of why the blackout happens. Both are
tracked in Disposition and "What to watch," not silently dropped.

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
recovered"). At the time this was originally written, no existing mechanism
detected this class of stall at all — see "Detection" below for that gap
analysis, and "Implemented 2026-09-13" for the detector since built and
live-validated to close it (detection only, not a fix).

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
total).

**Live-validated on the very next run — a real recurrence, caught
correctly.** `make r1 v` was relaunched (session `20260913_113129_acct1`).
Two ejects resolved normally (`respawn_detected`, ~18s and ~21s) — the
corrected detector stayed silent through both, confirming the false-positive
fix held. The third hit the exact original signature:
`"Controller: eject_and_dive — descent control ended (no_telemetry) —
holding until respawn"` at `11:36:06.517`, then no resolution until the
detector fired at `11:36:48.294` — **41.8s later**, just past the 40s
threshold, exactly as designed. `telemetry fresh=True at termination`
(logged automatically) — telemetry had recovered by then, same shape as the
original incident (the gap that causes the false-death call closes on its
own; the harm is what happens after).

The recorded video adds the piece the original incident never had:
**`RETURN TO BATTLE: 8`** — the game's own out-of-bounds warning, visible on
screen at t=300s into the recording, countdown running. Health reads intact
in the same frame — not a combat death. This is the mechanism the very
first `/check` in this investigation predicted from the operator's report
("why did it fly straight continuously... had to interrupt it to prevent it
flying out of bounds") now confirmed on video as a direct, actual
consequence rather than an inference: the aircraft was never killed, it
drifted out of the arena with nothing steering it back, exactly as
"Why nothing recovered" describes.

This closes the loop `/iterate`'s Run/Watch steps exist for. The first live
run caught a real defect in the fix itself (the false positive above) that
`make lint && make test` could not — a green gate is not the same as a
validated fix, and only actually running it, twice, surfaced first the bug
in the detector and then confirmed the corrected detector against a genuine
recurrence. See `.claude/skills/iterate/SKILL.md` step 5 for the standing
rule this incident reinforces.

### Detection timing, measured precisely (`20260913_113129_acct1`)

Two distinct moments, 41.8s apart — worth separating explicitly, since
"wingman detected it" is easy to misread as "at the moment it happened":

| video elapsed | wall clock | event |
|---|---|---|
| ~4:07 (247.1s) | 11:35:36.318 | Eject #3 begins (missiles empty) |
| ~4:37 (277.3s) | 11:36:06.517 | `_eject_descent_control` gives up: `"descent control ended (no_telemetry) — holding until respawn"` — **routine INFO logging, not flagged as an anomaly.** This existing log line predates Design 012/Anomaly 003 entirely; on its own it looks like ordinary eject bookkeeping. |
| **~5:19 (319.1s)** | **11:36:48.294** | `ANOMALY 003 DETECTED`, session ends |

The 41.8s gap between them is **by design**, not latency or a miss:
`eject_stuck_after_s: 40.0` requires the stuck state to persist that long,
specifically so the detector never fires on a brief, self-correcting
hiccup. Reviewing this video around 4:35 shows ordinary-looking flight two
seconds before the no-telemetry moment — there is no visible sign at 4:35
itself that anything is wrong; the aircraft only visibly drifts out of the
arena and triggers `RETURN TO BATTLE` well after, around t=300s (~4:40
after the no-telemetry moment).

### This is detection only — the underlying bug is NOT resolved

Stated plainly because "live-validated" above describes the *detector*
working correctly, not the *anomaly* being fixed: **nothing about the
false `no_telemetry` declaration, the aircraft being left unflown, or it
drifting out of the arena has changed.** Every occurrence still plays out
exactly as before — descent control still gives up wrongly, the aircraft is
still unflown, it still drifts and still triggers `RETURN TO BATTLE`. The
only difference `EjectStuckDetector` makes is that the **session now ends
itself, with a recording already captured**, ~40 seconds after the harmful
state begins, instead of continuing indefinitely until an operator notices
and interrupts (or the 120s `eject_max_s` bound resolves the FSM on its
own, with the aircraft's actual fate — recovered, still out of bounds,
penalized — unobserved either way). Disposition items (1) and (2) below —
the actual fix — remain fully open.

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

**Resolved (operator-confirmed 2026-09-14), with two smaller items left
open by design.** Diagnosed via `/check` against
`logs/wingman_20260913_080229.log`; the fix's resolution was itself
re-confirmed via a second `/check` on 2026-09-14 against n=49 real
occurrences across five sessions (see item 2's "`/check` verification"
entry below) — the operator reviewed that result and agreed. A sixth
session, still in progress as of 06:38, has added 13 more (n=62 running
total) with no exceptions — see the Summary above for the live count.
Detection
(item 4 below) is implemented and confirmed working against a genuine
recurrence (session `20260913_113129_acct1`, see "Implemented 2026-09-13"
above). Item 2 (the actual fix) is implemented and heavily validated. Item
1 is deprioritized on strong grounds (see its entry). Item 3 is a
pre-existing backstop, not new work. Four candidate directions were
identified, not mutually exclusive:

1. Raise `telemetry.stale_after_s` (currently 6.0s) — or give
   `_eject_descent_control` its own, longer, eject-specific allowance — so
   it stops declaring `no_telemetry` partway through an ordinary respawn
   transition. Originally deprioritized on n=2 as "weaker than it first
   looked." **Re-measured 2026-09-14 on n=43** (see "Full-day measurement"
   under item 2 below) — still deprioritized, but now for a stronger
   reason: item (2)'s fix bounds the actual harm to under 19s regardless
   of when (1) fires, so raising the threshold would reduce declaration
   *frequency*, not *harm*. Open, low-priority, revisit only if recovery
   times start climbing.
2. **Implemented 2026-09-13 (later same day).** Rather than tuning the
   timeout, gave the aircraft a way back to normal flight that doesn't
   depend on the unreliable signal at all. `Controller.eject_and_dive`'s
   post-descent "holding until respawn" wait (`controller.py`, the natural-
   completion `else:` branch) now also watches telemetry directly: N
   consecutive fresh reads (default 4, `eject_closed_loop.
   telemetry_confirm_polls`, ≈6.0s at the 1.5s poll interval) ends the hold
   early via the exact same `_eject_stop`/`on_complete`/`eject_complete`
   path the healthy `respawn_detected` cases already use — reusing proven
   machinery rather than adding a new FSM transition. A single fresh blip
   does not count; the streak resets on any stale read. Config: `0`
   disables it (falls back to the pre-existing wait-for-OCR-or-120s
   behaviour). 3 new tests in `tests/test_eject_closed_loop.py`
   (`test_hold_phase_resumes_when_telemetry_confirms_alive`,
   `..._is_not_cut_short_by_a_single_fresh_read`,
   `test_telemetry_confirm_polls_zero_disables_the_check`); full
   `eject_closed_loop` suite (31 tests) and `make lint && make test` green.

   **Live-validated 2026-09-13, same day, third `make r1 v` run
   (`session_20260913_121658_acct1`).** A real `no_telemetry` false-death
   fired at `13:25:42.026`; the telemetry-confirm check ended the hold at
   `13:25:48.027` — **6.28s later**, essentially exactly the designed 4 x
   1.5s = 6.0s window. FSM reached `GAME_BATTLE` by `13:25:48.305`.
   Compare: the two prior real occurrences ran uncorrected for 63.3s and
   41.8s (the latter only because the detector ended the session — it
   never actually recovered on its own). This is the first observed
   natural recovery, and it answers the mission-resumption question left
   open above: `mission_j20` restarted, `search_and_destroy` loops started,
   and the tree resumed real tactical selection the same tick
   (`Idle → Engage`), correctly reacting to a fast altitude drop six
   seconds later (`Engage → Climb`, `alt_rate=-570m/s`). Health was
   evidently known at the moment of recovery in this occurrence, so the
   `alive_event`-gated restart chain fired cleanly — the residual gap noted
   above (health unknown → mission stays paused) remains a real but
   unconfirmed risk, not observed in this trial.

   **Second live occurrence, same session, ~12 minutes later
   (`13:37:16.372` → `13:37:23.885`, 7.51s).** Different exit reason this
   time (`established`, not `no_telemetry`) — confirms the fix isn't
   narrowly tied to one exit path; it helps whenever the hold outlasts a
   respawn-OCR confirmation, regardless of why descent control gave up.
   Most direct evidence yet: the tree's first action on resuming was
   `Idle → BoundaryTurn`, not `Engage` — the aircraft was apparently near
   the arena edge at the exact moment control came back, and the tactic
   this whole investigation exists to protect engaged immediately, followed
   by `BoundaryTurn → Climb` 4s later. Mission restarted cleanly again.
   Two for two, both fast, both full recoveries.

   **Occurrence log** (further occurrences append here, not as new prose —
   same session unless noted; all `session_20260913_121658_acct1`):

   | # | exit reason | gave up → recovered | recovery time | first tactic on resume | notes |
   |---|---|---|---|---|---|
   | 1 | `no_telemetry` | 13:25:42.026 → 13:25:48.027 | 6.28s | Engage | mission restarted; detail above |
   | 2 | `established` | 13:37:16.372 → 13:37:23.885 | 7.51s | **BoundaryTurn** | the actual boundary-save, on camera |
   | 3 | `no_telemetry` | 13:43:00.671 → 13:43:06.693 | 6.02s | — | 3rd in 26 min of one session — frequency itself worth noting, see "What to watch" |
   | 4 | `no_telemetry` | 14:00:45.938 → 14:00:51.939 | 6.00s | — | clean, no errors |

   **New session, `session_20260913_164406_acct1` (started 16:44:06,
   ongoing).** Much higher sample: 11 `no_telemetry` occurrences in the
   first ~2h20m alone (roughly one per 12-15 min of active combat — this
   session's rate, not yet known to be typical). Every one resolved at or
   under **12.3s**, none via the 120s `eject_max_s` backstop, and
   `EjectStuckDetector` fired **zero** times (see main.py `ANOMALY 003
   DETECTED` — absent from this session's log entirely). Not every
   occurrence resolved via the telemetry-confirm path specifically — three
   independent recovery routes are visibly sharing the load, none of them
   slow:

   | # | gave up (no_telemetry) | resolved via | recovery time |
   |---|---|---|---|
   | 5 | 16:54:03.084 | respawn OCR confirmed | 3.8s |
   | 6 | 17:10:36.014 | round ended (GAME_END_B) | 6.3s |
   | 7 | 17:47:29.530 | **telemetry-confirm fix** | 6.0s |
   | 8 | 17:49:22.490 | round ended (GAME_END_B) | 12.3s |
   | 9 | 17:56:18.233 | respawn OCR confirmed | 0.3s |
   | 10 | 18:02:22.112 | **telemetry-confirm fix** | 7.6s |
   | 11 | 18:03:16.291 | round ended (GAME_END_B) | 12.3s |
   | 12 | 18:08:27.499 | round ended (GAME_END_B) | 10.8s |
   | 13 | 18:12:19.682 | respawn OCR confirmed | 1.8s |
   | 14 | 18:29:08.107 | round ended (GAME_END_B) | 7.8s |
   | 15 | 19:03:39.628 | **telemetry-confirm fix** | 6.1s |

   This is the first data showing the trigger recurs frequently under
   normal play (not a rare edge case) while the harmful consequence — long
   uncorrected drift — has not recurred even once since the fix shipped.
   "Round ended naturally" resolving the hold is a pre-existing, unrelated
   path (the match simply concluded) — counted here only to show the
   ceiling on unflown time stayed low regardless of which path closed it.

   Traced the downstream question this raises before implementing: does
   exiting `GAME_BATTLE_EJECT` alone (independent of whether the mission
   also restarts) restore the safety-critical behavior? Yes for the part
   that matters most: `BoundaryTurn`'s condition has no `mission_running`
   dependency, so once the FSM leaves `GAME_BATTLE_EJECT`, `Idle` stops
   winning and `BoundaryTurn`/`Climb`/`Evade` can actuate again regardless
   of whether full mission/combat also resumes. Mission/combat resumption
   specifically depends on `analyzer.alive_event`, which `on_enter_
   GAME_BATTLE` only sets if health is already known (`self._health is not
   None`) — health OCR has been unreliable in both real occurrences, so
   combat may stay paused for a life even after this fix correctly restores
   flight safety. Flagged as a smaller, separate residual gap, not blocking.

   **Full-day measurement (2026-09-14, `/iterate` review of the day's
   archived logs — no new live run, all data already existed from today's
   sessions).** Every `no_telemetry` occurrence across all of 2026-09-13's
   sessions with real combat (`wingman_20260913_154430.log` —
   `session_20260913_121658_acct1`, the log behind rows 1-4 above;
   `wingman_20260913_203647.log`, behind rows 5-15 above;
   `wingman_20260913_212404.log`; and the still-unarchived `wingman.log`
   from the last session of the day), measured with the same real-event
   pairing used for rows 1-15 (`descent control ended (no_telemetry)` →
   the next `GAME_BATTLE_EJECT → GAME_BATTLE` or `→ GAME_END_B`, scoped to
   the same episode — not a proxy signal; a first attempt using
   `alt=None`/`alt=<value>` in the BT snapshot line was tried and
   discarded because it kept measuring across state boundaries into
   unrelated lobby/matchmaking gaps, the exact "instrumentation can lie"
   trap this skill warns about):

   **n=43. Recovery time (declaration to resolution): min 1.29s, median
   7.37s, mean 8.22s, max 18.77s. Zero exceeded 30s. Zero exceeded 60s.
   Zero reached the 120s `eject_max_s` backstop. `EjectStuckDetector`
   fired zero times across all four sessions.** 12 of the 43 resolved via
   the telemetry-confirm fix specifically (items 2 above); the remainder
   via respawn OCR or the round simply ending — the fix is one of three
   routes sharing the load, exactly as rows 5-15 already showed, now
   confirmed at more than 4x the sample size.

   **This changes the calculus on Disposition item (1).** The original
   deprioritization reasoned from n=2 that "no single raised
   `stale_after_s` value reliably covers both" measured gaps (~6s, ~12s).
   That's still numerically true, but the premise it was protecting
   against — that a too-early declaration causes real harm — is now much
   weaker than when it was written: item (2)'s fix bounds the *consequence*
   of every declaration to under 19s regardless of whether the 6.0s
   threshold fires "early" or "on time." Raising `stale_after_s` would
   mean fewer declarations (less FSM churn, fewer eject-hold cycles,
   marginally less log noise) but would not measurably reduce harm, since
   harm is already tightly bounded by (2) independent of when (1) fires.
   **Recommendation: keep item (1) deprioritized, now on stronger grounds
   than "weak evidence" — not because the evidence is weak, but because
   n=43 shows the thing it would fix no longer matters much.** Revisit
   only if future data shows recovery times climbing (e.g., if the
   telemetry-confirm fix's assumptions stop holding under some
   not-yet-seen game-state combination).

   **`/check` verification, 2026-09-14, session started 02:30:** a fifth
   session (first on 2026-09-14) added 6 more real occurrences, checked
   independently via the `/check` skill against this exact fix rather than
   assumed from the earlier measurement. All 6 resolved 1.82s-11.81s, none
   via the explicit telemetry-confirm code path this time (all via
   respawn OCR or round-end — the other two of the three established
   routes, confirming the fix isn't the only thing keeping recovery fast).
   Zero detector firings. Running total at that point: **n=49 across five
   sessions, zero exceptions to the sub-19s ceiling.** Operator reviewed
   this result and agreed the harmful consequence is resolved (2026-09-14)
   — the basis for this doc's Summary update above.

   **Sixth session, still running as of 06:38.** 13 more occurrences,
   3.30s-16.31s, zero exceptions, zero detector firings. Running total:
   **n=62 across six sessions.** This session was not itself independently
   `/check`-verified line-by-line (the operator-agreed resolution above
   already stands on n=49) — logged here as a live count update, not a
   new verification pass.
3. `eject_max_s` (120s) already guarantees FSM recovery via `eject_complete`
   regardless of respawn OCR (see "Why nothing recovered") — this is now
   the backstop behind (2) rather than the primary recovery path.
4. **Detection — implemented.** `EjectStuckDetector`
   (`wingman/eject_stuck_detector.py`), gated on `--record-session`,
   watching how long `GAME_BATTLE_EJECT` itself persists — the one
   condition that held for the entire 63.3s harmful window, not just the
   initial ~12s telemetry gap (an earlier revision of this design keyed on
   the gap directly and would not have fired on this incident; see "Signal
   — revised twice"). Ends the session via the same `break` the
   `liveness`/`resource_sampler` guards already use, deliberately not
   gated on their `_safe` (GAME_LOBBY) requirement. Now a secondary
   backstop behind (2) as well — if telemetry-confirmed resumption works,
   this should fire far less often, and only ever on genuinely unrecovered
   cases.

Design 012 (session video + BT trace recording, `make rd v`) is available,
verified working, and paired with the detector: the `20260913_113129_acct1`
recurrence terminated itself with a recording already captured, and that
recording is what showed `RETURN TO BATTLE: 8` — the out-of-bounds warning
— rather than a missed respawn overlay. That's evidence against a missed-
overlay explanation for item (1) specifically: health read intact in the
frame, suggesting no death occurred at all in this occurrence either, closer
to the original incident's theory than the "likely missed overlay" revision.
Not conclusive on its own (one occurrence), but it argues for weighing (1)'s
`stale_after_s` fix over a respawn-overlay-detection fix specifically.

## What to watch

- Any recurrence of the log signature above.
- **Frequency, newly observed**: 3 occurrences in 26 minutes of one session
  (`20260913_121658_acct1`) — notably more often than the ~1-per-session
  rate seen across the two earlier live-test sessions. Not yet clear
  whether this session hit a patch of degraded OCR/capture specifically, or
  whether the true base rate was simply undersampled before. Track dwell/
  gap frequency across future sessions rather than assuming either.
- **Resolved by the `20260913_113129_acct1` recording**: whether a future
  occurrence's video shows a respawn transition — it showed
  `RETURN TO BATTLE: 8` instead, with health intact, arguing against a
  missed-overlay explanation. Still worth checking on further occurrences
  whether this is the typical shape or this one instance's particular case.
- **New, from the same recording**: what happens if `RETURN TO BATTLE`'s
  countdown expires with nothing correcting course — the session ended
  (by the detector) before this was observed. Does the game kill the
  aircraft, teleport it back, or something else? Relevant to how urgent
  fix candidate (1)/(2) actually are.
- Whether leaving the arena itself (not just the eject) disrupts HUD
  rendering or OCR — a speculative link between `RETURN TO BATTLE` and the
  `59:04`-window OCR trouble in the original incident, not yet checked.
- `GAME_BATTLE_EJECT` dwell times (from `eject_descent_active` going False,
  not raw entry) and telemetry-gap durations across future eject-during-
  respawn cases in general, healthy or not — to calibrate `stale_after_s`
  (currently 6.0s) and `eject_stuck_after_s` (40.0s, now confirmed to fire
  with real margin on one live recurrence) from more data.
- Whether `eject_max_s` (120s) is ever actually reached in a future
  occurrence — every observed occurrence so far has been caught by the
  40s detector well before that bound.

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

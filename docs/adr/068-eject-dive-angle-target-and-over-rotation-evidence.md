# ADR 068 — Eject Dive: Angle Target and Evidence-Gated Over-Rotation

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-08-09 | 1.7.1           |

*Accepted 2026-08-09 — all seven decisions implemented and flight-validated
across four live sessions (04:42 three-eject decision-5 validation, 13:12
24-eject combat trial of the hold rework, 19:02 with the eject driven through
the ADR 024 behavior tree), plus Code Review 014's thirteen findings resolved
and `make tp` fully green including both runtime gates.*

Extends [ADR 038](038-game-battle-altitude-speed-signals-for-phase3-and-eject-dive.md)
(Draft), [ADR 058](058-eject-dive-confirmation-via-raw-descent-rate.md)
(Accepted), and [ADR 067](067-metric-hud-units-pitch-normalization-recalibration.md)
(Draft). Neither 038 nor 058 is modified. This ADR supersedes ADR 058 on
decision 12 (the over-rotation guard's trigger condition) and on the
confirmed-only restriction for dive re-entry.

## Context

The intended behaviour on entering `GAME_BATTLE_EJECT` is a steep, roughly
vertical dive so the aircraft crashes promptly and respawns with fresh
missiles. The 2026-08-09 03:31 session did not produce one — the operator had
to take over manually. The full sequence, with corrected nose angles from
ADR 067:

```
03:31:48,725  eject_and_dive — NOSE_DOWN + AFTERBURNER engaged
03:31:50,162  Altitude: 8871 | Speed: 1301 | Nose: +49° (climb)
03:31:53,165  Altitude: 9578 | Speed:  961 | Nose: +43° (climb)
03:31:54,726  eject_and_dive — climbing (alt rate 236 ft/s) after 6.1s of
              nose-down — over-rotated, releasing instead of re-issuing
03:31:54,747  eject_and_dive — nose-down released, holding afterburner
03:31:59,164  Altitude: 9566 | Speed:  513 | Nose: -16° (dive)
03:32:05,164  Altitude: 9063 | Speed:  554 | Nose: -38° (dive)
03:32:08,164  Altitude: 8782 | Speed:  557 | Nose: -38° (dive)
   ... 6 consecutive samples at -37° to -38°, no further input ...
03:32:24,885  maneuver key 'k' pressed — entering GAME_BATTLE_MANUAL
03:32:32,168  Altitude: 6377 | Speed:  581 | Nose: -54° (steep_dive)
03:32:35,167  Altitude: 5840 | Speed:  879 | Nose: -73° (steep_dive)
03:32:38,170  Altitude: 4923 | Speed: 1530 | Nose: -90° (steep_dive)
```

Three defects compound into the observed failure.

### 1. The over-rotation guard misdiagnosed a zoom climb

ADR 058 decision 12 releases nose-down when the aircraft is climbing after a
long continuous hold, reading the climb as rotation past vertical. Its
implementation tests only `rate > 0` and elapsed hold time.

The eject fired from a 1301 KPH zoom climb. Every sample from the press to the
guard firing was a climb (+49°, +43°); the flight path never went down. The
aircraft had **under**-rotated — 6.1 s of nose-down had not yet overcome the
upward momentum — and the guard removed the one input that was working.

ADR 058's own evidence describes the real signature: *"8 holds that **dove
then climbed** while still held"*. Getting past vertical requires passing
through a dive. The implementation never checked the "dove then" half.

The operator's manual nose-down press is the controlled experiment: the same
input the guard refused produced -54°, -73°, -90° within 13 seconds.

### 2. A dive short of the target could never be corrected

`dive_reentries_left` was granted only when the nose phase exited
`confirmed` (ADR 058, to stop re-entry from re-running a loop that was failing
for its own reasons). Because this eject never confirmed, the count stayed at
zero, so the post-release watcher observed six consecutive samples at -38° and
had no mechanism to act. The aircraft glided for 30 s until manual takeover.

### 3. Confirmation at 53 degrees is not the stated goal

Confirmation released nose-down at `BAND_STEEP_DIVE` (sin 0.8, approx 53
degrees). The requirement is a near-vertical dive; releasing at 53 degrees
leaves the aircraft to settle shallower, which is what the post-release
samples show.

## Decision

**1. Over-rotation requires observed prior descent.**

`_eject_descended_since_press` is set by any descending sample during the
eject and reset when the sequence starts. The ADR 058 d12 release fires only
when it is set. A flight path that has only climbed is under-rotation, and
nose-down continues to be commanded.

This narrows d12 to the case its own measurements described; it does not
disable it.

**2. The release criterion is a measured angle, not the steep band.**

`target_dive_angle_deg: 75.0` — nose-down is held until the corrected
flight-path angle reaches -75 degrees (or the raw descent-rate path confirms).
75 rather than 90 because readings saturate at 90 whenever descent rate
exceeds displayed forward speed (ADR 067), so 90 is not a reliably observable
target. The ADR 058 raw descent-rate path is unchanged and still confirms
dives the angle cannot measure.

This is only possible because ADR 067 made the angle trustworthy. The band
machinery in ADR 038 and 058 existed largely to work around a normalization
that was compressed 5.28x; with a correct angle the criterion can state the
goal directly.

**3. Dive re-entry is granted whenever the closed loop is enabled.**

Both re-entry paths now require fresh telemetry showing an attitude short of
the target *and* nose-budget headroom, so the ADR 058 objection is answered at
the point of use rather than by withholding the budget: a give-up on missing
data still cannot spend a re-entry, while genuine under-rotation can.

**4. `total_nose_budget_s` raised from 10.0 to 40.0** (initially 20.0; raised
again after the 2026-08-09 10:04 flight; decision 6 later raises it to 90.0
when held time began to include the full descent).

Reaching 75 degrees from a fast climb takes about 19 s of held nose-down on
its own, and the budget must also absorb the failures that cost held time
without producing rotation: the 10:04 eject lost ~6 s to an OCR outage that
forced a timer release mid-rotation at +17 degrees, then ~6 s to a missed
nose-down key injection that the correction re-press was 1.8 s too late to
recover within a 20 s budget. All five ejects observed on the 20 s setting
exhausted it; the 10:04 eject then glided level for 25 s with zero remaining
key authority until manual takeover.

The budget remains the backstop against flying a full loop, but it is no
longer the primary protection: the over-rotation guard (decisions 1 and 5) now
detects the actual past-vertical signature — a climb after an observed
descent — so generous time no longer converts into loops. 40 s covers roughly
two full rotation attempts with correction overhead.

**5. The nose-up reversal requires the dive target to have been reached.**

The measure-correct-measure reversal (ADR 038, retained by ADR 058) fires when
the aircraft is descending and the last nose-down made the descent *shallower*
— the past-vertical signature. That proxy is weak well short of vertical,
where speed decay produces the same reading. Validation flight 2026-08-09
03:52:34 caught it: at **-37 degrees**, two samples 10 m/s apart triggered a
nose-up tap that took the eject to -11 degrees, away from the dive.

Being past vertical requires having *got* near vertical, and the flight-path
angle cannot separate "rotated to 37 degrees" from "rotated past vertical and
back to 37" — but history can. `_eject_reached_target_dive`, set when the
target angle is observed, now gates the reversal. The climb-based guard
(decision 1) remains the primary protection against a genuine over-rotation,
since rotating past vertical eventually produces a climb.

**6. Hold nose-down through the descent; scope rotation evidence per attempt.**
*(Added after the 2026-08-09 10:16 session.)*

That session (13 ejects, 13 respawns, zero manual takeovers) proved the
mechanics but exposed a structural cost in release-on-confirm: the game
auto-levels the moment pitch input stops, so every confirmation decayed within
3-6 s (17 decay/re-entry cycles against 15 confirmations) and 4 of 13 ejects
still exhausted the 40 s budget re-winning ground already won.

Two changes:

- **Confirmation no longer releases the key.** The nose phase keeps holding
  through the descent; the hold ends on respawn (stop event), the
  over-rotation guard, the nose budget, or a sensor frozen past 4x the check
  interval (a key is never left down against missing data). A dive that
  decays *while held* falls through to the correction machinery and is
  re-established in place. Release-on-confirm existed to avoid blindly
  holding past vertical when the ratio was untrustworthy; with ADR 067's
  corrected angle and a guard that detects the actual past-vertical signature
  (validated: 2 correct firings, 0 misfires), the fear it encoded is
  obsolete.
- **Evidence flags reset at each phase entry.** The session's four nose-up
  reversal misfires all followed one pattern: a -90 confirmation early in the
  eject left `reached_target_dive` set, so during a later re-entry the
  auto-level's pitch-up read as "descent got shallower after nose-down" and
  fired the reversal — helping the auto-level. Each phase call is a fresh
  rotation attempt from an unknown attitude; the over-rotation and reversal
  gates now trust only observations from the current attempt.

`total_nose_budget_s` rises to 90 as the loop backstop, since held time now
includes the productive descent itself (40-80 s from typical eject altitudes).

**7. The hold is its own loop; evidence, corrections, and budgets are scoped
per rotation attempt** *(added after Code Review 014).*

The deep review of decision 6's implementation (docs/code-review/014-2026-08.md)
confirmed six defects sharing one root cause: the confirmed hold was threaded
through the establishment loop, so machinery calibrated for a ~5 s rotation
attempt (legacy deadline, correction budget, reversal baseline, over-rotation
guard) ran throughout 40-80 s holds. The rework:

- `_eject_establish_dive_once` keeps the unmodified ADR 038/058 rotation
  semantics and returns on confirmation; `_eject_hold_established_dive` is a
  dedicated hold loop with exactly the checks a held descent needs — stop
  event, hold safety timeout (`hold_max_s`, default 120 s), telemetry-loss
  release after `stale_after_s` without a fresh sample (grace a stale poll
  previously did not get, CR-014-1/-6, with exit_reason kept `confirmed` so
  the watcher arms decay re-entry), afterburner engagement verification
  (previously unreachable on the respawn-ended path, CR-014-4), over-rotation
  release requiring the same distinct-sample streak as confirmation
  (CR-014-5), and decay hand-back.
- A decay while held starts a fresh establishment cycle: fresh correction
  budget (CR-014-3), fresh reversal baseline and evidence flags (CR-014-2) —
  each cycle is a rotation attempt in its own right.
- Held time at target is credited back to the rotation budget
  (`_eject_nose_hold_credit_s`), so `total_nose_budget_s` returns to 40 s and
  again means what it originally meant: a bound on unproductive rotation
  time, not on descent duration (CR-014-9). Runaway holds are bounded by the
  hold safety timeout and respawn instead.
- The post-release watcher reads each re-entry's outcome back and suppresses
  further re-entries after an over-rotation refusal (CR-014-10); the ADR 044/
  045 validators additionally exempt `match_ended` and `shutdown` in-phase
  cancellations, which the long hold now makes routine (CR-014-11).

**8. `confirm_descent_fps` stays 250.**

The value is compared against raw display units, i.e. m/s, so the effective
threshold is 250 m/s rather than the 250 ft/s its name implies. That is
*correct for the current purpose*: 250 m/s selects genuinely steep, fast
descents, whereas the nominal 76 m/s would have confirmed the 38-degree glide
this ADR exists to prevent. Renaming is deferred with the other unit misnomers
(ADR 067 decision 4).

## Consequences

- Ejects from climbing flight keep commanding nose-down instead of releasing
  at the first long-hold climb sample.
- A dive that stalls short of the target gets up to `dive_reentries` further
  nose-down holds instead of gliding until respawn or manual takeover.
- Nose-down can now be held up to 20 s cumulatively per eject. If a genuine
  over-rotation is ever mis-detected as under-rotation (both signals stale
  through the whole rotation), the aircraft could hold nose-down through a
  loop; the budget bounds that at 20 s, and decision 1 only *narrows* when
  release happens, so the guard still fires for the dive-then-climb case.
- Log lines now carry the measured angle (`nose -78deg`) and correct m/s units
  for rates. The ADR 044/045 validators key on other substrings and are
  unaffected.

## Validation

- `make test` — 456 passed.
- Unit coverage added: zoom climb without prior descent does not release and
  keeps commanding nose-down; dive-then-climb still releases as over-rotation;
  the prior-descent flag is set by a descending sample; a shallow post-release
  dive triggers re-entry; missing telemetry never spends a re-entry; no nose-up
  reversal before the dive target has been reached.

### Live validation flight 2026-08-09 03:52

The eject completed **without manual intervention** — the defect this ADR
addresses did not recur — and exposed decision 5 in the process:

```
03:52:16,358  eject_and_dive — NOSE_DOWN + AFTERBURNER engaged
03:52:19,296  Altitude: 10772 | Speed: 670 | Nose: -90° (steep_dive)
03:52:19,359  dive confirmed via target angle (nose -90deg, alt rate -161 m/s
              after 3.0s, 0 correction(s), 2 consecutive)
03:52:22,293  Altitude: 10372 | Speed: 693 | Nose: -54° (steep_dive)
03:52:22,367  dive decayed after confirmation (nose -54deg) — re-entering
              nose-down verification (2 re-entry left)
03:52:28,373  dive not established (band=dive, alt rate -88 m/s) —
              corrective nose-down re-issue (1/3)
03:52:31,297  Altitude: 9511 | Speed: 525 | Nose: -70° (steep_dive)
03:52:34,582  dive not established (band=dive, alt rate -78 m/s) —
              corrective nose-up re-issue (2/3)          <-- decision 5 defect
03:52:37,296  Altitude: 9215 | Speed: 225 | Nose: -11° (dive)
03:52:41,193  total nose-down budget (20s) exhausted — releasing
03:52:46,300  Altitude:  8748 | Speed: 326 | Nose: -90° (steep_dive)
03:52:49,299  Altitude:  8364 | Speed: 559 | Nose: -90° (steep_dive)
```

What the flight confirms:

- **Angle-target confirmation works and is fast** — confirmed at -90 degrees in
  3.0 s with zero corrections, against the old band criterion that would have
  released at 53.
- **Decay re-entry works** — the dive flattening to -54 degrees was detected and
  another nose-down hold commanded, the mechanism that was unreachable before
  decision 3.
- **The nose-up reversal at -37 degrees was wrong** and cost roughly 7 s and 26
  degrees of rotation (fixed by decision 5, added after this flight).
- The 20 s budget was reached; the aircraft then dove to -90 degrees and stayed
  there. With decision 5 the wasted reversal is gone, so the budget should not
  bind in the same way.

### Live validation flight 2026-08-09 04:42 (decision 5)

Three ejects on the decision 5 fix. The reversal defect did not recur and the
stated behaviour was met on every one:

| Measure | Result |
|---------|--------|
| Nose-up reversals short of target | 0 (was the 03:52 defect) |
| Over-rotation releases | 0 (was the original defect) |
| Corrections issued | 8, all nose-down — correct direction throughout |
| Ejects reaching -90 degrees | 3 of 3 |
| Confirmed via target angle | 2 of 3, both at -90 degrees |

The third eject is the clearest trace of decision 1 working: engaged at +41
degrees and 989 KPH, the loop kept commanding nose-down against a +160 m/s
climb — the exact condition the old guard released on — then rotated -16, -36,
-66, -90 over nine seconds.

**The 20 s budget bound on every eject in this session.** All three hit it.
Eject 3 reached -90 degrees 2.6 s before the budget expired, one telemetry
sample short of the two needed to confirm; ejects 1 and 2 confirmed, decayed to
-37 and -50 degrees, correctly took a re-entry, and then exhausted the budget
2-5 s later. Rotation from a fast climb to vertical measures about 19 s, so 20 s
left no headroom for the 6 s two-sample confirmation or any re-entry. The
2026-08-09 10:04 flight then showed the failure outcome directly — budget
exhausted mid-recovery, followed by a 25 s level glide with no remaining key
authority until manual takeover — and settled the question: decision 4 now
sets 40 s.

### Implementation defect found in production 2026-08-09 08:09

Decision 1's prior-descent check compared `altitude.rate` without a None guard.
`altitude_fresh()` does not imply a numeric rate — rate is None until two
accepted readings exist, which recurs after every telemetry gap. The result was
a `TypeError` that killed the eject thread mid-sequence:

```
File "wingman/controller.py", line 1744, in _run
    if rate < 0:
TypeError: '<' not supported between instances of 'NoneType' and 'int'
```

The `finally` block still released AFTERBURNER, NOSE_DOWN and NOSE_UP and
logged `eject_and_dive complete`, so no key was left held — but post-release
monitoring, re-entry and the afterburner hold were all lost for that eject.
Fixed at both sites; two regression tests cover the nose phase and the
post-release watcher, and both were verified to fail without the guard.

### Live validation session 2026-08-09 10:16 (decisions 1-5, 40 s budget)

13 ejects, 13 respawns, **zero manual takeovers**, zero crashes. 15
target-angle confirmations at -90 degrees, many within 3-4.5 s of engagement.
The over-rotation guard fired correctly twice and misfired zero times. Two
residual patterns from this session — the auto-level decay sawtooth and four
post-decay nose-up reversal misfires — are addressed by decision 6.

### Live validation session 2026-08-09 13:12 (decisions 6-7, post-CR-014)

3h17m, 11 missions all click-to-finish, **24 ejects, 24 completed
autonomously**, 26 respawns, zero crashes, zero errors, one unrelated manual
takeover (13:20, before the first eject). Every CR-014 mechanism was
exercised in flight:

| Mechanism | Count | Notes |
|-----------|-------|-------|
| In-phase confirmations (held) | 30 | plus 17 post-release confirmations |
| Decay while held, re-established in phase | 26 | fresh cycle each time |
| Telemetry lost during hold, released as confirmed | 2 | previously-dead path now delivering |
| Over-rotation releases (streak-deduped) | 7 | zero single-sample aborts |
| Afterburner re-presses during hold | 16 | see caveat below |
| Nose-up reversals | 1 | zero misfires against auto-level decays |

Eject durations: mean 63 s, p50 69 s, p90 83 s, max 117 s.

The anticipated "single confirmation held to respawn" signature did **not**
dominate — zero in-phase respawn cancellations. The traces show why:
contested airspace. Incoming missiles strike the diving aircraft (flare
bursts run concurrently with the eject), knocking a confirmed -90 dive to -9
degrees with speed collapsing 583 to 74 KPH — battle damage, not auto-level,
drives most decays, and 15 of 24 ejects exhausted the 40 s rotation budget
fighting through it before gliding under afterburner to a shot-down respawn.
The outcome is unaffected (24/24 fresh-missile respawns), and the rework's
actual claims — no misdiagnosed releases, graceful telemetry loss, bounded
recovery, no permanent glides — are all demonstrated.

Caveat worth a future look: 16 afterburner re-presses across 24 ejects
suggests the speed-trend check reads terminal-dive drag plateaus
("trend flat/falling") as a missed press. Bounded at 2 per eject and
harmless, but the heuristic is weak evidence during a dive.

- `make test` green (461 passed) and `make rr-path1-gate` PASS on the
  reviewed implementation.
- ~~Remaining for Accepted: one green `make tp` including the ADR 045 live
  lane.~~ **Done 2026-08-09** — green after the lane's capture was pinned to
  the config region, removing its hidden dependency on the game window's
  position (the true cause of the lane's historical flakiness).

# Wingman Safety Requirements

**UID**: DOC-SAF \
**Prefix**: SAF-

Safety properties of MetalStorm Wingman. The recurring hazard across ADRs 058,
059, 061, 063 and 064 is uncommanded flight: wingman issuing or continuing
flight-control input when the operator, or the game state, says it must not.
Each requirement states the property only; the governing ADR is referenced for
rationale and evidence. Tuning values live in wingman/config.yaml, never here.

## Operator flight-control input preempts commanded flight

**UID**: SAF-001

**Statement**: When the operator physically presses a flight-control key (NOSE_UP i,
NOSE_DOWN k, ROLL_LEFT j, ROLL_RIGHT l, or an arrow key) while wingman is
commanding flight — a mission thread holds the mission lock, an eject
sequence is active, or a tactic hold (climb, missile evade) is active —
wingman shall cease all commanded flight input, including the tactic hold
threads, within 2.0 s and transition to GAME_BATTLE_MANUAL, and shall not
re-command flight until health evidence indicates a death and respawn.

**Rationale**: The manual-takeover guarantee. The 2.0 s cessation bound is enforced by
tests/test_mission_cancel.py::test_cancel_releases_lock_within_two_seconds.
Requested 2026-08-07; behaviour shipped in wingman/controller.py
(_handle_maneuver_key_press). ADR 059 governs the post-death return to auto.
Tactic holds added 2026-08-17: a live session showed the FSM transitioning
to GAME_BATTLE_MANUAL while the climb hold kept pulsing nose-up and cycling
the afterburner for the rest of its 90 s cap — the transition alone does not
stop the hold threads, so takeover must stop them explicitly and the holds
must release when the FSM leaves their owning states.

## Injected key presses never trigger takeover

**UID**: SAF-001.1
**Relations**:
- **Type**: `Parent` \
  **ID**: `SAF-001`

**Statement**: Wingman's own injected key presses, including X-server auto-repeat echoes
delivered after release, shall never trigger the manual-takeover path.

**Rationale**: Injected keys echo back through the XRecord listener at a measured ~25 Hz
(ADR 053). Without echo discrimination, every eject correction would trigger
its own takeover.

## Entry grace window for stale keystrokes

**UID**: SAF-001.2
**Relations**:
- **Type**: `Parent` \
  **ID**: `SAF-001`

**Statement**: Physical flight-control presses within the 2.0 s grace window after
GAME_BATTLE or GAME_BATTLE_EJECT entry shall be ignored, and the ignored
press shall be logged when the exception is exercised.

**Rationale**: Stale-keystroke protection: keystrokes queued during a state transition must
not flip the aircraft into manual mode the instant a battle begins.

## No commanded flight while the aircraft is dead

**UID**: SAF-002

**Statement**: Wingman shall not issue flight-control input while health evidence indicates
the aircraft is not alive, and shall restart the mission only after health
evidence confirms the aircraft is alive again. This holds in every battle
state, including GAME_BATTLE_MANUAL and GAME_BATTLE_EJECT.

**Rationale**: The uncommanded-flight incident class: three separate production incidents
(missed-overlay respawn, manual-mode death, eject flying straight) reduced to
wingman commanding a dead or respawned aircraft. ADR 059 (single restart
path), ADR 061 (observed-death provenance).

## Every respawn is handled exactly once

**UID**: SAF-003

**Statement**: Every death-and-respawn episode shall trigger respawn handling exactly once:
by overlay OCR when it detects the episode, otherwise by the health-evidence
fallback. The fallback shall stand down when OCR owns the episode, so that no
episode is handled twice and no episode is dropped.

**Rationale**: ADR 064 dual-sensor design. Overlay OCR recall is ~92%; the health fallback
covers the misses (first live catch: 2026-08-07 08:12, tier=strong,
dead_for=4.6 s, OCR at 0% confidence). The recent-OCR-edge stand-down window
prevents double-firing. Scored per session in the respawn_shadow stats block.

## Unconfirmed health reads are never acted upon

**UID**: SAF-004

**Statement**: A raw health OCR read shall not change wingman's alive/dead state until it is
confirmed by recurrence: two of the last three reads agreeing within the
configured tolerance (health.value_confirm_window, value_confirm_tolerance).
Reads above health.max_plausible shall be discarded outright.

**Rationale**: ADR 063. A logged session measured roughly 50% garbage reads (fragments,
concatenations, a false 0) over the respawn overlay; acting on any single
read produced spurious death/respawn transitions.

## Bounded nose-down command during eject

**UID**: SAF-005

**Statement**: Cumulative commanded nose-down during a single eject sequence shall not
exceed telemetry.eject_closed_loop.total_nose_budget_s. A climb observed
after a continuous nose-down hold longer than over_rotation_after_s shall
release the hold rather than re-issue it.

**Rationale**: ADR 058 d12. Across 27 logged ejects the productive descent happened in the
first ~8-11 s of hold; holding longer over-rotated the aircraft into a climb
— commanded input producing the opposite of the commanded intent.

## Bounded missile-evade hold

**UID**: SAF-006

**Statement**: A missile-evade hold shall release all held flight keys unconditionally no
later than behavior_tree.missile_evade.max_hold_s after entry, and shall
release them on program exit and on controller cleanup. Absence of fresh
perception samples shall not end the hold early, and shall not extend it past
the cap.

**Rationale**: ADR 070 d6. A detection stuck true (a HUD element that keeps matching the
template, a frozen frame) would otherwise pin afterburner and a
full-deflection roll for the rest of the mission and fly the aircraft out of
the arena — the same failure class as the ADR 069 nose-hold budget. The cap
firing is a detector fault and is logged at WARNING.

## No injected key survives process termination

**UID**: SAF-007

**Statement**: On termination — normal exit, SIGTERM, or controller cleanup — wingman shall
release every key it is capable of injecting, such that no wingman-injected
key press remains asserted in the X server after the wingman process has
exited. This holds regardless of which thread held the key (mission, eject,
evade, disengage, recovery) and regardless of the termination point, including
termination inside a press-and-release call. Every key any code path injects
shall be present in the cleanup release list.

**Rationale**: XTest key state lives in the X SERVER and survives the injecting process; a
key left held is auto-repeated into whatever window has focus, indefinitely.
Incident class: NOSE_DOWN/AFTERBURNER left pressed for the whole X session
after a mid-eject exit (pre-ADR 069 cleanup hardening), and the 2026-08-14
stuck-'i' incident (a test-suite mission thread outliving its keyboard stub —
CR-016-01). Enforced by Controller.cleanup()'s unconditional release of the
full injectable-key list (audit 2026-08-14 added the previously missing
'escape' and MISSION_J20_KEY), SIGTERM routing through the graceful path, and
stoppable daemon threads. The test suite carries its own session-end
release-all net in tests/conftest.py as defense in depth.

## Bounded climb hold

**UID**: SAF-008

**Statement**: A climb hold shall release all held flight keys unconditionally no later
than its duration cap — behavior_tree.climb.max_climb_s for a band-recovery
climb, behavior_tree.climb.mission_start_max_s for a mission-start climb —
after entry, and shall release them on mission cancellation, on program
exit, and on controller cleanup. Absence of fresh telemetry shall not end
the hold early, and shall not extend it past the cap.

**Rationale**: ADR 073 (3.2b/3.2c), mirroring SAF-006: an unreadable altitude (OCR dropout,
frozen frame) would otherwise pin nose-up and afterburner indefinitely — the
ADR 069 nose-hold-budget failure class. The cap firing without altitude
confirmation is logged at WARNING as a fault indicator. Enforced by the
climb thread's finally-block release, the stop event set in cleanup(), and
the mission prologue's cancellation path.

## Bounded respawn spawn-attitude hold

**UID**: SAF-009

**Statement**: A spawn-attitude guard hold shall release its held flight key
unconditionally no later than behavior_tree.climb.spawn_guard.max_hold_s
after entry, and shall release it on manual takeover, on eject or
missile-evade start, when the FSM leaves GAME_BATTLE, on program exit, and
on controller cleanup. When a climb hold is active at release time, the
guard shall skip the OS-level key release — the climb hold owns the key
state and its own release path applies.

**Rationale**: ADR 076. The guard holds NOSE_UP from death detection through the respawn
screen so the new life's first frames are already pitching up; while the
respawn screen is up the hold is inert, so the entire risk window is the
first seconds after spawn. The cap mirrors SAF-006/SAF-008: absence of the
alive handoff signal is a perception fault and is logged at WARNING. The
ownership rule prevents the guard's release from yanking a climb pitch
pulse in progress (two threads must never independently release the same
key).

## Tactic handoff leaves a flyable attitude

**UID**: SAF-010

**Statement**: A tactic that commands pitch shall not release its pitch input to neutral
while the observed flight-path angle is outside the flyable band bounded by
behavior_tree.climb.exit_pitch_deg. Before releasing it shall command the
aircraft back inside that band, subject to a bounded pulse budget, or hand
explicitly to a recovery tactic. Exhausting the budget shall release and be
logged as a fault indicator, not treated as success.

**Rationale**: ADR 086 d1. SAF-005 already bounds commanded nose-DOWN during an eject with
both a budget and an attitude-referenced release rule; SAF-008 bounds the
climb by DURATION only, and that missing half is a live defect. On
2026-08-21 the climb released all three flight keys at +73 degrees, leaving
the aircraft ballistic; it coasted 1500 m further, stalled at 24 KPH, fell
into an unrecovered dive and hit the ground with two missiles still racked.
84 climb completions in that session produced 21 near-vertical stalls.
Critically, SAF-008 and FR-007 were BOTH satisfied throughout — every
existing requirement describes the climb while it runs or when it starts,
none describes what it hands back. Currently unsatisfied by design; the
@relation marker lands with the ADR 086 implementation.

## Ground-collision avoidance by predicted time to ground

**UID**: SAF-011

**Statement**: While wingman is commanding flight in GAME_BATTLE and the aircraft is
descending, wingman shall command a recovery when the predicted time to
ground — altitude divided by descent rate — falls below
behavior_tree.climb.recover_below_time_s. When that predicted time is below
behavior_tree.climb.confirm_bypass_time_s the recovery shall fire on a
single qualifying reading rather than waiting for
behavior_tree.climb.confirm_reads confirmations. A rejected or absent
telemetry reading during an established descent shall not clear the descent
state for behavior_tree.climb.descent_memory_s.

**Rationale**: ADR 086 d2-d4. The existing recovery band is expressed in ALTITUDE, but the
emergency is governed by descent RATE: at the 552 m/s descent measured on
2026-08-21 the 4000 m sustain band allowed 7.2 s and the 500 m emergency
band allowed 0.9 s, while telemetry ticks about every 3 s and confirm_reads
of 2 needs roughly 6 s — the confirmation was slower than the margin it
protected, so the band could not fire at any altitude tuning. The band was
also frozen: make_climb_condition holds its decision when altitude is None,
and rapid altitude change is exactly what trips the ADR 038/067
plausibility filter, so the net went deaf precisely when needed. Absence of
perception must not read as absence of danger. Currently unsatisfied by
design; the @relation marker lands with the ADR 086 implementation.

## No terrain impact while the airframe is serviceable

**UID**: SAF-012

**Statement**: Wingman shall not fly the aircraft into terrain while it is commanding
flight and the airframe remains serviceable. A ground impact that is not
the intended conclusion of an eject sequence is a violation. Verification
is by outcome over a session: near-vertical stalls, counted as telemetry
samples at or above +80 degrees flight-path angle with speed below 120 KPH,
and ground impacts occurring while missiles remain available.

**Rationale**: ADR 086. Modelled on FR-005, which is likewise verified by outcome rather
than by mechanism because no in-game sensor reports the property directly.
This requirement is the standing statement of the hazard that SAF-010 and
SAF-011 address by different means, and it is what stays true if either
mechanism is later replaced. Baseline for comparison, session
2026-08-21 04:16: 21 near-vertical stalls across 84 climb completions and
51 respawns over 16 missions (3.2 per mission), against 53 missiles-empty
ejects. The eject share is by design and is expected to be unchanged; the
stall share is the target. Currently unsatisfied by design.

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
commanding flight — a mission thread holds the mission lock, or an eject
sequence is active — wingman shall cease all commanded flight input within
2.0 s and transition to GAME_BATTLE_MANUAL, and shall not re-command flight
until health evidence indicates a death and respawn.

**Rationale**: The manual-takeover guarantee. The 2.0 s cessation bound is enforced by
tests/test_mission_cancel.py::test_cancel_releases_lock_within_two_seconds.
Requested 2026-08-07; behaviour shipped in wingman/controller.py
(_handle_maneuver_key_press). ADR 059 governs the post-death return to auto.

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

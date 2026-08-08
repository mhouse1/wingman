# Wingman Functional Requirements

**UID**: DOC-FR \
**Prefix**: FR-

Functional behaviour of MetalStorm Wingman: the FSM contract, mission
lifecycle, and timing bounds. The replay path YAML
(tests/replay_paths/adr037_paths.yaml) remains the source of truth for FSM
acceptance criteria; requirements here point at it and do not re-encode it.

## Game-state FSM contract

**UID**: FR-001

**Statement**: Wingman shall model the game session as a finite state machine with the
states GAME_UNKNOWN, GAME_LOBBY, GAME_WAITING, GAME_STARTING,
GAME_STARTING_STALLED, GAME_BATTLE, GAME_BATTLE_MANUAL, GAME_BATTLE_EJECT
and GAME_END_B, whose transition sequence for the standard mission loop is
validated per tick by the runtime replay gate.

**Rationale**: The executable criterion is tests/replay_paths/adr037_paths.yaml
(expected_state, expected_trigger, max_settle_time_s per step), enforced by
make rr-path1-gate (ADR 044) and the live-screen lane (ADR 045).

## Tick cadence and non-blocking OCR

**UID**: FR-002

**Statement**: The main loop shall process one captured frame per tick at the cadence
configured by loop_interval_sec, and OCR work shall run asynchronously such
that a tick never blocks on OCR completion. Per-crop OCR timings shall be
recorded per session and compared against the release baseline using the
thresholds in config.yaml.

**Rationale**: The performance regression workflow: PerformanceTracker histograms per crop,
docs/performance/current vs release, make tp preview gates.

## Mission launch after Good Luck

**UID**: FR-003

**Statement**: On detecting the Good Luck banner in GAME_STARTING, wingman shall launch the
mission after at most mission.good_luck_wait_s, and earlier when battle-alive
evidence is confirmed while mission.good_luck_bypass_on_alive is enabled.
Whether the wait ran its full length or was bypassed shall be logged.

**Rationale**: ADR 065: the wait is the no-evidence fallback; the bypass converts confirmed
health evidence into earlier launches (measured mean saving 3.9 s per launch
across 54 armed windows in three sessions).

## Battle entry on confirmed battle-alive evidence

**UID**: FR-004

**Statement**: Wingman shall enter the battle within 1.0 s of the first confirmed
battle-alive indication during GAME_STARTING.

**Rationale**: Bound settled 2026-08-07 from three live sessions (54 armed windows, 40
first-raw-read measurements): once battle-alive is confirmed, the Good-Luck
wait polls at 0.1 s, so 1.0 s is comfortably satisfiable. The ~1.5 s
confirmation latency measured in those sessions is upstream of "first
confirmed indication" and does not count against this bound. ADR 032 designed
the fallback; ADR 065 made it reachable.

## Battle-alive probe observability

**UID**: FR-004.1
**Relations**:
- **Type**: `Parent` \
  **ID**: `FR-004`

**Statement**: The GAME_STARTING battle-alive probe shall log every attempt, including
attempts that read no value, and shall report attempt count and
first-raw-read time on disarm.

**Rationale**: ADR 065 Decision 4. The probe was unreachable for three months because a
probe that never ran and a probe that ran and saw nothing produced identical
logs: silence. This requirement is what makes FR-004 falsifiable in
production.

## Aircraft remains inside the battle arena

**UID**: FR-005

**Statement**: During GAME_BATTLE, wingman shall continuously steer the aircraft toward
detected enemy positions, so that the aircraft does not fly outside the
battle arena.

**Rationale**: ADR 028 (minimap ring-engage navigation). The preprogrammed mission path
sometimes carried the aircraft outside the battle area. Enemies only render
inside the arena, so navigation that always steers toward detected enemies
bounds the excursion. No arena-boundary sensor exists; the requirement is
verified by outcome (excursion observations with ring-engage on vs off).
Implemented by EngageNavigator.update (wingman/engage_nav.py).

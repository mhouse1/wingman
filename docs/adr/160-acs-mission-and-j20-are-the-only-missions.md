# ADR 160 — ACS_MISSION and J20 Are the Only Missions

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-14 | 1.9.2           |

## Decision

Operator, 2026-10-14: "currently wingman is configured as mission su30, rename
that to ACS_MISSION, and remove the other missions except mission j20".

**D1. `mission_su30` is renamed ACS_MISSION, with its behavior unchanged.** The
script that ADR 144 defined and ADR 147 gave its own altitude floor is the
mission the operator flies, and it no longer flies only the Su-30: the
2026-10-14 sessions flew it on a faster jet. It becomes the single ACS mode that
[Design 011](../hldd/011-acs-mode-hldd.md) and the README expected the per-jet
scripts to merge into. Only the names change:

| Was | Now |
|-----|-----|
| `mission.default_mission: su30` | `mission.default_mission: acs` |
| `su30_mission:` block | `acs_mission:` block, same keys and values |
| `Controller.mission_su30`, `_run_su30_sequence`, `_su30_*` | `mission_acs`, `_run_acs_sequence`, `_acs_*` |
| `ControllerConfig.su30` | `ControllerConfig.acs` |
| `MISSION_SU30_KEY` (`o`), handler `start_su30_mission` | `MISSION_ACS_KEY` (still `o`), `start_acs_mission` |
| `_last_mission` value `"su30"` | `"acs"` |
| log lines `mission_su30 - step N/4` | `mission_acs - step N/4` |
| `docs/missions/su30.md` | `docs/missions/acs.md` |

ADR 147's per-mission floor is now keyed on `"acs"`. ADR 144's padlock block
reads the same `_last_mission` check under the new name.

**D2. Four missions are removed:** `mission_loiter` (ADR 109 to 123),
`mission_survey` (Design 017 phase 4b), `mission_f111` (ADR 149) and
`mission_jas39` (ADR 145 D1 and D2). Also removed:

- Each mission's method, config block, schema section, `ControllerConfig` field,
  `_start_default_mission` entry and `restart_last_mission` branch.
- The `y` hotkey (`MISSION_LOITER_KEY`).
- The loiter's survival-hold seam: `is_survival_hold`, the `survival_hold` field
  of the tree's snapshot, Eject yielding to it, and the disengage and navigator
  suppressions that read it.
- The survey's hooks in shared flight code: the roll gate (`_survey_owns_turns`),
  the boundary-turn and sustain-climb refusals, and `note_boundary`, which only
  fed the loiter's and the survey's own boundary state.
- The JAS39 cloak loop (`start_cloak_loop` / `stop_cloak_loop`); its interval
  lived in `jas39_mission`.
- `wingman/survey.py`, `scripts/toggle-survey.py`, `make survey`, and the
  mission specs `docs/missions/f111.md` and `docs/missions/jas39.md`.
- Five members of the Perception port that only these missions read:
  `nose_direction` (the loiter's entry pull-up), `read_compass_heading` and the
  three full-map members (the survey). The analyzer keeps the methods.
  `tests/test_perception_port.py` requires every port member to have a reader.
- "or loiter" from FR-009's statement: it now covers the configured-mission
  hotkey.
- The tests of each.

**D3. Kept**, because they are not one mission's:

- `mission_j20`.
- `u` launching whichever mission `mission.default_mission` names (ADR 145 D3).
- The `_scripted_*` helpers that ADR 149 extracted from `mission_su30`; ACS uses
  them.
- The `wingsweep` and `activate_special_weapon` key helpers and their keys.
  They are game bindings, and the keys stay in `INJECTABLE_KEYS` (SAF-007).
- The compass (`wingman/compass.py`, `hdg=` on the tick line) and the full-map
  reader (`wingman/full_map.py`). They are perception, not missions. Nothing
  opens the full map now, so the reader is dormant.

**D4. No alias for the old names.** A config, or a `config.local.yaml`, that
still says `default_mission: su30` (or `f111`, `jas39`, `survey`) fails schema
validation at start, naming the value. That is safer than silently launching a
different mission. `scripts/session-report.py` reads both `mission_su30` and
`mission_acs`, so logs from before the rename still parse.

## Consequences

- A respawn restart after this change logs `restarting last mission (ACS)`.
- `mission.default_mission` accepts `j20` and `acs` only.
- Superseded parts: ADR 145 D1 and D2 (the JAS39 mission and its cloak loop),
  ADR 149 (the F-111 mission), and the loiter decisions of ADR 109 to 123. ADR
  145 D3 (`u` launches the configured mission) and ADR 144 and 147 (now ACS)
  stand. Those ADRs are not edited.
- Design 017's survey work, paused since 2026-10-04, has no mission to resume
  from. `main` as of PR 82 still has `wingman/survey.py` and `mission_survey`.
- `docs/missions/README.md`, `README.md`, `docs/architecture.md` and the job aid
  now list two missions.

## Validation

- `make lint` and `make test` green with the removals.
- A live session on `default_mission: acs` shows `mission_acs - step 1/4` at
  battle entry and `restarting last mission (ACS)` after a death. It must show
  no `mission_su30` line.
- `o` during a battle logs `starting ACS mission` and preempts the running
  mission, as it did for su30.

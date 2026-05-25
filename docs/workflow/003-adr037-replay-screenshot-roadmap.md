# Workflow 003 - ADR037 Replay Screenshot Roadmap

| Status | Date | Wingman Version |
|--------|------|-----------------|
| Draft | 2026-05-25 | 1.6.10 |

## Purpose

Define the staged plan to move from the temporary `make y` replay smoke lane to the full ADR037 grounded paths (`PATH1`, `PATH2`) with real curated screenshots.

## Current State

- Replay assertion gating is implemented in runtime replay mode.
- Grounded path definitions live in `tests/replay_paths/adr037_paths.yaml`.
- `make y` runs `tests/test_replay_integration_make_y.py` using generated placeholder screenshots (`SMOKE_PATH`).
- Real screenshot sets for `PATH1` and `PATH2` are not yet captured.

## Validation Snapshot (2026-05-25)

- `make y` passed (`1 passed`) on branch `experimental_changes`.
- `make tp` passed (`32 passed`) and regenerated preview performance artifacts.
- Replay smoke gate is operational for CI/local command usage while screenshot capture work is pending.

Observed non-blocking warnings to track:

- `uv` environment mismatch warning (`VIRTUAL_ENV=.venv-1` vs project `.venv`).
- EasyOCR / Torch quantization deprecation warnings.
- Shutdown-time OCR executor warning (`cannot schedule new futures after interpreter shutdown`).

## Deliverables

1. Screenshot fixture inventory for `PATH1` and `PATH2`.
2. Curated screenshot library in `test_screenshots/integration_test`.
3. Replay validation report proving all required files exist.
4. Integration test upgrade from `SMOKE_PATH` to real path execution.
5. CI command migration from temporary `make y` smoke to full path gate.

## Phased Plan

### Phase 1 - Fixture Inventory Lock

- Freeze the filename contract from `tests/replay_paths/adr037_paths.yaml`.
- Generate a missing-file report via replay required-screenshot output.
- Track ownership and source run for each required screenshot.

Exit criteria:
- Required list is stable and reviewed.

### Phase 2 - Capture and Normalize Screenshots

- Capture screenshots from representative runtime sessions.
- Normalize dimensions/crop alignment to the replay region contract.
- Verify naming matches replay config exactly.

Exit criteria:
- All files for `PATH1` and `PATH2` exist in `test_screenshots/integration_test`.

### Phase 3 - Path Validation

- Run replay paths with real screenshots using the current assertion engine.
- Resolve assertion timing failures by adjusting path injection times or settle windows.
- Keep trigger/state ordering strict; avoid weakening assertions without evidence.

Exit criteria:
- `PATH1` and `PATH2` pass replay assertions locally on repeat runs.

### Phase 4 - Test and CI Promotion

- Update integration test to execute `PATH1` (and optionally `PATH2`) directly.
- Keep `make y` as fallback until full screenshot path is stable.
- After stability window, repoint `make y` to real path gate and remove placeholder smoke mode.

Exit criteria:
- `make y` validates real ADR037 path behavior with curated screenshots.

## Risks and Mitigations

- Risk: OCR variance causes flaky settle windows.
  - Mitigation: tune `max_settle_time_s` using observed replay traces, not guesses.
- Risk: Filename drift between config and fixtures.
  - Mitigation: enforce report-based missing-file checks in test setup.
- Risk: Temporary smoke lane lingers too long.
  - Mitigation: set promotion criteria and closeout checklist in PR template.

## Immediate Next Actions

1. Capture first batch for all `PATH1` filenames.
2. Run replay required-screenshot report and close missing gaps.
3. Add one PR that switches integration test from `SMOKE_PATH` to `PATH1` once complete.

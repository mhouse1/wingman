# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Git — Never Commit or Push Without Manual Review

**Do not run `git commit`, `git push`, `git tag`, `make p`, or `make wrelease` unless the user explicitly asks for that action in the current request.**

Finishing a task means leaving the work in the working tree and reporting what changed. It does not mean committing it. The user reviews every change before it enters history.

- Stage nothing and commit nothing on your own initiative, however small or "obviously correct" the change is.
- Completing a test run, a green gate, or a fix is **not** authorization to commit.
- Earlier permission does not carry forward. "Commit this" authorizes that one commit, not the next one.
- When work is ready, say so and list the changed files. Wait to be asked.
- If a commit seems warranted, propose it and stop — including the message you would use.

This rule outranks any workflow convenience below, including the release workflow in `## Commands`.

## Project Overview

MetalStorm Wingman — AI automation for MetalStorm (PC). Runs unattended mission loops via screen capture, OCR, and keyboard/mouse injection. The version is defined in `wingman/main.py` as `WINGMAN_VERSION`.

## Commands

Use `uv` for dependency management (`uv sync --all-groups`). Always prefer `make <target>` over ad-hoc Python/pytest commands.

**Run the app:**
```bash
make r          # INFO console only
make rd         # DEBUG log to wingman.log
```

**Tests:**
```bash
make test                             # full pytest suite + HTML report
make test1                            # single OCR check (region 33 / continue)
make test2                            # single OCR check (region 9 / incoming)
pytest tests/test_analyzer.py -k foo  # run one test by name
```

**Validation gates (run before releasing):**
```bash
make tp             # fast: test + ADR044/ADR045 runtime gates + performance preview
make tp-full        # full: tp + ADR037 PATH1/PATH2 real-OCR lane
make rr-path1-gate  # ADR044 deterministic runtime replay gate
make rr-live-path1-gate  # ADR045 live-screen capture gate
make ocr            # real-OCR integration tests (PATH1 + PATH2)
```

**Release workflow** (user-invoked only — see the git rule at the top of this file):
```bash
make wrelease   # commit version + performance artifacts, regenerate charts
make p "msg"    # stage, commit, push
```

**Calibration:**
```bash
make calibrate                   # recalibrate all crop regions interactively
make calibrate-crop CROP=respawn # recalibrate one named region
make add-crops                   # calibrate new images from test_screenshots/to_be_added/
```

## Architecture

The main loop runs in `wingman/main.py` (`main()`). Each 1.5-second tick captures the game screen, feeds it through the analyzer, and dispatches controller actions.

**Core modules:**

- `wingman/capture.py` — `Capture`: wraps `mss` to grab a BGR frame from a configured monitor region. Must be called from the thread that constructed it (mss uses thread-local storage).
- `wingman/crop_region.py` — `CropCoords` (NamedTuple) and helpers. All crop coordinates are fractions of the capture frame (0.0–1.0); x before y. Has no internal imports — safe to use anywhere.
- `wingman/analyzer.py` — `GameStateAnalyzer`: owns the `transitions`-based FSM (`GameState` enum), the EasyOCR thread pool, incoming template matching, respawn detection, and health/ammo OCR. Thread-local EasyOCR readers avoid races; `_ocr_init_lock` serializes first-time model download. Exposes `trigger_event()` for FSM transitions.
- `wingman/controller.py` — keyboard/mouse injection, click-to-crop helpers, hotkey bindings. Also houses `REGION_*` string constants used as log labels.
- `wingman/performance.py` — `PerformanceTracker`: records per-crop OCR timings and incoming→flare latency into bucketed histograms; writes `run_*.json` to `docs/performance/current/`.
- `wingman/replay.py` — `ScreenshotReplayCapture` (injects pre-recorded screenshots in place of live frames), `ReplayAssertionEngine` (records FSM state + timing for validator), `LivePathCaptureEngine` (captures real monitor frames during ADR045 live-screen test lane). Driven by YAML path configs under `tests/replay_paths/`.

**FSM states** (defined in `analyzer.py`):

`GAME_UNKNOWN → GAME_LOBBY → GAME_WAITING → GAME_STARTING → GAME_BATTLE → GAME_END_B → GAME_LOBBY`

Manual takeover (`i/j/k/l` keys) moves to `GAME_BATTLE_MANUAL`. `GAME_STARTING_STALLED` fires when matchmaking times out without a "Good Luck" detection.

**Configuration:** `wingman/config.yaml` defines the capture region, monitor index, all named crop coordinates (`crops:`), OCR/detection parameters, and performance regression thresholds. Crop coordinates use fractional screen positions and are edited by the calibration tooling.

**Test harness layers:**
1. `make test` — pytest unit/integration tests (fast, no game needed).
2. `make rr-path1-gate` — runs the real `wingman.main` loop with replayed PATH1 screenshots, then validates FSM transitions against assertions.
3. `make rr-live-path1-gate` — `live_screen_presenter.py` shows timed screenshots on-screen while the real monitor-capture path runs; validates round-trip timing.
4. `make ocr` — real-OCR tests on archived game screenshots in `test_screenshots/integration_test/` (slow, skipped if screenshots are all-black placeholders).

**Performance workflow:** Each session writes `docs/performance/current/run_*.json`. `make wrelease` copies them to `docs/performance/release/`, commits, and regenerates HTML charts. The performance regression check in `PerformanceTracker` compares the current session against the release baseline using the thresholds in `config.yaml`.

---

## Sequential Numbering — All `docs/` Subdirectories

Every file created under any subdirectory of `docs/` must have a zero-padded three-digit prefix:

```
001-my-document.md
002-another-document.md
```

Before creating a new file in any `docs/` subdirectory, list the existing files in that folder to find the highest number and increment by 1. Never guess or reuse a number — gaps and collisions break the sequence across sessions.

This applies to: `docs/adr/`, `docs/job-aids/`, `docs/performance/`, `docs/code-review/`, and any future subdirectory under `docs/`.

## ADR — Sequential Numbering

The general sequential numbering rule above applies. Additionally: before creating a new ADR, list the files in `docs/adr/` to find the highest existing number and increment by 1.

## ADR — Performance Changes

Performance ADRs must include actual log excerpts with timing data, not just estimates. ADR 019 is the reference example — before/after timings should come directly from production logs.

## ADR — Superseding Decisions

Do not modify an ADR that has status `Accepted`. If a decision is superseded, write a new ADR and reference the old one. This keeps the decision history intact.

## Command Execution

Always use the project Makefile and bash shell for commands in this repository.

- Prefer `make <target>` for tests, builds, and project tasks.
- Use bash as the execution shell for terminal commands.
- Do not bypass the Makefile with ad-hoc `python`, `pytest`, or shell commands when a Makefile target already covers the task.

## Diagrams

Always use Mermaid for diagrams in documentation. Never use ASCII text diagrams (no box-drawing characters, no `┌─┐` borders, no `→` arrow art). Wrap all diagrams in a fenced code block with the `mermaid` language tag.

Use a compatibility-first Mermaid profile for shared docs:

- Default to syntax that renders across common Mermaid versions/renderers.
- Keep node labels plain-language; do not put symbolic expressions (for example `>`, `<`, `<=`, `>=`, or punctuation-heavy logic) inside node declarations.
- Put equations/conditions in nearby bullets or surrounding prose, not in decision-node text.
- **Forbidden in node labels and edge labels:** `~`, `/`, `+`, `@`, `#`, `;` — these cause silent parse failures across common renderers. Use plain English instead: "and" for `+`, "approx" for `~`, "via" for `/`, "at" for `@`.
- **Edge labels** (`-->|text|` syntax) must contain only plain alphanumeric text, spaces, hyphens, and periods. No other punctuation.
- To include a special character in a node label, wrap the label in double quotes: `A["label text"]` — this requires Mermaid v10+; prefer omitting special characters for maximum compatibility.

Advanced Mermaid features are allowed when both conditions are met:

1. The target renderer/pipeline version is known to support the feature.
2. A simplified fallback diagram (or equivalent textual explanation) is provided for portability.

After creating or editing Mermaid blocks, verify they render in the target environment (not only in one local preview).

## Lock Release in Finally Blocks

Never use `try: lock.release() except RuntimeError: pass` in finally blocks. Always guard with:

```python
if self._some_lock.locked():
    self._some_lock.release()
```

The swallowed-exception pattern silently leaves the lock held if `release()` fails, permanently blocking future `acquire(blocking=False)` callers.

## Stoppable Daemon Threads

Any long-running daemon thread must be stoppable via a `threading.Event`. Use `event.wait(timeout=interval)` as the loop tick — not `while True: time.sleep(interval)`. The stop event must be set in `cleanup()` before the executor is shut down:

```python
# __init__
self._my_stop = threading.Event()

# thread body
while not self._my_stop.wait(timeout=5.0):
    ...

# cleanup()
self._my_stop.set()
```

## Lock Acquire Timeout on Main-Loop Paths

Any lock that can be held by a background thread must use `acquire(timeout=N)` when called from the main loop. Bare `with lock:` is only safe when both sides run in background threads. Return or skip the cycle gracefully on timeout:

```python
if not self._some_lock.acquire(timeout=5.0):
    logger.warning("lock timeout - skipping frame")
    return cached_result
try:
    ...
finally:
    self._some_lock.release()
```

## Document Heading Format

All new documents (job aids, performance docs, code reviews, ADRs, and any other docs under `docs/`) must begin with a title and a compact status/metadata table immediately after:

```
# <Document Type NNN> — <Title>

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-04-07 | 1.6.0           |
```

- Read `WINGMAN_VERSION` from `wingman/main.py` — never guess it.
- Use today's actual date.
- Use `Draft` for new documents; update to `Active` or `Accepted` once reviewed.
- **ADRs** must start as `Draft` when first created.
- Update an ADR to `Accepted` only after implementation is complete.

## Code Review Todos

Review files live in `docs/code-review/` and are numbered sequentially (`001-2026-03.md`, `002-…`, etc.). Each file covers one review cycle.

Closed review files are historical records. Do not edit a closed review to change findings, severity, or the narrative assessment after the cycle is complete.

Allowed before closure:
- Add a final resolution summary for items reviewed in that cycle.

After closure:
- Record later status changes in a new review-cycle file, not by rewriting the old file.
- Reference the original item ID and mark the current disposition explicitly: `Resolved`, `Deferred`, `Superseded`, or `Closed as stale`.
- Treat the latest review file as the authoritative source for current disposition.

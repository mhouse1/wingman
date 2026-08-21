# ADR 085 — Tech-Debt Remediation: Input Extraction, Typed Config, Parameter Object, Lint Gate

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Accepted | 2026-08-20 | 1.8.5           |

## Context

[Future 002](../FUTURE/002-engineering-capability-assessment.md) audited the tree
and kept six findings after filtering them against the project's actual resource
envelope (nights and weekends, no funding, no desktop-capable build agent). This
ADR records the batch that closed them, shipped together as v1.8.5.

The findings were not new. Four of the six were already diagnosed:

- [Future 001](../FUTURE/001-principal-architect-improvements.md) item 1 called
  for splitting `controller.py` in 2026-04, and its own 2026-08-14 status update
  recorded that the file had **grown** from 1,700 to 2,802 lines instead — and it
  reached 3,422 by the time this ADR was written.
- Future 001 item 2 called for a validated config model. The failure it predicts
  had already occurred: code review 015 found all five
  `behavior_tree.missile_evade` values silently equal to their code defaults.
- [Code review 015](../code-review/015-2026-08.md) item CR-015-05 recorded the
  comment-stripping calibration writer as **Open**, and code review 016 carried
  it forward unchanged.
- [Research 006](../research/006-coding-standard-adoption.md) specified the ruff
  rule set, wiring, and adoption sequence on 2026-08-16;
  [Research 007](../research/007-pycharm-ide-fit.md) re-affirmed it on 2026-08-20
  as the highest-value, IDE-independent item.

What was missing was execution, not analysis. The organising argument for doing
it now is in Future 002 section 3.1: the primary author of new code here is an AI
session, and a 3,400-line module is a cost paid **on entry to every session**, not
occasionally at debugging time. That inverts the usual "no time to refactor"
defence — the accumulated tax exceeds the one-off cost.

## Decision

### d1 — Extract `wingman/input_linux.py` (Future 002 A-01)

The XAUTHORITY bootstrap, XTest injection helpers, keysym alias table, `_XKeyEvent`,
and the `_LinuxXTestKeyboard` XRecord shim move out of `controller.py` into a module
of their own: 394 lines relocated, `controller.py` 3,422 → 3,090.

This is the seam Future 001 nominated, and it is chosen over the other candidates
because the subsystem produced **three distinct production incidents** — stuck
keys, delayed-echo false takeovers, and XTest release latency under load — and
therefore earns isolated ownership and its own tests.

**Platform agnosticism is preserved.** Wingman runs on Windows and Linux, and
this extraction must not quietly make it Linux-only. `controller.py` imports
`input_linux` *unconditionally* — it has to, to re-export the symbols other
modules reach for — so the module is only safe on Windows because every
module-scope import in it is stdlib, `Xlib` is imported lazily inside the
functions that use it, and `_LinuxXTestKeyboard` is instantiated solely behind
the `sys.platform != "win32"` check. That invariant is undocumented-fragile by
nature: hoisting a `from Xlib import ...` to the top of the file would break
Windows startup and **nothing on Linux would fail**. It is therefore recorded in
the module docstring and enforced by
`test_input_linux.py::test_module_scope_imports_are_stdlib_only`, which parses
the file and rejects any non-stdlib module-scope import.

The Windows mouse path (`ctypes.windll.user32`) deliberately stays inline in
`controller.py`. Extracting a matching `input_windows.py` would be symmetric but
is not justified by evidence — that code has produced no incidents, and it is
~30 lines against the ~400 this extraction moved.

Backward compatibility is preserved deliberately rather than cleaned up:
`controller.py` re-exports every moved symbol, because `tests/conftest.py`,
`wingman/move_game_window.py`, and `tests/test_controller_no_keyboard.py` import
them from there. `keyboard_module` stays a module-level name in `controller.py`
so the ~30 existing `monkeypatch.setattr(controller_module, "keyboard_module", …)`
calls keep working. The extraction is a move, not a redesign.

### d2 — Declarative config schema with fail-fast validation (A-03)

`wingman/config_schema.py` declares the shape of every key the program reads.
`load_config` validates against it and refuses to start on an unknown key, a wrong
type, an out-of-range value, or a missing required key — reporting **all** problems
at once, with a did-you-mean suggestion for unknown keys:

```
config.yaml failed schema validation (1 problem):
  - behavior_tree.missile_evade.max_maneuver_s: unknown key — did you mean 'max_manoeuvre_s'?
```

Three deliberate limits on scope:

- **No new dependency.** A pydantic model was the Future 001 prescription, but
  the failure mode to close is *unknown keys*, and 300 lines of declarative
  schema plus 60 lines of walker close it without adding pydantic to a project
  that already carries torch and easyocr.
- **Defaults are not injected.** The schema validates; it does not supply values.
  Injecting defaults would change behaviour at ~200 `cfg.get(key, default)` call
  sites for no correctness gain, and any divergence between a schema default and
  a code default would become a silent behaviour change.
- **`crops:` is a free-form map.** Crop names are calibration targets, not
  program constants, so unknown-key rejection applies to each crop's *shape*, not
  to the names.

A first draft of the schema over-constrained two values and broke
`test_make_y_replay_integration_smoke`: it marked `crops` **required** and gave
`loop_interval_sec` a 0.05 s floor, while the replay smoke lane legitimately
substitutes the analyzer, ships no crops, and ticks at 0.01 s. The rule the
schema now follows: *required* means the program cannot construct itself without
the key (`region`, `monitor`), and a bound is only declared where the program
genuinely requires it. Inventing plausible-looking limits produces false
rejections, and a gate that cries wolf is a gate people pass `validate=False` to.

Writing the schema surfaced live drift immediately: eleven keys that the code
reads were absent from `config.yaml` and would have been rejected as unknown the
moment anyone set them — the waiting-fallback block and `play_reclick_*`
(`tick_handlers.py`), `padlock_spread_missiles`, `debounce_consecutive_required`
(`analyzer.py`), and `min_crop_samples` / `min_reaction_events`
(`performance.py`). All are now declared, and
`test_no_config_key_is_read_without_being_declared` walks the AST of `wingman/`
each run so the schema cannot fall behind again.

### d3 — `ControllerConfig` parameter object (A-02)

`Controller.__init__` went from **21 parameters to 7**. The split is
collaborators versus configuration: analyzer, capture, exit event, callback, and
crops stay explicit arguments because they are wiring a reader needs to see;
every tuned value moves into a frozen `ControllerConfig`.

`ControllerConfig.from_config(cfg, **overrides)` becomes the single place that
knows which `config.yaml` block feeds which controller setting — that mapping was
previously inlined at the `main.py` call site. Unknown override names raise
`TypeError`, matching d2's contract on the YAML side. The dataclass is frozen so a
tactic thread cannot retune the controller mid-flight.

The config fields are unpacked to locals under their historical names at the top
of `__init__`. That is deliberate: it confines the change to the signature and
leaves the ~460-line body byte-identical, so a parameter-object refactor cannot
hide a semantic one. Decomposing that body is separate work.

### d4 — Ruff lint gate (A-04, executing Research 006)

Research 006's configuration is adopted verbatim — `E`, `W`, `F`, `B`, `SIM`,
`ARG`, `RET` at line-length 100 — wired as `make lint` inside `make tp`.
Four rules were added to the ignore list during baseline triage, each with its
reason recorded in `pyproject.toml`; the two that are judgement calls rather than
mechanics:

- **`E402`** — module preambles legitimately precede imports here
  (`WINGMAN_VERSION`, and the `sys.path` bootstrap the standalone scripts need).
- **`SIM102`** — ruff declines to autofix the seven remaining nested `if`s
  because collapsing them would relocate the comments between the two conditions.
  In this codebase those comments carry the incident that motivated the branch,
  so the nesting stays and the rule goes. Suppressing a style rule beats damaging
  the record.

`ruff format` is **not** in the gate yet. Research 006 step 3 calls for applying
it once as a dedicated, behaviour-free commit; until that lands, `make lint`
would fail on formatting alone and bury real findings. `make format` and
`make format-check` exist for that pass; `format-check` moves into `lint`
afterwards.

**Autofix is not free, and this is the caveat for future adoptions.** Ruff's
`F401` fix deleted the body of `test_wingman_main_imports`, whose entire purpose
was `try: from wingman import main / except: pytest.fail(...)`. To the linter the
import is an unused binding; to the test it *is* the assertion. The autofix left
`try: pass`, and the smoke test went on passing while asserting nothing — a
silent loss of coverage that no test failure would ever have reported. The test
now uses `importlib.import_module`, which states the intent in a form the linter
cannot misread. **Every autofix in a batch this size must be diff-reviewed
individually; a green suite afterwards does not prove the fixes were correct.**
Two branch-merging fixes (`SIM114`, `RET505`) were verified line-by-line against
`git show HEAD` for the same reason.

**Baseline: 225 findings, all resolved.** The rule set earned its selection on the
first run — Research 006 predicted bugbear would be the highest-value set "because
this codebase registers callbacks and spawns threads", and `B023` found exactly
that class of latent bug (d5 below). It also found a `pytest.raises(Exception)`
that never asserted the real exception, a duplicate test stub whose second
definition silently shadowed the first with an **incompatible signature**, an
undefined name in a type annotation, four dead locals in `analyzer.py`, and a
missing `assert` in a test written for this very ADR.

### d5 — Late-binding fix in the XRecord listener (found by d4)

`_stop_watcher` and `_record_handler` are defined inside the listener's reconnect
loop and captured `iter_done`, `d_ctrl`, `ctx`, `d_rec`, `_ef` and `display_name`
by reference rather than binding them.

`_stop_watcher` runs in its own thread and **can outlive the iteration that
created it**: the reconnect path sets `iter_done`, sleeps 3 s, then rebinds all
of them. The 0.5 s watcher poll normally wins that race, which is why this has
never been observed — but under the GIL and X starvation this codebase has
already measured (code review 016 recorded 6–8 s overshoots under load), a
late-waking watcher would read the *next* iteration's `iter_done` — a fresh,
unset Event — defeating the guard that exists to make it exit, and then disable
the **live** record context. That silently kills hotkeys, and with them the
SAF-001 manual-takeover path.

Both closures now bind their loop variables as default arguments.
`_record_handler` is consumed synchronously and cannot actually outlive its
iteration; it is bound anyway so the rule holds for every closure in the loop
rather than resting on a call-ordering argument a later edit could invalidate.

### d6 — Every swallowed exception now logs (A-06)

The thirteen `except Exception: pass` sites are gone. The classification that
matters is by consequence, not by frequency:

- **Key-release failures log at ERROR**, naming the key and what a failed
  release leaves behind. A swallowed release is the start of a stuck-key
  incident — the class that has already cost this project three of them. This
  covers `_eject_key`, the disengage-roll and missile-evade release paths, and
  the cleanup release-all safety net. The message is platform-aware via
  `_LATCH_NOTE`: XTest key state is server-side and survives process death on
  Linux, while on Windows the injected key stays down in the OS input queue. The
  consequence is identical — uncommanded flight input the operator cannot clear
  — so the ERROR is unconditional and only the named mechanism differs. A first
  draft hardcoded "latched in the X server" into these platform-agnostic paths,
  which would have printed a false explanation on every Windows run.
- **The manual-takeover FSM transition logs at ERROR.** Its guard wrapped
  `trigger_event("manual_takeover")`; a swallow there leaves the FSM in
  `GAME_BATTLE` while the operator believes they have control. The flight-input
  stop has already run by that point, so the aircraft is safe — but the
  divergence must be visible. SAF-001.
- **Benign shutdown and diagnostic paths log at DEBUG** with the exception text:
  Display close during reconnect, the debug-image write, the portal token read.

`make tp` now also fails if a new one appears, via ruff `SIM105`.

### d7 — Comment-preserving calibration writer (A-08, closing CR-015-05)

`_save_config` was `yaml.dump(cfg)`, which reformats the whole file and deletes
every comment in it. Calibration only ever changes `crops.<name>.coords`, so the
edit is now applied to those lines **textually** and every other byte is left as
it was. A `make calibrate` run changes exactly two lines per crop.

`ruamel.yaml` was considered and rejected: the surgical path needs no dependency,
and it guarantees byte-identity outside the edited lines rather than merely
attempting to preserve comments through a round trip.

The result is re-parsed and compared to the intended mapping before it is
written. If the surgical path cannot express the change — a crop removed, an
unexpected entry shape — it falls back to a full dump, but backs the original up
to `config.yaml.bak` and says so on stdout first. Silent data loss is replaced by
loud, recoverable data loss.

## Consequences

- `Controller` construction is a breaking signature change. All 29 call sites
  across `main.py` and nine test files are updated in this change; a stray
  keyword now raises `TypeError` rather than being silently ignored.
- `make tp` gains `lint` as its first target, so it fails fast and cheap.
- 39 new tests across four files: `test_config_schema.py` (16),
  `test_input_linux.py` (9), `test_controller_config.py` (8),
  `test_calibrate_config_writer.py` (6). All four are wired into `make test`.
- The AST drift guard in `test_config_schema.py` means **adding a config key now
  requires adding it to the schema in the same change**. That coupling is
  intentional: a key not in the schema is a key that fails at startup.
- **CR-015-05 is resolved.** Its disposition belongs in the next review-cycle
  file per the closed-file rule, not in code review 015 or 016.
- Future 002 findings A-01, A-02, A-03, A-04, A-06 and A-08 are closed. A-01 is
  closed only as to the *extraction*: `Controller.__init__`'s body is still ~460
  lines and `controller.py` is still 3,090. The next seam is a decision for its
  own ADR.

## Verification

`make tp-full` passed end to end (exit 0) on 2026-08-20: `lint` clean, 666 tests
passed / 2 skipped, ADR 044 deterministic replay gate PASS, ADR 045 live-screen
gate PASS ("live runtime validation succeeded"), and the ADR 037 real-OCR lane
11 passed. `make reqs-gate` PASS — no `@relation` marker was disturbed by the
argument renames.

**Live session, 2026-08-20 05:45–05:50 (v1.8.5, 5 m 41 s):** one mission,
100% click-to finish, 2 respawns, 0 spawn crashes, **0 ERROR lines in 2,457
log lines**. Every changed path was exercised:

- d2 — `Configuration loaded from wingman/config.yaml` with validation active.
- d3 — `Controller` built through `ControllerConfig.from_config`; all nine
  hotkeys registered.
- d1 — the extracted module resolved keycodes live
  (`XKey: registered 'backspace' (keycode=22)` …) and set up XAUTHORITY.
- d6 — SIGTERM took the graceful path it was widened to protect:

```
05:50:56,217 [INFO] Exit requested, shutting down
05:50:56,233 [INFO] Controller: cancel_mission called
05:50:56,266 [INFO] Controller: all injectable keys released
05:50:56,267 [INFO] Controller: all keyboard hooks deregistered
05:50:56,283 [INFO] ThreadPoolExecutor shut down successfully
```

An `XQueryKeymap` immediately after exit returned **no held keycodes** — the
stuck-key class this ADR's logging targets did not occur, and would now be
visible at ERROR if it had.

Incidentally confirming ADR 080: the health dropout histogram closed at
`{"lt2s": 53, "2to5s": 0, "5to10s": 0, "10to20s": 0, "gte20s": 0, "over_5s": 0,
"p95_s": 1.6, "max_s": 1.63}` — no dropout crossed 2 s, against the 7–25 s gaps
that motivated that ADR.

**Unrelated observation for a future decision (not addressed here):** the fuel
crop shows the fragment-churn signature ADR 080 fixed for `health`. Live reads
oscillated between plausible values (100/97/94) and implausible ones (8/5/2)
within the same climb, and `climb — fuel 2% reached floor 10% — afterburner
released` fired 7 times in 5 minutes on those misreads. It is **not** a
regression from this batch: the previous session's log (2 h 44 m, pre-v1.8.5)
carries 461 sub-20% fuel reads and 118 such releases. Worth its own
measurement-first ADR in the ADR 080 mould.

## Related

- [Future 002](../FUTURE/002-engineering-capability-assessment.md) — the findings.
- [Future 001](../FUTURE/001-principal-architect-improvements.md) — items 1 and 2,
  diagnosed 2026-04 and unshipped until now.
- [Research 006](../research/006-coding-standard-adoption.md) — the ruff
  specification this executes; its step 5 asked for this ADR.
- [Research 007](../research/007-pycharm-ide-fit.md) — the enforcement-layer
  argument for gating in the Makefile rather than in an IDE.
- [Code review 015](../code-review/015-2026-08.md) — CR-015-05.
- ADR 066 — the StrictDoc precedent for spiking a tool before gating on it.
- ADR 070 — the missile-evade config block whose silent defaults motivated d2.

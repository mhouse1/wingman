# ADR 047 — Host Environment Pre-flight Check

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-06-13 | 1.6.19          |

## Context

Setting up Wingman on a new machine requires several tools to be present and correctly
configured before any `make` target or runtime invocation will succeed. Current
failure modes are cryptic: a missing `uv` binary surfaces as a shell error buried in
Makefile output; a missing `easyocr` dependency surfaces as a Python `ImportError`
deep in the OCR worker thread; a Python version below 3.10 causes a syntax error on
match statements rather than a clear version message.

There is no single step that verifies the environment before the user attempts to run
the project for the first time. This is a recurring friction point after clones, OS
upgrades, or environment resets.

## Decision

Add a host environment pre-flight check as a standalone `make` target (`make preflight`)
that validates all required tools and Python packages before any other target is run.
The check must not run automatically inside other targets; it is opt-in.

### What to check

**System tools** — must be findable on `PATH`:

| Tool    | Minimum version | Why required                                      |
|---------|-----------------|---------------------------------------------------|
| Python  | 3.10            | `pyproject.toml` lower bound; project uses match statements and `str \| None` type union syntax |
| uv      | any             | All Makefile targets invoke `uv run --active` or fall back to bare Python |
| make    | any             | All project workflows go through the Makefile     |
| git     | any             | Version history, `make wrelease` commit step, performance tracking |

**Python packages** — verify importable inside the active venv (via `uv run --active`):

| Package     | Reason                                              |
|-------------|-----------------------------------------------------|
| cv2         | Core image processing; template matching and crop extraction |
| easyocr     | OCR for all crop regions (heavy torch dependency)   |
| mss         | Screen capture; `Capture` class depends on it       |
| keyboard    | Key injection; requires elevated privileges on Linux |
| numpy       | Frame arrays; used throughout                       |
| yaml        | Config loading (`wingman/config.yaml`)              |
| transitions | FSM library for `GameStateAnalyzer`                 |
| plotly      | Performance chart generation                        |
| pandas      | Performance CSV handling                            |

**Runtime privileges** (advisory, not hard-fail):

- On Linux, the `keyboard` library requires root or `input` group membership for global
  key injection. The check should detect the platform, attempt a privilege probe, and
  print a warning if insufficient — not block. The project can still run OCR tests and
  calibration without keyboard injection.

## Scope

In scope:

- A single Python script `tests/preflight.py` that performs all checks and exits 0 on
  pass, non-zero on any hard failure.
- A `make preflight` target that invokes it.
- Clear, actionable error messages for each failing check: what is missing, what version
  was found vs required, and the command to fix it.

Out of scope:

- Automatic remediation (no `uv sync` invocation from within the check).
- IDE or CI gating — this is a developer tool, not a required gate in `make test`.
- GPU/CUDA availability — out of scope because the project is CPU-only by default
  (`use_gpu: false` in config).

## Implementation Approach

1. Write `tests/preflight.py`:
   - Each check is a function that returns `(passed: bool, message: str)`.
   - Aggregate results, print a summary table, and exit with the count of failures.
   - System tool checks use `shutil.which()` and `subprocess.check_output --version`.
   - Python version check uses `sys.version_info`.
   - Package checks use `importlib.util.find_spec()` inside the current interpreter
     (the script will be invoked via `uv run --active python` so the venv is active).
   - Keyboard privilege probe: on Linux only, attempt `open('/dev/input/event0', 'rb')`
     and catch `PermissionError`; emit a warning, not a failure.

2. Add `make preflight` target:

   ```makefile
   preflight:
       $(PYTHON_RUN) tests/preflight.py
   ```

3. Output format — one line per check, aligned columns:

   ```
   [PASS] python         3.12.3 >= 3.10
   [PASS] uv             0.4.25
   [PASS] make           4.4.1
   [PASS] git            2.45.2
   [PASS] cv2            4.10.0
   [PASS] easyocr        1.7.2
   [PASS] mss            9.0.1
   [WARN] keyboard       0.13.5  (Linux: root or 'input' group required for key injection)
   [PASS] numpy          1.26.4
   [PASS] yaml           (pyyaml 6.0.2)
   [PASS] transitions    0.9.2
   [PASS] plotly         6.6.0
   [PASS] pandas         2.3.3
   
   Pre-flight: 13 passed, 0 failed, 1 warning.
   ```

## Acceptance Criteria

- `make preflight` exits 0 on a fully configured machine.
- `make preflight` exits non-zero and prints an actionable message for each of:
  - Python < 3.10 installed.
  - `uv` not on PATH.
  - Any required package not importable.
- On Linux without keyboard privileges, `make preflight` exits 0 with a warning line.
- The script itself has no runtime dependency on any Wingman module — it must be
  runnable before `uv sync` has been run (using only the stdlib for system checks;
  package import checks run inside the active venv).

## Consequences

Positive:

- First-run experience on a new machine is guided rather than cryptic.
- Reduces support burden for environment setup failures.
- Serves as living documentation of the exact tool and package requirements.

Trade-offs:

- One more file to keep in sync with `pyproject.toml` as dependencies change.
- Advisory keyboard warning may cause confusion if users expect a clean pass on
  machines where they do not intend to run Wingman's runtime injection path.

## Alternatives Considered

1. Add environment checks inline at `wingman/main.py` startup.
   - Rejected: couples startup path to diagnostic logic; fails only when the full
     runtime is invoked rather than giving a dedicated check step.

2. Use a shell script instead of Python.
   - Rejected: package import checks must run inside the venv, which is awkward from a
     shell script; a Python script invoked via `uv run --active` already has the right
     environment.

3. Gate `make test` on `make preflight`.
   - Rejected: pre-flight adds latency and would break CI-style workflows that manage
     their own environment. Opt-in is the right scope.

## References

- `pyproject.toml` — authoritative dependency list
- `Makefile` — `PYTEST_RUN` and `PYTHON_RUN` variable definitions
- ADR 013 — Automated test architecture

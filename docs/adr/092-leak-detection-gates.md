# ADR 092 — Leak Detection: Source-Site Guard and Log-Based Gate

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-23 | 1.8.5           |

## Context

ADR 091 fixed a leak that ran for two months at up to 1,666 MB/h. Research 009
generalises the lesson. Neither prevents recurrence — nothing in the test suite
or the release gates would notice if the same defect, or a different one,
returned tomorrow.

The audit that motivated this ADR found three specific gaps:

1. `test_repeated_key_events_open_exactly_one_display` pins the fixed function
   only. A new per-operation handle anywhere else fires nothing.
2. `_linux_click` still constructs a `Display` per call — the identical defect,
   with no test of any kind.
3. **No test or gate fails on actual memory growth.** The `resource_monitor`
   tests feed synthetic curves to the verdict function and check it labels them
   correctly; they test the reporter, not the system, and would all pass while
   wingman leaked a gigabyte an hour. The leak was found by a two-hour live
   session, and nothing automated would catch its return.

Gap 3 is the one that matters. Research 009's central finding is that the cause
sat where nobody was looking, so a guard that only watches known patterns
institutionalises looking where we already looked.

## Decision

Build **both** mechanisms, with deliberately separated roles.

| | Source-site guard | Log-based leak gate |
|---|---|---|
| Detects | the known *cause* pattern | any leak, by *symptom* |
| Runs | every commit, in `make test` | against real session data, in `make leak-check` |
| Input | the source tree | `logs/wingman_*.log` |
| Deterministic | yes | no — depends on what has been flown |
| Catches unknown causes | no | yes |
| Latency to detection | seconds | one long session |

They are complements, not alternatives: the guard is cheap and immediate but
narrow; the gate is broad but needs a qualifying session to say anything.

## Design 1 — Source-site guard

A test that pins the set of handle-construction sites, so adding one becomes a
decision rather than a merge.

**Discovery by AST, not regex.** Walk each `wingman/*.py` for `ast.Call` nodes
whose callee resolves to a watched constructor name, recording the **enclosing
function** rather than the line number — line numbers shift on every unrelated
edit and would make the test a maintenance tax.

**The registry is `(module, function) -> count`**, with a justification per
entry. Current state, from an AST scan on 2026-08-23:

| module | function | n | justification |
|--------|----------|---|---------------|
| `input_linux.py` | `_linux_click` | 1 | per-call; low hundreds per session, and its sleeps must not be held under the injection lock (ADR 091, "Not done") |
| `input_linux.py` | `_shared_xtest_display` | 1 | **the approved factory** — one per process |
| `input_linux.py` | `_listener_loop` | 3 | once per listener start: setup, record, control connections |
| `input_linux.py` | `_record_handler` | 1 | guarded by `if new:` — only when keys are registered after the loop starts |
| `move_game_window.py` | `_connect` | 1 | one-shot tooling, not on the tick path |

The failure message must teach, not just fail:

```
New Display() construction site: wingman/foo.py:bar

If this runs per operation on the tick path, it is the ADR 091 defect —
each construction retains ~16.2 KB permanently. Use the shared factory.
If it is justified, add it to _APPROVED_SITES with the reason.
```

**Watched constructors** start as `Display` alone. The list is extensible, and
Research 009's table of candidate categories is where to look when adding to it.
Deliberately not generalised further now: a guard that fires on everything gets
disabled.

## Design 2 — Log-based leak gate

`scripts/leak-check.py`, exposed as `make leak-check`.

**Parse.** Read `logs/wingman_*.log` and the live `wingman.log`, extracting
`RESOURCE elapsed=...` lines into a per-session series.

**Anchor and qualify.** Rates are measured from a post-warm-up anchor, never
from t=0 — wingman allocates over a gigabyte loading OCR readers in the first
five minutes, and dividing that across a session manufactures a phantom leak. A
session qualifies only with a post-warm-up window of at least `min_window_h`
and at least `min_samples` samples.

**Three outcomes, never two.**

```mermaid
flowchart TD
    A[Parse sessions from logs] --> B{Any qualifying session}
    B -->|no| C[INSUFFICIENT DATA exit 2]
    B -->|yes| D[Measure post-warmup growth rate]
    D --> E{Rate under the pass threshold}
    E -->|yes| F[PASS exit 0]
    E -->|no| G[FAIL exit 1]
```

`INSUFFICIENT` is **not** a pass. This is the whole reason the gate needs care:
short sessions underread the leak by roughly tenfold — 0.75 h runs measured
+120 and +288 MB/h while the same code leaked over 1,300 MB/h in long ones. A
green light derived from a twenty-minute session retires the question while the
defect is live, which is worse than no gate at all. The existing
`resource_monitor` already refuses to attribute on short windows; this inherits
that discipline rather than inventing one.

**Signal preference.** Use `mi_use` (live allocation) when the log carries it.
Fall back to `rss` for older logs, with wider thresholds and the verdict marked
lower-confidence, because RSS includes arena retention that is not a leak — the
post-fix session showed RSS growing +109 MB/h while live allocation was flat,
and reporting that as a leak would be a false positive.

**Wingman-side and game-side must be separated.** MetalStorm leaks ~215 MB/h on
its own, consistently across every session measured. That must never fail a
wingman gate. Report it, attribute it, do not gate on it.

**Report the distribution, not just the verdict.** Print the latest qualifying
session against the median and range of prior qualifying sessions, so a slow
regression is visible before it crosses a threshold.

**Thresholds live in `config.yaml`** under a `leak_gate:` section, matching the
project convention that tuning values never sit in code or in a requirement.

## Where they run

- **Source-site guard** — `make test`. Deterministic, milliseconds.
- **Leak gate** — `make leak-check`, wired into `make tp`.

The gate is deliberately **not** in `make test`. Its result depends on a mutable
local directory and on whether anyone has flown recently; a suite that fails for
reasons unrelated to the commit gets ignored, and one that passes vacuously is
worse.

Exit-code handling differs by caller, and this is intentional:

| caller | PASS | FAIL | INSUFFICIENT |
|--------|------|------|--------------|
| `make leak-check` | 0 | 1 | 2 |
| `make tp` | continue | **block** | warn loudly, continue |
| `make wrelease` | continue | **block** | **block** |

Releasing a build whose memory behaviour has never been measured on a long
session is exactly how the last leak shipped, so `wrelease` treats absence of
evidence as a failure. `tp` runs far more often and must stay usable without a
recent soak.

## Validation

The gate is unusually testable: ~170 archived logs exist with known answers.
Unit tests assert the corpus outcomes directly.

| log | window | signal | expected |
|-----|--------|--------|----------|
| `wingman_20260823_002829.log` | 6.77 h | mi_use +1491 | **FAIL** |
| `wingman_20260823_065033.log` | 2.26 h | mi_use +974 | **FAIL** |
| `wingman_20260823_124159.log` | 1.59 h | mi_use +824 | **FAIL** |
| `wingman_20260823_151050.log` | 1.34 h | mi_use +9 | **PASS** |
| `wingman_20260823_230230.log` | 3.01 h | mi_use +2 | **PASS** |
| `wingman_20260821_083045.log` | 0.75 h | — | **INSUFFICIENT** |
| any pre-2026-08-21 log | — | no RESOURCE lines | **INSUFFICIENT** |

Corpus assertions pin behaviour against real data but depend on files that are
gitignored and machine-local, so they must **skip, not fail**, when the corpus is
absent. Synthetic fixtures carry the deterministic edge cases: warm-up excluded
from the rate, game-side growth not failing the wingman gate, `mi_use` preferred
over `rss`, malformed and truncated lines ignored.

## Consequences

- A new per-operation handle fails at commit time with an explanation.
- A leak from **any** cause fails the release gate once a qualifying session
  exists — the property the last two months lacked.
- `make wrelease` now requires a recent long session. That is a real workflow
  cost, and it is the point.
- The guard's registry needs updating whenever a construction site is
  legitimately added. That friction is the mechanism, not a side effect.
- Neither mechanism detects a leak *during* a session. ADR 090's memory guard
  remains the only in-flight protection.

## Alternatives considered

**Only the source-site guard.** Rejected: it cannot catch what it was not told
to watch, which is the exact failure mode Research 009 documents.

**Only the log gate.** Reasonable, and it was the stronger of the two if forced
to choose. Rejected because the guard is roughly twenty lines and moves
detection from hours to seconds for the pattern we know bites.

**A memory assertion in the ADR 044/045 runtime gates.** Rejected: those replay
a short path injecting a handful of key events, and the defect needed ~80,000
constructions to become visible. It would have passed throughout the leak.
Accumulating defects need a duration gate, not a unit test.

**Failing `make test` on log analysis.** Rejected — see "Where they run".

## References

- ADR 091 — the fix, and `_linux_click` recorded as deliberately not done
- Research 009 — the generalised defect class and detection lessons
- ADR 090 — the in-flight memory guard, unchanged by this ADR
- Performance 008 — the incident record and the underreading-short-sessions data

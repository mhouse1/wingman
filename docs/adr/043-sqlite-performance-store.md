# ADR 043 — SQLite Performance Store

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-05-30 | 1.6.13          |

## Context

`PerformanceTracker` currently writes one JSON file per session to
`docs/performance/current/run_YYYYMMDD_HHMMSS.json`.  Each file stores
pre-aggregated statistics (mean, p50, p95, p99) for five OCR crops and
one reaction-latency series.

The `_aggregate_folder()` function loads **every** JSON file into memory
on each call, re-runs weighted-mean aggregation in Python, and discards
raw samples after emission.  This creates several pain points:

- Cross-version and date-range queries require loading all files and
  filtering in Python.
- `_aggregate_folder` re-reads the same files repeatedly (called from
  both the regression check and the HTML chart renderer).
- Comparing current performance against the `release/` baseline uses a
  separate folder of identical-format files, so the aggregation code
  runs twice with no shared cache.
- There is no way to query "all runs on this machine" vs "all runs across
  machines" — machine identity is not recorded.

The current file count (`current/`) is ~30 files and growing; scaling
pain is low today but will increase as continuous capture sessions
accumulate.

## Decision

Replace the per-session JSON files with a single SQLite database
(`docs/performance/performance.db`).  `PerformanceTracker` reads and
writes via the standard-library `sqlite3` module — no new dependency.

### Schema

```sql
CREATE TABLE runs (
    run_id      TEXT PRIMARY KEY,   -- 'YYYYMMDD_HHMMSS'
    version     TEXT NOT NULL,
    start_ts    REAL NOT NULL,
    end_ts      REAL,
    rounds      INTEGER NOT NULL DEFAULT 0,
    hostname    TEXT                -- platform.node(); NULL for legacy imports
);

CREATE TABLE ocr_samples (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    crop        TEXT NOT NULL,      -- 'incoming', 'respawn', 'health', etc.
    seconds     REAL NOT NULL
);

CREATE TABLE reaction_samples (
    id          INTEGER PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    seconds     REAL NOT NULL
);

CREATE INDEX idx_ocr_run    ON ocr_samples(run_id, crop);
CREATE INDEX idx_react_run  ON reaction_samples(run_id);
```

Raw samples replace pre-aggregated statistics.  Percentiles and means
are computed by SQL queries at read time, or by loading the relevant
sample rows and calling the existing `_compute_stats` helper.

### Migration

A one-time `migrate_json_to_db(folder, db_path)` utility converts
existing `run_*.json` files.  JSON files report only aggregated stats
(mean, p50, p95, p99, n) — raw samples are not recoverable, so each
legacy run is imported as a single synthetic sample row per crop that
preserves the mean and n count.  Reaction data is imported the same way.

The `release/` folder follows the same migration path; baseline runs are
tagged with a `release_baseline = 1` column on the `runs` table (or
stored in a separate `release.db` — TBD during implementation).

### PerformanceTracker changes

| Current method         | Change                                          |
|------------------------|-------------------------------------------------|
| `__init__`             | Open (or create) `performance.db`               |
| `on_session_end`       | Insert run + bulk-insert raw sample rows        |
| `_aggregate_folder`    | Replace with SQL `AVG` / `GROUP BY` query       |
| `_load_run_file`       | Remove                                          |
| `on_enter_game_lobby`  | Keep round-buffer flush; accumulate into session buffer as now |

The in-memory session buffers (`_session_crops`, `_session_reaction`)
are unchanged during a run.  Only the persistence layer changes.

### Query examples

Aggregate all runs for the current version:
```sql
SELECT crop,
       COUNT(*)         AS n,
       AVG(seconds)     AS mean,
       MAX(seconds)     AS max
FROM   ocr_samples
JOIN   runs USING (run_id)
WHERE  version = '1.6.13'
GROUP  BY crop;
```

Trend: per-day mean OCR latency for `incoming` crop:
```sql
SELECT date(start_ts, 'unixepoch') AS day,
       AVG(seconds)                AS mean_ocr_s
FROM   ocr_samples
JOIN   runs USING (run_id)
WHERE  crop = 'incoming'
GROUP  BY day
ORDER  BY day;
```

## Consequences

**Benefits**
- Single file replaces growing directory of small JSON files.
- Cross-version and date-range queries are plain SQL — no Python loops.
- Raw samples are preserved, enabling accurate percentile recalculation
  as the sample set grows across sessions.
- Duplicate aggregation passes are eliminated; `_aggregate_folder` is
  called once per report generation.
- Machine identity (`hostname`) enables per-machine baselines.

**Risks / mitigations**
- SQLite write contention: `PerformanceTracker` is the only writer and
  writes only at session end, so WAL mode is not required.  Use
  `check_same_thread=False` and the existing `_lock` for thread safety.
- Legacy data fidelity: migrated rows carry only the mean, not the
  original distribution.  This is acceptable — legacy data is already
  aggregated in the JSON files.
- Database file in `docs/`: consistent with current JSON location; `docs/performance/performance.db` is added to `.gitignore` (matching the treatment of `docs/performance/current/`).

## Alternatives Considered

**Keep JSON files, add an index file** — avoids migration but does not
solve the repeated-load or query-flexibility problems.

**DuckDB / pandas** — richer analytics but adds a dependency for a
use-case that standard `sqlite3` covers without installation.

**PostgreSQL / remote store** — overkill for a single-machine tool with
no multi-user requirement.

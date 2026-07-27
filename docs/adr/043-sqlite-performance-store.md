# ADR 043 — SQLite Performance Store

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Deferred | 2026-07-17 | 1.6.24          |

## Deferred — Not Currently Worth Implementing

Decision (2026-07-17): shelve this ADR rather than implement it now.

The revised aggregates-as-rows design (below) is sound and solves the
pain points in Context, but at current scale those pain points are
theoretical, not measured: 321 `release/` sessions accumulated over
~2 months total ~1.4 MB and parse in well under a second.
`_aggregate_folder` already does the cross-session weighted-mean math
in Python today, and `tests/runtime_performance_tracking.py` already
builds per-version chart points from the file globs — nothing
currently needed is blocked by the JSON approach.

Implementing this ADR would require rewriting `PerformanceTracker`'s
write path, `tests/runtime_performance_tracking.py`'s read path, and
`make wrelease`, plus a migration script — real cost for a data store
holding ~1.4 MB. It would also trade away human-readable git diffs on
individual session files for a binary `release.db`. Context's own
framing ("scaling pain is low today but will increase...") is
optimizing for a problem that doesn't exist yet, which runs against
this project's stated principle of not designing for hypothetical
future requirements.

**Revisit if either trigger occurs:**
- File count grows enough that glob/parse time becomes measurably
  visible in `make tp` / `make wrelease` runtimes (order of thousands
  of sessions, not hundreds).
- A real need emerges for ad-hoc queries the current release/preview
  scripts can't answer (e.g. arbitrary cross-version, cross-machine
  slicing).

The design below is left intact for that future revisit rather than
deleted.

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

Replace the per-session JSON files with two identically-schemed SQLite
databases: `docs/performance/current.db` (gitignored, mirrors today's
`current/` folder) and `docs/performance/release.db` (committed,
mirrors today's `release/` folder). `PerformanceTracker` reads and
writes via the standard-library `sqlite3` module — no new dependency.

Two files rather than one db with an `is_release` flag: git tracks
whole files, not rows, so a single db mixing in-flight current-period
data with the permanent baseline can't be partially gitignored the way
the current two-folder split allows. Keeping them as separate files
preserves that boundary exactly.

**The database stores per-session aggregates, not raw samples.**  An
earlier draft of this ADR stored one row per OCR/reaction sample.
Measurement against the existing `docs/performance/release/` history
(321 sessions, ~675k combined samples) showed that raw-sample storage
would run ~40 MB for that same window versus ~1.4 MB as aggregated
JSON today — a ~30x cost for a capability (exact cross-session
percentile recomputation) this project does not currently need. The
schema below is a relational mirror of the existing per-session JSON
shape instead, keeping the storage footprint roughly at parity with
today while still solving the actual pain points (repeated Python
re-aggregation, no cross-version/date queries, no shared cache, no
machine identity).

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

CREATE TABLE crop_stats (
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    crop        TEXT NOT NULL,      -- 'incoming', 'respawn', 'health', etc.
    n           INTEGER NOT NULL,
    mean        REAL NOT NULL,
    p50         REAL NOT NULL,
    p95         REAL NOT NULL,
    p99         REAL NOT NULL,
    PRIMARY KEY (run_id, crop)
);

CREATE TABLE reaction_stats (
    run_id      TEXT PRIMARY KEY REFERENCES runs(run_id),
    n           INTEGER NOT NULL,
    mean        REAL NOT NULL,
    p50         REAL NOT NULL,
    p95         REAL NOT NULL,
    p99         REAL NOT NULL,
    max         REAL NOT NULL
);

CREATE INDEX idx_crop_stats_version ON crop_stats(run_id);
CREATE INDEX idx_runs_version       ON runs(version);
```

The same schema is applied to both `current.db` and `release.db`.

Per-session percentiles are still computed once, in Python, by the
existing `_compute_stats` helper at `on_session_end` — exactly as
today. What moves into SQL is the *cross-session* merge: weighted mean
across many runs (`SUM(mean * n) / SUM(n)`), version/date filtering,
and the release-vs-current-period comparison. Cross-session
percentiles remain an approximation (most-recent-session's p95/p99
used as a proxy) — this is unchanged from the current `_aggregate_folder`
behavior, not a regression.

`make wrelease` copies rows from `current.db` into `release.db`
(`ATTACH 'current.db'; INSERT INTO release_db.runs SELECT * FROM
current_db.runs; ...`, mirroring the per-table structure) and then
clears `current.db` — the same shape as today's `cp` + `rm`, just
row-copy instead of file-copy.

### Migration

A one-time `migrate_json_to_db(folder, db_path)` utility converts
existing `run_*.json` files by inserting one `runs` row, one
`crop_stats` row per crop, and one `reaction_stats` row per file —
a direct field-for-field copy, since the JSON already stores exactly
these aggregates. No sample data is synthesized or lost. Run once for
`current/` → `current.db` and once for `release/` → `release.db`.

### PerformanceTracker changes

| Current method         | Change                                          |
|------------------------|-------------------------------------------------|
| `__init__`             | Open (or create) `current.db`                   |
| `on_session_end`       | Insert `runs` row + one `crop_stats` row per crop + one `reaction_stats` row into `current.db` (same `_compute_stats` output as today, written to SQL instead of JSON) |
| `_aggregate_folder`    | Replace with SQL `SUM`/`GROUP BY` weighted-mean query over `crop_stats` JOIN `runs`, parameterized by which db (`current.db` or `release.db`) to query |
| `_load_run_file`       | Remove                                          |
| `on_enter_game_lobby`  | Keep round-buffer flush; accumulate into session buffer as now |

The in-memory session buffers (`_session_crops`, `_session_reaction`)
and the round-histogram logging path are unchanged. Only the
persistence layer changes.

`tests/runtime_performance_tracking.py` (`_release_mode`, `_preview_mode`,
`_build_points_from_runs`) currently globs `release/run_*.json` and
`current/run_*.json` directly — this is in scope for the migration and
must be rewritten to query `release.db` / `current.db` instead of
globbing files. `make wrelease`'s `cp` / `rm` steps are replaced by the
`ATTACH`-and-copy above; `git add -f` targets `release.db` instead of
`release/*.json`.

### Query examples

Weighted-mean aggregate across all runs for a given version:
```sql
SELECT crop,
       SUM(n)                    AS n,
       SUM(mean * n) / SUM(n)    AS weighted_mean
FROM   crop_stats
JOIN   runs USING (run_id)
WHERE  runs.version = '1.6.24'
GROUP  BY crop;
```

Trend: per-day weighted-mean OCR latency for `incoming` crop:
```sql
SELECT date(runs.start_ts, 'unixepoch')  AS day,
       SUM(mean * n) / SUM(n)            AS mean_ocr_s
FROM   crop_stats
JOIN   runs USING (run_id)
WHERE  crop_stats.crop = 'incoming'
GROUP  BY day
ORDER  BY day;
```

Release baseline for the current version (run against `release.db`):
```sql
SELECT crop, SUM(n) AS n, SUM(mean * n) / SUM(n) AS weighted_mean
FROM   crop_stats
JOIN   runs USING (run_id)
WHERE  runs.version = '1.6.24'
GROUP  BY crop;
```

## Consequences

**Benefits**
- Two files replace two growing directories of small JSON files, at
  roughly the same total size (aggregates in, aggregates stored).
- Cross-version and date-range queries are plain SQL — no Python loops.
- Duplicate aggregation passes are eliminated; both the regression
  check and the chart renderer query the db instead of each
  re-globbing and re-parsing every JSON file.
- Machine identity (`hostname`) enables per-machine baselines.
- The `current.db` / `release.db` split preserves today's git boundary
  (in-flight data ignored, promoted baseline committed) exactly —
  `make wrelease` copies rows between dbs instead of copying files
  between folders.

**Risks / mitigations**
- SQLite write contention: `PerformanceTracker` is the only writer and
  writes only at session end, so WAL mode is not required.  Use
  `check_same_thread=False` and the existing `_lock` for thread safety.
- Cross-session percentile accuracy: unchanged from today — percentiles
  are still per-session values, and cross-session "trend" percentiles
  remain a most-recent-session proxy, not a true recomputation. If
  exact cross-session percentiles are ever needed, that's a separate,
  deliberate feature (optional raw-sample logging, explicitly
  size-bounded/pruned, likely kept out of the committed `release.db`
  entirely) — not part of this migration.
- Database file committed to git: `docs/performance/release.db` is the
  direct replacement for today's force-added `release/*.json` files and
  is committed the same way on `make wrelease`. Because rows are
  per-session aggregates (not raw samples), the file's growth rate
  tracks today's JSON growth rate (~1.4 MB for 321 sessions observed
  so far), not the ~30x-larger raw-sample alternative that was
  considered and rejected below. Being a single binary file, its git
  diffs won't be human-readable the way `release/*.json` diffs are —
  acceptable since nobody currently reviews those diffs by hand, but
  worth noting as a minor loss of transparency.

## Alternatives Considered

**Keep JSON files, add an index file** — avoids migration but does not
solve the repeated-load or query-flexibility problems.

**Raw per-sample storage** (one row per OCR/reaction sample) — the
original draft of this ADR. Enables exact percentile recomputation
across arbitrary session slices, but measured against the existing
321-session `release/` history this runs ~40 MB versus ~1.4 MB for the
aggregates-as-rows design, a ~30x cost for a capability not currently
needed. Also left unresolved how the permanently-committed `release/`
baseline record would be reconciled with a single, growing, gitignored
db file. Rejected in favor of storing the same aggregates the JSON
files already carry.

**DuckDB / pandas** — richer analytics but adds a dependency for a
use-case that standard `sqlite3` covers without installation.

**PostgreSQL / remote store** — overkill for a single-machine tool with
no multi-user requirement.

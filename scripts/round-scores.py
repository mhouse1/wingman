#!/usr/bin/env python3
"""Design 018: load every session's rounds into the score dashboard's Postgres.

    round-scores.py check               exit 0 when the dashboard is switched on
    round-scores.py ingest [--dry-run]  read the stats files, the logs of sessions
                                        that have none, and grafana/changes.yaml,
                                        and load them
    round-scores.py after-session       the ingest `make r` runs once wingman exits
                                        (Phase 2); silent unless it can run

A round is a mission that ended at the end-of-round screen (outcome click_to);
its figures come from the PERFORMANCE panel, empty when it was not read. A
session that ended without a stats file is rebuilt from its log. The ingest
never deletes a round and keeps no state: it reads every file each time, so the
next run makes up for a missed one.

The steps from a file to rows are pure functions (tests/test_round_scores.py).
The load runs psql inside the stack's Postgres container (`docker compose exec
-T`), so the project needs no database driver and Postgres publishes no port.
Standard library and PyYAML only.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import socket
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from wingman.config_local import merge, read_overlay  # noqa: E402
from wingman.config_schema import ConfigError, assert_valid_config, schema_default  # noqa: E402

CONFIG = REPO / "wingman" / "config.yaml"
STATS_DIRS = (REPO / "docs" / "performance" / "current", REPO / "docs" / "performance" / "release")
LOGS_DIR = REPO / "logs"
CHANGES = REPO / "grafana" / "changes.yaml"
SCHEMA = REPO / "grafana" / "schema.sql"
COMPOSE = REPO / "grafana" / "compose.yaml"

SWITCH = "score_dashboard.enabled"
# The host the dashboard runs on (Makefile `require-veda`, ADR 100 D8).
VEDA_HOST = "veda"
# The operator's time zone, which the logs are written in. A log time carries no
# offset; in the hour that repeats when daylight time ends it is read as the
# first of the two (zoneinfo's fold=0).
TZ = ZoneInfo("America/New_York")
# A stats file's run_id is its session's start, within a second of its log's
# first line; a log this close to a known session is that session.
LOG_MATCH_S = 5.0

COLUMNS = ("run_id", "mission_index", "started_at", "ended_at", "score", "kills",
           "deaths", "assists", "wingman_version", "tags", "manual_takeovers")

_ACCOUNT_RE = re.compile(r"_(acct\d+)$")
_STAMP_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_READ_RE = re.compile(r"ROUND PERFORMANCE: score (\d+), K/D/A (\d+)/(\d+)/(\d+)")
_UNREAD = "ROUND PERFORMANCE: not read"
_TO_END_RE = re.compile(r"Game state: \w+ → GAME_END_B")
_FROM_END = "Game state: GAME_END_B →"
_TO_MANUAL_RE = re.compile(r"Game state: \w+ → GAME_BATTLE_MANUAL")
_RUN_ID_RE = re.compile(r"^\d{8}_\d{6}")
# PerformanceTracker's line in cleanup, the one place a log names its run_id. A
# session the shutdown watchdog ends later still has it (2026-10-30 07:25, acct2).
_RUN_FILE = "session data written to"
_RUN_FILE_RE = re.compile(r"session data written to \S*run_(\d{8}_\d{6}\w*)\.json")
_RANKING = "SQUADRON RANKING: rank"
_RANKING_RE = re.compile(r"SQUADRON RANKING: rank (\d+), trophies ([\d,]+)")
RANKING_COLUMNS = ("run_id", "read_at", "rank", "trophies", "tags")


@dataclass(frozen=True)
class Round:
    """One row of the `rounds` table."""

    run_id: str
    mission_index: int
    started_at: "datetime | None"
    ended_at: datetime
    score: "int | None" = None
    kills: "int | None" = None
    deaths: "int | None" = None
    assists: "int | None" = None
    wingman_version: "str | None" = None
    tags: dict = field(default_factory=dict)
    # The round's manual takeovers. Above 0, the operator flew part of it: the
    # dashboard charts it apart from wingman's own rounds (operator, 2026-10-29).
    manual_takeovers: int = 0

    @property
    def key(self) -> "tuple[str, int]":
        return self.run_id, self.mission_index


# -- the switch -------------------------------------------------------------------

def _switch(config_path: Path) -> "tuple[bool | None, str]":
    """(whether the dashboard is switched on, the problem when the config does not
    validate). The shipped config merged with the machine's overlay, validated
    as a live run validates it (CR-018-16); None when it does not validate."""
    with open(config_path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    overlay = read_overlay(config_path)
    merged = merge(cfg, overlay) if overlay else cfg
    try:
        assert_valid_config(merged, source=str(config_path))
    except ConfigError as exc:
        return None, str(exc)
    return bool((merged.get("score_dashboard") or {}).get("enabled", schema_default(SWITCH))), ""


def check(config_path: Path = CONFIG) -> int:
    """0 when the dashboard is switched on; 1 with the key to set when it is off;
    2 when the config does not validate."""
    enabled, problem = _switch(config_path)
    if enabled is None:
        print(problem, file=sys.stderr)
        return 2
    if not enabled:
        print("The score dashboard is switched off (Design 018). To switch it on for this\n"
              "machine, add to wingman/config.local.yaml:\n\n"
              "  score_dashboard:\n    enabled: true\n", file=sys.stderr)
        return 1
    return 0


@dataclass(frozen=True)
class Ranking:
    """One row of the `squadron_rankings` table (ADR 166)."""

    run_id: str
    read_at: datetime
    rank: int
    trophies: int
    tags: dict = field(default_factory=dict)


# -- rows from a stats file -------------------------------------------------------

# The account `make r` flies: the main Wine prefix, whose run_id has no suffix.
MAIN_ACCOUNT = "main"


def account_of(run_id: str) -> str:
    """The account a session flew on: from the `_acctN` suffix `make r1` to `r3`
    give its run_id, else `main`, the prefix `make r` flies (operator, 2026-10-29:
    four accounts, r, r1, r2, r3)."""
    m = _ACCOUNT_RE.search(run_id)
    return m.group(1) if m else MAIN_ACCOUNT


def _string_tags(tags) -> dict:
    return {str(k): str(v) for k, v in tags.items()} if isinstance(tags, dict) else {}


def rows_from_stats(data: dict) -> "list[Round]":
    """One row per click-to-finish mission of a session's stats file, with its
    manual takeovers counted. None for a file from before the reader (no
    `round_performance` block): its rounds were never read, and counting them as
    unread would drag the read rate down."""
    if "round_performance" not in data:
        return []
    run_id = str(data["run_id"])
    base = {"account": account_of(run_id), **_string_tags(data.get("tags"))}
    rows = []
    for mission in data.get("missions") or []:
        if mission.get("outcome") != "click_to":
            continue
        start, duration = mission.get("start_ts"), mission.get("duration_s")
        if start is None or duration is None:
            continue
        perf = mission.get("performance") or {}
        rows.append(Round(
            run_id, int(mission["index"]),
            datetime.fromtimestamp(start, timezone.utc),
            datetime.fromtimestamp(start + duration, timezone.utc),
            perf.get("score"), perf.get("kills"), perf.get("deaths"), perf.get("assists"),
            data.get("wingman_version"),
            {**base, **_string_tags(mission.get("tags"))},     # a mission's own tags win
            int(mission.get("manual_takeover_count") or 0)))
    return rows


def rankings_from_stats(data: dict) -> "list[Ranking]":
    """One row per reading of the squadron leaderboard in a stats file (ADR 166)."""
    run_id = str(data.get("run_id", ""))
    tags = {"account": account_of(run_id), **_string_tags(data.get("tags"))}
    return [Ranking(run_id, datetime.fromtimestamp(r["ts"], timezone.utc), int(r["rank"]),
                    int(r["trophies"]), tags)
            for r in data.get("squadron_rankings") or []
            if r.get("ts") is not None and r.get("rank") is not None]


# -- rows from a log --------------------------------------------------------------

def line_time(line: str) -> "datetime | None":
    """A log line's time, read in the operator's time zone."""
    m = _STAMP_RE.match(line)
    if m is None:
        return None
    return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(
        microsecond=int(m.group(2)) * 1000, tzinfo=TZ)


def session_start(run_id: str) -> "datetime | None":
    """When a session started, from its run_id (`YYYYmmdd_HHMMSS…`)."""
    if not _RUN_ID_RE.match(run_id):
        return None
    return datetime.strptime(run_id[:15], "%Y%m%d_%H%M%S").replace(tzinfo=TZ)


def log_run_id(start: datetime) -> str:
    return start.strftime("%Y%m%d_%H%M%S") + "_log"


def rows_from_log(lines, run_id: str) -> "list[Round]":
    """One row per `ROUND PERFORMANCE` line of a log, read or not, at the line's
    time, tagged `source: log`, and with the `account` of the run_id that
    PerformanceTracker's line names; a log cut off before that line names no
    account (a run_id without a suffix is `main`). A line with no
    `Game state: … → GAME_END_B` line since the last GAME_END_B exit, or since the
    last line counted, is not a round: a second click-through of the same end
    screens logged it (Design 018, open question 4). Each `→ GAME_BATTLE_MANUAL`
    line in a round is one of its manual takeovers (operator, 2026-10-29)."""
    rows = []
    at_end = False
    takeovers = 0
    account = None
    for raw in lines:
        if ("GAME_END_B" not in raw and "ROUND PERFORMANCE" not in raw
                and "GAME_BATTLE_MANUAL" not in raw and _RUN_FILE not in raw):
            continue
        line = _ANSI_RE.sub("", raw)
        named = _RUN_FILE_RE.search(line)
        if named:
            account = account_of(named.group(1))
            continue
        if _TO_MANUAL_RE.search(line):
            takeovers += 1
            continue
        if _FROM_END in line:
            at_end = False
            takeovers = 0
            continue
        if _TO_END_RE.search(line):
            at_end = True
            continue
        read = _READ_RE.search(line)
        if read is None and _UNREAD not in line:
            continue                    # the shadow page checks, a failed read's traceback
        when = line_time(line)
        if not at_end or when is None:
            continue
        at_end = False
        figures = tuple(int(g) for g in read.groups()) if read else (None, None, None, None)
        rows.append(Round(run_id, len(rows), None, when, *figures, None, {"source": "log"},
                          takeovers))
        takeovers = 0
    if account is None:
        return rows
    return [replace(r, tags={**r.tags, "account": account}) for r in rows]


def rankings_from_log(lines, run_id: str) -> "list[Ranking]":
    """One row per `SQUADRON RANKING: rank N, trophies T` line of a log, tagged
    `source: log`, with the account its run-file line names, as for its rounds."""
    readings = []
    account = None
    for raw in lines:
        if _RANKING not in raw and _RUN_FILE not in raw:
            continue
        line = _ANSI_RE.sub("", raw)
        named = _RUN_FILE_RE.search(line)
        if named:
            account = account_of(named.group(1))
            continue
        m = _RANKING_RE.search(line)
        when = line_time(line)
        if m and when is not None:
            readings.append((when, int(m.group(1)), int(m.group(2).replace(",", ""))))
    tags = {"source": "log"} | ({"account": account} if account else {})
    return [Ranking(run_id, when, rank, trophies, tags) for when, rank, trophies in readings]


def log_start(path: Path) -> "datetime | None":
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return line_time(fh.readline())
    except OSError:
        return None


def loaded_starts(run_ids) -> "list[datetime]":
    """The starts of the table's sessions that came from a stats file. One
    rebuilt from its log is left out, so its log is read again and the upsert
    gives its rows what a later reader finds there."""
    return [s for s in (session_start(r) for r in run_ids if not r.endswith("_log")) if s]


def logs_to_rebuild(paths, known_starts) -> "list[tuple[Path, datetime]]":
    """The logs of sessions with no stats file and not in the table yet: each log
    whose first line is not within LOG_MATCH_S of a known session's start."""
    found = []
    for path in paths:
        start = log_start(path)
        if start is None:
            continue
        if any(abs((start - known).total_seconds()) <= LOG_MATCH_S for known in known_starts):
            continue
        found.append((path, start))
    return found


# -- the change log ---------------------------------------------------------------

def load_changes(text: str) -> "list[tuple[datetime, str, str | None]]":
    """grafana/changes.yaml's entries as (at, label, ref). Each needs a label and a
    time with its UTC offset."""
    entries = yaml.safe_load(text) or []
    changes = []
    for n, entry in enumerate(entries, 1):
        at, label = entry.get("at"), entry.get("label")
        if isinstance(at, str):
            at = datetime.fromisoformat(at)
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError(f"changes.yaml entry {n}: `at` needs a time with its UTC offset")
        if not label:
            raise ValueError(f"changes.yaml entry {n}: `label` is missing")
        changes.append((at, str(label), str(entry["ref"]) if entry.get("ref") else None))
    return changes


# -- the load ---------------------------------------------------------------------

def _csv_value(value) -> str:
    if value is None:
        return ""                       # an unquoted empty field is NULL to COPY … csv
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _csv(records) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    for record in records:
        writer.writerow([_csv_value(v) for v in record])
    return buf.getvalue()


def rounds_csv(rows) -> str:
    """The rows as COPY … (FORMAT csv) input, in COLUMNS order."""
    return _csv((r.run_id, r.mission_index, r.started_at, r.ended_at, r.score, r.kills,
                 r.deaths, r.assists, r.wingman_version, r.tags, r.manual_takeovers)
                for r in rows)


def changes_csv(changes) -> str:
    return _csv(changes)


def rankings_csv(rankings) -> str:
    """The squadron readings as COPY … (FORMAT csv) input, in RANKING_COLUMNS order."""
    return _csv((r.run_id, r.read_at, r.rank, r.trophies, r.tags) for r in rankings)


def load_script(rows_text: str, changes_text: str, rankings_text: str = "") -> str:
    """One transaction: the rows into a temporary table, then upserted by (run_id,
    mission_index), which prints how many were new; the squadron readings the
    same way by (run_id, read_at); the `changes` table replaced by the change
    log. A round or a reading is never deleted."""
    cols = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in COLUMNS[2:])
    rcols = ", ".join(RANKING_COLUMNS)
    rupdates = ", ".join(f"{c} = EXCLUDED.{c}" for c in RANKING_COLUMNS[2:])
    return (
        "BEGIN;\n"
        "CREATE TEMP TABLE incoming (LIKE rounds INCLUDING DEFAULTS) ON COMMIT DROP;\n"
        f"COPY incoming ({cols}) FROM STDIN WITH (FORMAT csv);\n"
        f"{rows_text}\\.\n"
        "WITH loaded AS (\n"
        f"    INSERT INTO rounds ({cols}) SELECT {cols} FROM incoming\n"
        f"    ON CONFLICT (run_id, mission_index) DO UPDATE SET {updates}\n"
        "    RETURNING (xmax = 0) AS inserted)\n"
        "SELECT count(*) FILTER (WHERE inserted) FROM loaded;\n"
        "CREATE TEMP TABLE incoming_rankings (LIKE squadron_rankings INCLUDING DEFAULTS)"
        " ON COMMIT DROP;\n"
        f"COPY incoming_rankings ({rcols}) FROM STDIN WITH (FORMAT csv);\n"
        f"{rankings_text}\\.\n"
        "WITH loaded AS (\n"
        f"    INSERT INTO squadron_rankings ({rcols}) SELECT {rcols} FROM incoming_rankings\n"
        f"    ON CONFLICT (run_id, read_at) DO UPDATE SET {rupdates}\n"
        "    RETURNING (xmax = 0) AS inserted)\n"
        "SELECT count(*) FILTER (WHERE inserted) FROM loaded;\n"
        "DELETE FROM changes;\n"
        "COPY changes (at, label, ref) FROM STDIN WITH (FORMAT csv);\n"
        f"{changes_text}\\.\n"
        "COMMIT;\n")


def _compose(project: "str | None", env_file: "str | None") -> "list[str]":
    args = ["docker", "compose", "-f", str(COMPOSE)]
    if project:
        args += ["-p", project]
    if env_file:
        args += ["--env-file", env_file]
    return args


def psql(sql: str, compose: "list[str]") -> str:
    """Run `sql` through psql in the stack's Postgres container, as the superuser
    that owns the tables; its unaligned, tuples-only output."""
    result = subprocess.run(
        compose + ["exec", "-T", "postgres", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-t", "-A",
                   "-U", "postgres", "-d", "wingman"],
        input=sql, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"psql failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout


def read_stats(dirs) -> "list[dict]":
    sessions = []
    for directory in dirs:
        for path in sorted(Path(directory).glob("run_*_stats.json")):
            try:
                sessions.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError) as exc:
                print(f"round-scores: skipping {path}: {exc}", file=sys.stderr)
    return sessions


def ingest(dry_run: bool = False, project: "str | None" = None,
           env_file: "str | None" = None) -> int:
    sessions = read_stats(STATS_DIRS)
    rows: "dict[tuple[str, int], Round]" = {}
    rankings: "dict[tuple[str, datetime], Ranking]" = {}
    from_stats = 0
    for data in sessions:
        session_rows = rows_from_stats(data)
        from_stats += bool(session_rows)
        rows.update((r.key, r) for r in session_rows)
        rankings.update(((r.run_id, r.read_at), r) for r in rankings_from_stats(data))

    known = [s for s in (session_start(str(d.get("run_id", ""))) for d in sessions) if s]
    compose = _compose(project, env_file)
    if not dry_run:
        out = psql(SCHEMA.read_text(encoding="utf-8")
                   + "\nSELECT DISTINCT run_id FROM rounds;\n", compose)
        known += loaded_starts(out.split())

    from_logs = 0
    for path, start in logs_to_rebuild(sorted(LOGS_DIR.glob("wingman_*.log")), known):
        with open(path, encoding="utf-8", errors="replace") as fh:
            log_rows = rows_from_log(fh, log_run_id(start))
        with open(path, encoding="utf-8", errors="replace") as fh:
            rankings.update(((r.run_id, r.read_at), r)
                            for r in rankings_from_log(fh, log_run_id(start)))
        from_logs += bool(log_rows)
        rows.update((r.key, r) for r in log_rows)

    changes = load_changes(CHANGES.read_text(encoding="utf-8"))
    ordered = sorted(rows.values(), key=lambda r: (r.ended_at, r.run_id, r.mission_index))
    read = [r for r in ordered if r.score is not None]
    new = "none loaded (dry run)"
    if not dry_run:
        out = psql(load_script(rounds_csv(ordered), changes_csv(changes),
                               rankings_csv(sorted(rankings.values(),
                                                   key=lambda r: (r.read_at, r.run_id)))),
                   compose)
        counts = [line for line in out.split() if line.isdigit()] + ["?", "?"]
        new = f"{counts[0]} new rows, {counts[1]} new squadron readings"
    print(f"round-scores: {from_stats} sessions from stats files, {from_logs} from logs; "
          f"{len(ordered)} rounds, {len(read)} read, score {sum(r.score for r in read):,}; "
          f"{len(rankings)} squadron readings; {len(changes)} changes; {new}")
    return 0


def stack_up() -> bool:
    """True when the stack's Postgres container is running. False when it is not,
    or Docker cannot say."""
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(COMPOSE), "ps", "--status", "running", "--services"],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and "postgres" in result.stdout.split()


def after_session(config_path: Path = CONFIG) -> int:
    """Phase 2: the ingest `make r` (and so `r1` to `r3`) runs once wingman exits,
    so a session's rounds reach the dashboard without a manual ingest. It does
    nothing, silently, unless the host is VEDA, the dashboard is switched on and
    the stack is up, so `make r` on a clone behaves as before. A failed ingest is
    printed and ignored. Always 0: the recipe keeps wingman's exit status."""
    if socket.gethostname() != VEDA_HOST:
        return 0
    enabled, _problem = _switch(config_path)
    if not enabled or not stack_up():
        return 0
    try:
        ingest()
    except Exception as exc:                     # never fail the session's recipe
        print(f"round-scores: the after-session ingest failed (ignored): {exc}",
              file=sys.stderr)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="exit 0 when score_dashboard.enabled is on")
    ing = sub.add_parser("ingest", help="load the rounds and the change log")
    ing.add_argument("--dry-run", action="store_true",
                     help="read and count, load nothing (needs no Docker)")
    ing.add_argument("--project", help="Compose project, for a throwaway stack")
    ing.add_argument("--env-file", help="Compose env file, for a throwaway stack")
    sub.add_parser("after-session", help="the ingest `make r` runs once wingman exits")
    args = parser.parse_args(argv)
    if args.command == "check":
        return check()
    if args.command == "after-session":
        return after_session()
    try:
        return ingest(args.dry_run, args.project, args.env_file)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"round-scores: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    os.chdir(REPO)
    sys.exit(main())

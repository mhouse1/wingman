"""Design 018 V1: the score dashboard's ingest, change log and dashboard, without
Docker or the stack (scripts/round-scores.py)."""

import csv
import importlib.util
import io
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from constants import CONFIG_PATH

_SPEC = importlib.util.spec_from_file_location("round_scores", Path("scripts/round-scores.py"))
rs = importlib.util.module_from_spec(_SPEC)
sys.modules["round_scores"] = rs        # dataclasses look their module up here
_SPEC.loader.exec_module(rs)

_START = 1_761_560_000.0                # 2026-10-27, epoch seconds


def _mission(index, outcome="click_to", performance=None, start=_START, duration=300.0, **extra):
    return {"index": index, "start_ts": start + index * 400, "duration_s": duration,
            "outcome": outcome, "performance": performance, **extra}


def _stats(missions, run_id="20261028_004906_acct1", **extra):
    return {"run_id": run_id, "wingman_version": "1.9.3", "missions": missions,
            "round_performance": {"rounds_read": 0, "rounds_unread": 0}, **extra}


_READ = {"score": 163, "kills": 1, "deaths": 2, "assists": 0}


# -- rows from a stats file -------------------------------------------------------

def test_only_click_to_finish_missions_become_rows():
    rows = rs.rows_from_stats(_stats([_mission(0, performance=_READ),
                                      _mission(1, outcome="lobby_exit"),
                                      _mission(2, outcome="unknown")]))
    assert [r.mission_index for r in rows] == [0]


def test_figures_are_empty_when_the_panel_was_not_read():
    (read, unread) = rs.rows_from_stats(_stats([_mission(0, performance=_READ), _mission(1)]))
    assert (read.score, read.kills, read.deaths, read.assists) == (163, 1, 2, 0)
    assert (unread.score, unread.kills, unread.deaths, unread.assists) == (None, None, None, None)


def test_ended_at_is_the_start_plus_the_duration():
    (row,) = rs.rows_from_stats(_stats([_mission(0, duration=287.5)]))
    assert row.started_at == datetime.fromtimestamp(_START, timezone.utc)
    assert row.ended_at - row.started_at == timedelta(seconds=287.5)
    assert row.wingman_version == "1.9.3"


def test_a_file_from_before_the_reader_gives_no_rows():
    data = _stats([_mission(0)])
    del data["round_performance"]
    assert rs.rows_from_stats(data) == []


# -- tags -------------------------------------------------------------------------

def test_the_account_comes_from_the_run_id_suffix():
    (row,) = rs.rows_from_stats(_stats([_mission(0)], run_id="20261029_071302_acct3"))
    assert row.tags == {"account": "mobius3"}


@pytest.mark.parametrize("run_id, pilot", [
    ("20261031_023505_acct1", "mobius0"),     # its end screen reads MOBIUS0
    ("20261030_162007", "mobius1"),           # its end screen reads MOBIUS1
    ("20261031_043634_acct2", "mobius2"),
    ("20261030_223038_acct3", "mobius3"),
    ("20261031_043634_acct4", "acct4")])      # not named yet: it keeps its tag
def test_each_prefix_is_its_pilot_and_the_numbers_do_not_line_up(run_id, pilot):
    """Operator, 2026-10-31. The tag belongs to the prefix, so sessions from before
    the Makefile swapped r and r1 keep their pilot."""
    assert rs.account_of(run_id) == pilot


def test_a_session_without_the_suffix_flew_the_main_account():
    """`make r` flies the main prefix, and its run_id has no suffix."""
    (row,) = rs.rows_from_stats(_stats([_mission(0)], run_id="20261028_052534"))
    assert row.tags == {"account": "mobius1"}


def test_session_and_mission_tags_are_copied_and_the_missions_win():
    data = _stats([_mission(0, tags={"jet": "j20"}), _mission(1)],
                  tags={"jet": "acs", "map": "countdown"})
    first, second = rs.rows_from_stats(data)
    assert first.tags == {"account": "mobius0", "jet": "j20", "map": "countdown"}
    assert second.tags == {"account": "mobius0", "jet": "acs", "map": "countdown"}


def test_the_same_file_read_twice_gives_the_same_keys():
    data = _stats([_mission(0, performance=_READ), _mission(1), _mission(2)])
    assert [r.key for r in rs.rows_from_stats(data)] == [r.key for r in rs.rows_from_stats(data)]


# -- rows from a log --------------------------------------------------------------

def _line(t, text):
    return f"2026-10-29 {t},123 [INFO] {text}\n"


_LOG = [
    _line("02:50:29", "Configuration loaded from wingman/config.yaml"),
    _line("02:56:49", "\x1b[96m🎮 Game state: GAME_BATTLE_EJECT → GAME_END_B\x1b[0m"),
    _line("02:56:51", "\x1b[93m📋 ROUND PERFORMANCE: score 230, K/D/A 2/5/0\x1b[0m"),
    _line("02:56:54", "🎮 Game state: GAME_END_B → GAME_LOBBY"),
    _line("03:09:14", "🎮 Game state: GAME_BATTLE → GAME_END_B"),
    _line("03:09:16", "ROUND PERFORMANCE: not read — no KDA on any of 4 frames: kda ''"),
    _line("03:09:17", "ROUND PERFORMANCE: shadow — page 2: kda ''"),
    _line("03:09:20", "🎮 Game state: GAME_END_B → GAME_LOBBY"),
    # 2026-10-28: a second click-through of the same end screens.
    _line("03:09:29", "ROUND PERFORMANCE: not read (4 frame(s), no two read alike)"),
]


def test_a_log_gives_one_row_per_round_read_or_not():
    rows = rs.rows_from_log(_LOG, "20261029_025029_log")
    assert [(r.mission_index, r.score, r.kills, r.deaths, r.assists) for r in rows] == [
        (0, 230, 2, 5, 0), (1, None, None, None, None)]
    assert all(r.tags == {"source": "log"} and r.started_at is None for r in rows)
    assert rows[0].ended_at == datetime(2026, 10, 29, 2, 56, 51, 123000, tzinfo=rs.TZ)


def test_a_reading_with_no_end_screen_before_it_is_not_a_round():
    assert len(rs.rows_from_log(_LOG, "x")) == 2, "the second click-through"


def test_a_log_time_in_the_repeated_hour_is_read_as_daylight_time():
    when = rs.line_time("2026-11-01 01:30:00,000 [INFO] x")
    assert when.utcoffset() == timedelta(hours=-4)


def test_a_log_session_is_named_for_its_first_line():
    assert rs.log_run_id(rs.line_time(_LOG[0])) == "20261029_025029_log"


def _write_log(directory, name, first_time):
    path = directory / name
    path.write_text(_line(first_time, "Configuration loaded"), encoding="utf-8")
    return path


def test_a_stats_files_jet_reaches_its_round():
    (row,) = rs.rows_from_stats(_stats([_mission(0, tags={"jet": "J-10 FIREBIRD"})]))
    assert row.tags["jet"] == "J-10 FIREBIRD"


def test_a_log_keeps_a_jet_until_the_next_read():
    log = [_line("02:50:00", "\x1b[93m✈️ JET NAME: SU-35 SUPER FLANKER\x1b[0m")] + _LOG
    assert [r.tags.get("jet") for r in rs.rows_from_log(log, "x")] == ["SU-35 SUPER FLANKER"] * 2
    log = (log[:5] + [_line("02:58:00", "JET NAME: not read — no trophy icon on the label")]
           + log[5:])
    assert [r.tags.get("jet") for r in rs.rows_from_log(log, "x")] == ["SU-35 SUPER FLANKER", None]


def test_a_jet_changed_in_the_wait_reaches_a_log_round():
    """ADR 168: both lines are wingman's, the click's then the recheck's. The recheck's
    own note must not read as a failed reading."""
    log = [_line("02:49:00", "✈️ JET NAME: F-14 TOMCAT"),
           _line("02:49:30", "Jet recheck: the wait's label changed, was F-14 TOMCAT"),
           _line("02:49:30", "✈️ JET NAME: SU-27 FLANKER")] + _LOG
    assert rs.rows_from_log(log, "x")[0].tags["jet"] == "SU-27 FLANKER"


def test_a_log_keeps_a_jet_with_a_slash_or_a_dot():
    for jet in ("F/A-18F SUPER HORNET", "M.III"):
        log = [_line("02:50:00", f"✈️ JET NAME: {jet}")] + _LOG
        assert rs.rows_from_log(log, "x")[0].tags["jet"] == jet


def test_a_lobby_that_failed_to_read_gives_a_log_round_no_jet():
    log = [_line("02:40:00", "✈️ JET NAME: SU-35 SUPER FLANKER"),
           _line("02:50:00", "JET NAME: not read — no trophy icon on the label")] + _LOG
    assert "jet" not in rs.rows_from_log(log, "x")[0].tags


def test_a_log_takes_its_account_from_the_run_file_line():
    """2026-10-30: a `make r2` session the shutdown watchdog ended wrote no
    stats file, and its 12 rounds showed as "no account"."""
    named = _LOG + [_line("03:10:00", "PerformanceTracker: session data written to "
                                      "docs/performance/current/run_20261029_025030_acct2.json")]
    assert {r.tags["account"] for r in rs.rows_from_log(named, "x")} == {"mobius2"}
    main = [_line("03:10:00", "PerformanceTracker: session data written to "
                              "docs/performance/current/run_20261029_025030.json")]
    assert {r.tags["account"] for r in rs.rows_from_log(_LOG + main, "x")} == {"mobius1"}
    assert all("account" not in r.tags for r in rs.rows_from_log(_LOG, "x")), \
        "a log cut off before the line names no account"


def test_a_session_rebuilt_from_its_log_is_read_again():
    starts = rs.loaded_starts(["20261029_071302_acct3", "20261030_072533_log", "20261029_185939"])
    assert starts == [rs.session_start("20261029_071302_acct3"),
                      rs.session_start("20261029_185939")]


def test_a_log_near_a_stats_file_or_a_loaded_session_is_not_rebuilt(tmp_path):
    with_stats = _write_log(tmp_path, "a.log", "07:13:02")
    loaded = _write_log(tmp_path, "b.log", "08:00:00")
    orphan = _write_log(tmp_path, "c.log", "09:00:00")
    known = [rs.session_start("20261029_071302_acct3"), rs.session_start("20261029_075957_log")]
    assert [p for p, _ in rs.logs_to_rebuild([with_stats, loaded, orphan], known)] == [orphan]


# -- the change log ---------------------------------------------------------------

def test_the_change_log_parses_and_every_entry_has_a_label_and_an_offset():
    changes = rs.load_changes(Path("grafana/changes.yaml").read_text(encoding="utf-8"))
    assert changes
    assert all(label and at.tzinfo is not None for at, label, _ref in changes)
    assert all(ref is None or Path(ref).exists() for _at, _label, ref in changes)


def test_a_change_without_an_offset_is_refused():
    with pytest.raises(ValueError, match="offset"):
        rs.load_changes("- at: 2026-10-27T05:45:00\n  label: x\n")
    with pytest.raises(ValueError, match="label"):
        rs.load_changes("- at: 2026-10-27T05:45:00-04:00\n")


# -- the load ---------------------------------------------------------------------

def test_the_copy_csv_has_nulls_json_tags_and_offsets():
    rows = rs.rows_from_stats(_stats([_mission(0, performance=_READ, tags={"note": 'a "b", c'}),
                                      _mission(1)]))
    text = rs.rounds_csv(rows)
    first, second = list(csv.reader(io.StringIO(text)))
    assert first[:2] == ["20261028_004906_acct1", "0"]
    assert datetime.fromisoformat(first[3]).utcoffset() == timedelta(0)
    assert json.loads(first[9]) == {"account": "mobius0", "note": 'a "b", c'}
    assert second[4:8] == ["", "", "", ""]
    assert ",,,," in text.splitlines()[1], "unquoted empty fields, which COPY reads as NULL"


def test_the_load_is_one_transaction_that_upserts_and_replaces_the_changes():
    script = rs.load_script("r\n", "c\n")
    assert script.startswith("BEGIN;") and script.rstrip().endswith("COMMIT;")
    assert "FROM STDIN WITH (FORMAT csv);\nr\n\\.\n" in script
    assert "ON CONFLICT (run_id, mission_index) DO UPDATE" in script
    assert "DELETE FROM changes;" in script
    assert "DELETE FROM rounds" not in script, "a round is never deleted"


# -- the squadron readings (ADR 166) -------------------------------------------------

def test_a_stats_files_squadron_readings_become_rows_with_its_account():
    data = _stats([], squadron_rankings=[{"ts": 1793430302.0, "rank": 659, "trophies": 358032}])
    (row,) = rs.rankings_from_stats(data)
    assert (row.run_id, row.rank, row.trophies, row.tags) == (
        "20261028_004906_acct1", 659, 358032, {"account": "mobius0"})
    assert row.read_at == datetime.fromtimestamp(1793430302.0, timezone.utc)
    assert rs.rankings_from_stats(_stats([])) == []


def test_a_logs_squadron_lines_become_rows_with_the_account_it_names():
    log = [_line("02:10:00", "\x1b[93m🏆 SQUADRON RANKING: rank 659, trophies 358,032\x1b[0m"),
           _line("02:20:00", "SQUADRON RANKING: not read — the menu did not open"),
           _line("03:10:00", "PerformanceTracker: session data written to "
                             "docs/performance/current/run_20261029_025030_acct2.json")]
    (row,) = rs.rankings_from_log(log, "x")
    assert (row.rank, row.trophies, row.tags) == (659, 358032,
                                                  {"source": "log", "account": "mobius2"})
    assert row.read_at == datetime(2026, 10, 29, 2, 10, 0, 123000, tzinfo=rs.TZ)


def test_the_load_upserts_the_squadron_readings_in_the_same_transaction():
    script = rs.load_script("r\n", "c\n", "q\n")
    body = script[:script.index("COMMIT;")]
    assert "COPY incoming_rankings (run_id, read_at, rank, trophies, tags)" in body
    assert "q\n\\.\n" in body
    assert "ON CONFLICT (run_id, read_at) DO UPDATE" in body
    assert "DELETE FROM squadron_rankings" not in script


def test_the_dashboard_shows_the_latest_rank_and_trophies():
    """Operator, 2026-10-31: both the rank and the trophy count on the dashboard."""
    now = dict(_dashboard_sql())["Squadron now"]
    assert 'rank AS "rank"' in now and 'trophies AS "trophies"' in now
    assert "ORDER BY read_at DESC" in now and "LIMIT 1" in now


@pytest.mark.parametrize("title, column", [("Squadron rank per reading", "rank"),
                                           ("Squadron trophies per reading", "trophies")])
def test_rank_and_trophies_are_charted_over_time_as_one_series(title, column):
    """Operator, 2026-10-31: "track squad ranking and trophies over time like the
    score per round", and "the rank and trophies are account independent"."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    (panel,) = [p for p in dashboard["panels"] if p["title"].startswith(title)]
    (target,) = panel["targets"]
    assert f'{column} AS "{column}"' in target["rawSql"]
    assert "FROM squadron_rankings" in target["rawSql"]


def test_the_rank_axis_runs_from_high_to_low_to_line_up_with_the_trophies():
    """Operator, 2026-11-17: a better place is a smaller rank, so the rank chart is flipped
    to rise with the trophies chart beside it. Grafana's time series has no reversed axis;
    negative-Y plots the values negated and keeps the real ones in the tooltip."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    rank, trophies = (next(p for p in dashboard["panels"] if p["title"].startswith(t))
                      for t in ("Squadron rank per reading", "Squadron trophies per reading"))
    assert rank["fieldConfig"]["defaults"]["custom"].get("transform") == "negative-Y"
    assert "transform" not in trophies["fieldConfig"]["defaults"].get("custom", {})


def test_no_squadron_panel_depends_on_the_account():
    """A filter by account could only hide a reading of the same figure."""
    sqls = [sql for _title, sql in _dashboard_sql() if "squadron_rankings" in sql]
    assert sqls and not any("tags" in sql or "account" in sql for sql in sqls)


def test_memory_then_the_jet_ranges_then_the_squadron_charts_sit_under_score_per_round():
    """Operator, 2026-11-03: the jet ranges go under Score per round. Operator,
    2026-11-25: Memory per sample goes between them, at the same width, so memory
    growth and score line up on one time axis."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    place = {p["title"].split(" (")[0]: p["gridPos"] for p in dashboard["panels"]}
    score = place["Score per round: wingman and manual takeover"]
    memory = place["Memory per sample"]
    assert memory["y"] == score["y"] + score["h"]
    assert (memory["x"], memory["w"]) == (score["x"], score["w"])
    jets = place["Score range by jet"]
    assert jets["y"] == memory["y"] + memory["h"]
    below = jets["y"] + jets["h"]
    assert place["Squadron rank per reading"]["y"] == below
    assert place["Squadron trophies per reading"]["y"] == below


def test_the_schema_keeps_the_trophies():
    schema = Path("grafana/schema.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE squadron_rankings ADD COLUMN IF NOT EXISTS trophies bigint;" in schema


def test_the_schema_names_each_jet_once_by_its_letters_and_digits():
    """Operator, 2026-11-08: F-106 DELTA DART and F106 DELTA DART were two jets on
    the dashboard. The view keys a spelling the way `jet_name.same_jet` does, and is
    replaced on every load, as the schema is applied before each one."""
    schema = Path("grafana/schema.sql").read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW jet_names AS" in schema
    assert "regexp_replace(upper(tags->>'jet'), '[^A-Z0-9]', '', 'g')" in schema


def _account_and_jet():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    panel = next(p for p in dashboard["panels"] if p["title"] == "Account and jet score per round")
    return dashboard, panel


def test_the_account_and_jet_chart_follows_the_two_boxes():
    """Operator, 2026-11-17: isolate one jet on one account and see whether its score per
    round improves over time."""
    _dashboard, panel = _account_and_jet()
    for target in panel["targets"]:
        sql = target["rawSql"]
        assert "coalesce(tags->>'account', 'no account') IN (${account:sqlstring})" in sql
        assert "coalesce(jet, 'not recorded') IN (${jet:sqlstring})" in sql
        assert "manual_takeovers = 0" in sql
    (avg,) = [t["rawSql"] for t in panel["targets"] if "moving average" in t["rawSql"]]
    assert "${window:raw} - 1 PRECEDING" in avg


def test_the_account_and_jet_boxes_pick_several_or_all_and_the_jets_follow_the_accounts():
    dashboard, _panel = _account_and_jet()
    boxes = {v["name"]: v for v in dashboard["templating"]["list"]}
    for name in ("account", "jet"):
        assert boxes[name]["multi"] and boxes[name]["includeAll"], name
    assert "IN (${account:sqlstring})" in boxes["jet"]["query"]
    assert "JOIN jet_names ON spelling = tags->>'jet'" in boxes["jet"]["query"]


def test_the_account_and_jet_chart_keeps_the_account_colours():
    _dashboard, panel = _account_and_jet()
    colours = {o["matcher"]["options"]: o["properties"][0]["value"]["fixedColor"]
               for o in panel["fieldConfig"]["overrides"] if o["matcher"]["id"] == "byRegexp"}
    score = {o["matcher"]["options"]: o["properties"][0]["value"]["fixedColor"]
             for o in _score_panel()["fieldConfig"]["overrides"]
             if o["matcher"]["id"] == "byRegexp" and "manual" not in o["matcher"]["options"]}
    assert colours == score


def test_every_panel_that_names_a_jet_names_it_through_the_view():
    """A panel that groups by the spelling read splits one jet in two again."""
    by_jet = [(title, sql) for title, sql in _dashboard_sql() if "tags->>'jet'" in sql]
    assert len(by_jet) == 5
    for title, sql in by_jet:
        assert "JOIN jet_names ON spelling = tags->>'jet'" in sql, title
        assert "tags->>'jet' AS" not in sql and "coalesce(tags->>'jet'" not in sql, title
        assert "GROUP BY tags->>'jet'" not in sql, title


# -- the dashboard ----------------------------------------------------------------

_VAR_RE = re.compile(r"\$\{(\w+)(?::\w+)?\}|\$(\w+)")


def _texts(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("rawSql", "title", "description") and isinstance(value, str):
                yield value
            else:
                yield from _texts(value)
    elif isinstance(node, list):
        for item in node:
            yield from _texts(item)


def test_the_dashboard_parses_and_defines_every_variable_it_uses():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    defined = {v["name"] for v in dashboard["templating"]["list"]}
    used = {a or b for text in _texts(dashboard) for a, b in _VAR_RE.findall(text)}
    assert used - {"__timeFilter"} <= defined, used - defined
    assert {"granularity", "aggregate", "metric", "window", "tags", "tz"} <= used


def test_every_braced_reference_names_a_defined_variable():
    """2026-10-29: `${{window:raw}}`, an f-string escape left doubled, reached the
    dashboard; Grafana left it alone and Postgres refused both moving averages, so
    Score per round lost its lines. The pattern above does not see it."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    defined = {v["name"] for v in dashboard["templating"]["list"]}
    for text in _texts(dashboard):
        assert "{{" not in text and "}}" not in text, text
        for inner in re.findall(r"\$\{([^}]*)\}", text):
            assert inner.split(":")[0] in defined, (inner, text)


def test_every_round_panel_filters_by_the_tags_box_quoted():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    sqls = [t["rawSql"] for p in dashboard["panels"] for t in p["targets"]]
    rounds = [sql for sql in sqls if "squadron_rankings" not in sql and "resource_" not in sql
              and "run_timings" not in sql]
    assert rounds and all("tags @> ${tags:sqlstring}::jsonb" in sql for sql in rounds)
    assert not any("tags:raw" in sql for sql in sqls)


# -- the resource trail (Performance 008, operator 2026-11-15) ------------------------

_SAMPLE_0 = ("RESOURCE elapsed=0s rss_mb=678 d_rss=+0 anon_mb=365 d_anon=+0 mi_use_mb=140 "
             "d_mi_use=+0 mi_free_mb=0 d_mi_free=+0 shmem_mb=n/a d_shmem=n/a peak_rss_mb=688 "
             "swap_mb=0 threads=9 d_threads=+0 fds=14 d_fds=+0 gc=(637,8,3) ocr_med=n/a "
             "ocr_p95=n/a n_ocr=0 pool_depth=n/a game_rss_mb=503 d_game_rss=+0 game_swap_mb=0 "
             "sys_swap_mb=5040 load=0.7/0.37/0.27")
_SAMPLE_1 = ("RESOURCE elapsed=900s rss_mb=2542 d_rss=+1864 anon_mb=2199 d_anon=+1834 "
             "mi_use_mb=1543 d_mi_use=+1403 mi_free_mb=272 d_mi_free=+272 shmem_mb=n/a "
             "d_shmem=n/a peak_rss_mb=3032 swap_mb=0 threads=31 d_threads=+22 fds=17 d_fds=+3 "
             "gc=(134,2,8) ocr_med=0.34 ocr_p95=0.50 n_ocr=894 pool_depth=0 game_rss_mb=1404 "
             "d_game_rss=+901 game_swap_mb=0 sys_swap_mb=5039 load=18.19/11.16/4.89")
_SESSION_START = datetime(2026, 10, 29, 4, 0, 0, tzinfo=rs.TZ)
# The session-end block as the logger writes it: one stamped line, then the rest
# of the message on lines of its own.
_RESOURCE_LOG = [
    _line("04:00:00", _SAMPLE_0),
    _line("04:15:00", "\x1b[92m" + _SAMPLE_1 + "\x1b[0m"),
    _line("10:00:00", "RESOURCE SUMMARY elapsed=6.0h samples=72 measured=5.8h "
                      "(rates exclude 10min warm-up)"),
    "  wingman rss 678->3410MB (+2732)  rate=+148 MB/h  peak=3737MB\n",
    "  VERDICT: BOTH processes growing above 50 MB/h (game faster) — see Performance 008\n",
    _line("10:00:01", "STANDBY: malloc_trim released 1716 MB (rss 3335 -> 1619 MB)"),
    _line("10:05:00", "PerformanceTracker: session data written to "
                      "docs/performance/current/run_20261029_040000_acct2.json"),
]


def test_each_resource_line_is_a_sample_at_its_time_with_its_figures():
    samples, _ = rs.resources_from_log(_RESOURCE_LOG, _SESSION_START)
    assert [(s.elapsed_s, s.at) for s in samples] == [
        (0, datetime(2026, 10, 29, 4, 0, 0, 123000, tzinfo=rs.TZ)),
        (900, datetime(2026, 10, 29, 4, 15, 0, 123000, tzinfo=rs.TZ))]
    first, second = (s.figures for s in samples)
    assert (first["rss_mb"], first["game_rss_mb"], first["threads"], first["fds"]) == (
        678, 503, 9, 14)
    assert first["ocr_med"] is None and first["shmem_mb"] is None, "n/a is NULL"
    assert (second["mi_use_mb"], second["mi_free_mb"], second["ocr_p95"]) == (1543, 272, 0.5)
    assert (second["load1"], second["load5"], second["load15"]) == (18.19, 11.16, 4.89)
    assert set(first) == set(rs.RESOURCE_FIGURES)


def test_the_summary_line_is_not_a_sample():
    samples, _ = rs.resources_from_log(_RESOURCE_LOG, _SESSION_START)
    assert len(samples) == 2


def test_a_resource_session_keeps_its_verdict_trim_and_account():
    _, session = rs.resources_from_log(_RESOURCE_LOG, _SESSION_START)
    assert session == rs.ResourceSession(
        _SESSION_START, "20261029_040000_acct2",
        "BOTH processes growing above 50 MB/h (game faster) — see Performance 008",
        1716, True, {"account": "mobius2"})


def test_a_running_session_is_not_complete_until_its_log_is_rotated():
    running = _RESOURCE_LOG[:2]
    _, session = rs.resources_from_log(running, _SESSION_START)
    assert (session.run_id, session.verdict, session.complete, session.tags) == (
        None, None, False, {})
    _, rotated = rs.resources_from_log(running, _SESSION_START, finished=True)
    assert rotated.complete


def test_a_log_without_a_sample_has_no_resource_session():
    assert rs.resources_from_log(_RESOURCE_LOG[2:], _SESSION_START) == ([], None)


def test_a_complete_sessions_log_is_not_read_again_but_the_current_log_always_is(tmp_path):
    done, open_, current = (tmp_path / n for n in ("a.log", "b.log", "wingman.log"))
    done.write_text(_line("01:00:00", "x"), encoding="utf-8")
    open_.write_text(_line("02:00:00", "x"), encoding="utf-8")
    current.write_text(_line("03:00:00", "x"), encoding="utf-8")
    complete = [rs.log_start(done), rs.log_start(current)]
    found = rs.logs_for_resources([done, open_], current, complete)
    assert [(p.name, finished) for p, _start, finished in found] == [
        ("b.log", True), ("wingman.log", False)]


def test_the_resource_rows_are_upserted_and_never_deleted():
    samples, session = rs.resources_from_log(_RESOURCE_LOG, _SESSION_START)
    script = rs.load_script("r\n", "c\n", "q\n", rs.resources_csv(samples),
                            rs.sessions_csv([session]))
    body = script[:script.index("COMMIT;")]
    assert "ON CONFLICT (session_start, elapsed_s) DO UPDATE" in body
    assert "ON CONFLICT (session_start) DO UPDATE" in body
    assert "DELETE FROM resource" not in script
    first = next(csv.reader(io.StringIO(rs.resources_csv(samples))))
    assert len(first) == len(rs.RESOURCE_COLUMNS)
    assert first[1:3] == ["0", "2026-10-29T04:00:00.123000-04:00"]
    assert first[3] == "678" and first[rs.RESOURCE_COLUMNS.index("ocr_med")] == ""
    (row,) = csv.reader(io.StringIO(rs.sessions_csv([session])))
    assert row[4] == "true" and json.loads(row[5]) == {"account": "mobius2"}


def test_the_schema_has_every_resource_column():
    schema = Path("grafana/schema.sql").read_text(encoding="utf-8")
    for column in rs.RESOURCE_COLUMNS + rs.SESSION_COLUMNS:
        assert re.search(rf"^\s+{column}\s", schema, re.M), column


def test_the_resource_panels_sit_under_the_score_panels_and_ignore_the_tags_box():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    panels = [p for p in dashboard["panels"]
              if any("resource_samples" in t["rawSql"] for t in p["targets"])]
    assert [p["title"] for p in panels] == ["Memory per sample (MB)",
                                            "Threads and open files per sample",
                                            "OCR time per sample", "Resources per session"]
    rest = panels[1:]  # Memory per sample sits under Score per round (2026-11-25)
    others = [p for p in dashboard["panels"] if p not in panels
              and not any("run_timings" in t["rawSql"] for t in p["targets"])]
    assert min(p["gridPos"]["y"] for p in rest) == max(
        p["gridPos"]["y"] + p["gridPos"]["h"] for p in others + panels[:1])
    assert not any("tags @>" in t["rawSql"] for p in panels for t in p["targets"])


def test_the_session_rates_start_after_the_summarys_warm_up():
    """The table's MB/h must be the RESOURCE SUMMARY's: from the first sample past the
    warm-up, as ResourceSampler measures it."""
    from wingman import resource_monitor
    sql = dict(_dashboard_sql())["Resources per session"]
    assert f"elapsed_s >= {resource_monitor._WARMUP_S:.0f}" in sql


# -- the run timings (the runtime trend HTML's figures, operator 2026-11-16) ----------

def _run(**over):
    data = {"version": "1.9.4", "run_id": "20261115_210159_acct2",
            "start_ts": _START, "end_ts": _START + 3600,
            "ocr_crops": {"incoming": {"n": 4731, "mean": 0.4296, "p50": 0.406, "p95": 0.5941,
                                       "p99": 0.6851},
                          "telemetry": {"n": 0, "mean": 0.0}},
            "reaction": {"n": 354, "mean": 0.4374, "p50": 0.4128, "p95": 0.577, "p99": 0.9119,
                         "max": 1.3517},
            "host": {"hostname": "veda"}}
    return data | over


def test_each_crop_and_the_reaction_of_a_run_file_is_a_timing_row():
    rows = rs.timings_from_run(_run())
    assert [(t.figure, t.n, t.mean, t.p95) for t in rows] == [
        ("incoming", 4731, 0.4296, 0.5941), ("reaction", 354, 0.4374, 0.577)]
    first = rows[0]
    assert (first.run_id, first.wingman_version) == ("20261115_210159_acct2", "1.9.4")
    assert first.started_at == datetime.fromtimestamp(_START, timezone.utc)
    assert first.ended_at - first.started_at == timedelta(hours=1)
    assert first.tags == {"account": "mobius2", "host": "veda"}


def test_a_figure_without_samples_gives_no_timing():
    """Its mean of 0 is not a time: the zero telemetry crop above, a run with no
    warning, and an old file's empty blocks."""
    rows = rs.timings_from_run(_run(reaction={"n": 0, "mean": 0.0}, ocr_crops={}))
    assert rows == []


def test_a_run_file_without_a_run_id_or_a_start_gives_no_timing():
    assert rs.timings_from_run(_run(run_id=None)) == []
    assert rs.timings_from_run(_run(start_ts=None)) == []


def test_an_old_run_file_has_no_host_tag():
    data = _run()
    del data["host"]
    assert rs.timings_from_run(data)[0].tags == {"account": "mobius2"}


def test_the_run_files_are_read_and_the_stats_files_beside_them_are_not(tmp_path):
    (tmp_path / "run_20261115_210159_acct2.json").write_text(json.dumps(_run()), encoding="utf-8")
    (tmp_path / "run_20261115_210159_acct2_stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / "run_broken.json").write_text("{", encoding="utf-8")
    assert [r["run_id"] for r in rs.read_runs([tmp_path])] == ["20261115_210159_acct2"]


def test_the_run_timings_are_upserted_and_never_deleted():
    rows = rs.timings_from_run(_run())
    script = rs.load_script("r\n", "c\n", timings_text=rs.timings_csv(rows))
    body = script[:script.index("COMMIT;")]
    assert "ON CONFLICT (run_id, figure) DO UPDATE" in body
    assert "DELETE FROM run_timings" not in script
    first = next(csv.reader(io.StringIO(rs.timings_csv(rows))))
    assert len(first) == len(rs.TIMING_COLUMNS)
    assert first[1] == "incoming" and first[4:7] == ["1.9.4", "4731", "0.4296"]
    assert json.loads(first[-1]) == {"account": "mobius2", "host": "veda"}


def test_the_schema_has_every_timing_column():
    schema = Path("grafana/schema.sql").read_text(encoding="utf-8")
    table = schema[schema.index("CREATE TABLE IF NOT EXISTS run_timings"):]
    for column in rs.TIMING_COLUMNS:
        assert re.search(rf"^\s+{column}\s", table, re.M), column


def _timing_panels(dashboard):
    return [p for p in dashboard["panels"]
            if any("run_timings" in t["rawSql"] for t in p["targets"])]


def test_the_timing_panels_sit_under_the_resource_panels_and_follow_the_version_box():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    panels = _timing_panels(dashboard)
    assert [p["title"] for p in panels] == ["OCR time per crop per session",
                                            "Missile warning to flares per session",
                                            "OCR and reaction time by version"]
    others = [p for p in dashboard["panels"] if p not in panels]
    assert min(p["gridPos"]["y"] for p in panels) == max(
        p["gridPos"]["y"] + p["gridPos"]["h"] for p in others)
    sqls = [t["rawSql"] for p in panels for t in p["targets"]]
    assert all("wingman_version IN (${version:sqlstring})" in sql for sql in sqls)
    assert not any("tags @>" in sql for sql in sqls)
    version = next(v for v in dashboard["templating"]["list"] if v["name"] == "version")
    assert version["multi"] and version["includeAll"] and "run_timings" in version["query"]


def test_the_version_table_weights_each_mean_by_its_count_as_the_html_did():
    sql = dict(_dashboard_sql())["OCR and reaction time by version"]
    for figure in ("incoming", "respawn", "health", "ammo_flares", "ammo_missiles", "telemetry",
                   "reaction"):
        assert f"sum(mean * n) FILTER (WHERE figure = '{figure}')" in sql, figure
        assert f"nullif(sum(n) FILTER (WHERE figure = '{figure}'), 0)" in sql, figure


# -- the switch -------------------------------------------------------------------

def test_the_shipped_config_leaves_the_dashboard_off(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, config)
    assert rs.check(config) == 1


def test_an_overlay_switches_it_on(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, config)
    (tmp_path / "config.local.yaml").write_text("score_dashboard:\n  enabled: true\n",
                                                encoding="utf-8")
    assert rs.check(config) == 0


def test_an_overlay_that_does_not_validate_is_refused(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, config)
    (tmp_path / "config.local.yaml").write_text("score_dashboard:\n  enable: true\n",
                                                encoding="utf-8")
    assert rs.check(config) == 2


# -- a manual takeover (operator, 2026-10-29) ---------------------------------------

def test_a_round_with_a_manual_takeover_is_kept_with_its_count():
    """The dashboard charts it apart from wingman's own: 2026-10-29, round 6,
    four takeovers."""
    rows = rs.rows_from_stats(_stats([_mission(0, performance=_READ),
                                      _mission(1, performance=_READ, manual_takeover_count=4),
                                      _mission(2, manual_takeover_count=0)]))
    assert [(r.mission_index, r.manual_takeovers) for r in rows] == [(0, 0), (1, 4), (2, 0)]
    assert rs.rounds_csv(rows[1:2]).rstrip().endswith(",4")


def test_a_log_round_counts_its_takeovers_and_they_do_not_carry_over():
    log = [
        _line("04:00:00", "🎮 Game state: GAME_STARTING → GAME_BATTLE"),
        _line("04:02:00", "🎮 Game state: GAME_BATTLE_EJECT → GAME_BATTLE_MANUAL"),
        _line("04:03:00", "🎮 Game state: GAME_BATTLE_MANUAL → GAME_BATTLE"),
        _line("04:04:00", "🎮 Game state: GAME_BATTLE → GAME_BATTLE_MANUAL"),
        _line("04:05:00", "🎮 Game state: GAME_BATTLE_MANUAL → GAME_END_B"),
        _line("04:05:02", "📋 ROUND PERFORMANCE: score 900, K/D/A 6/1/2"),
        _line("04:05:09", "🎮 Game state: GAME_END_B → GAME_LOBBY"),
        _line("04:06:00", "🎮 Game state: GAME_STARTING → GAME_BATTLE"),
        _line("04:11:00", "🎮 Game state: GAME_BATTLE → GAME_END_B"),
        _line("04:11:02", "📋 ROUND PERFORMANCE: score 200, K/D/A 1/3/0"),
    ]
    assert [(r.score, r.manual_takeovers) for r in rs.rows_from_log(log, "x")] == [
        (900, 2), (200, 0)]


def _dashboard_sql():
    """(panel title, query) for every query of every panel."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    return [(p["title"], t["rawSql"]) for p in dashboard["panels"] for t in p["targets"]]


def _score_per_round():
    return [sql for title, sql in _dashboard_sql() if title.startswith("Score per round")]


def _score_panel():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    return next(p for p in dashboard["panels"] if p["title"].startswith("Score per round"))


def test_every_round_panel_but_score_per_round_is_wingman_only():
    others = [sql for title, sql in _dashboard_sql()
              if not title.startswith("Score per round") and "FROM rounds" in sql]
    assert others and all("manual_takeovers = 0" in sql for sql in others)


def test_score_per_round_draws_each_account_and_the_takeovers_apart():
    """Operator, 2026-10-29: a dot colour per account, wingman's moving average,
    and the rounds with a manual takeover with their own."""
    _hover, dots, wingman, taken_over = _score_per_round()
    assert "AS metric" in dots and "tags->>'account'" in dots
    assert "AND manual_takeovers = 0" in dots and "AND manual_takeovers = 0" in wingman
    assert '"wingman, moving average"' in wingman
    assert "NOT manual_takeovers = 0" in taken_over
    assert '"manual takeover"' in taken_over and '"manual takeover, moving average"' in taken_over


def test_hovering_a_dot_names_the_rounds_account_jet_and_kda():
    """Operator, 2026-11-01. A time series tooltip shows a series' name and value and
    nothing else from its row, so each account, jet and K/D/A is its own series."""
    hover, *_ = _score_per_round()
    assert "AS metric" in hover and "score AS value" in hover
    for part in ("tags->>'account'", "coalesce(jet,", "K/D/A ", "kills", "deaths", "assists"):
        assert part in hover
    assert "manual_takeovers = 0 AND" not in hover, "an orange dot has its details too"


def test_a_round_with_no_jet_keeps_its_details():
    """In SQL a NULL jet would blank the whole name, and 548 read rounds have none."""
    hover, *_ = _score_per_round()
    assert "coalesce(jet, 'jet not recorded')" in hover
    assert "LEFT JOIN jet_names" in hover


def test_the_hover_series_win_a_tie_with_the_account_dots():
    """Both draw the round at one spot and the first series wins the tie. Queried after
    the account dots, the tooltip read `mobius3 300` with no details."""
    assert _score_panel()["targets"][0]["refId"] == "D"


def test_the_hover_series_draw_nothing_so_the_legend_still_isolates_an_account():
    """A legend click leaves alone any series hidden from the legend. Drawn as dots, every
    account stayed on screen when one was isolated. These must also stay out of the
    legend: 460 of them on 2026-11-01."""
    (hover,) = [o for o in _score_panel()["fieldConfig"]["overrides"]
                if o["matcher"] == {"id": "byFrameRefID", "options": "D"}]
    props = {q["id"]: q["value"] for q in hover["properties"]}
    assert props["custom.hideFrom"] == {"legend": True, "tooltip": False, "viz": False}
    assert props["custom.drawStyle"] == "line"
    assert props["custom.lineWidth"] == 0
    assert props["custom.showPoints"] == "never"


def test_both_moving_averages_are_lines_and_the_takeover_one_shows_its_points():
    """Operator, 2026-10-29: two lines, to see whether a manual takeover scores better
    than wingman alone. Taken-over rounds are few; a line with one point draws
    nothing unless its points show."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    panel = next(p for p in dashboard["panels"] if p["title"].startswith("Score per round"))
    props = {o["matcher"]["options"]: {q["id"]: q["value"] for q in o["properties"]}
             for o in panel["fieldConfig"]["overrides"]}
    for name in ("wingman, moving average", "manual takeover, moving average"):
        assert props[name]["custom.drawStyle"] == "line"
    assert props["manual takeover, moving average"]["custom.showPoints"] == "always"


def _by_jet():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    return next(p for p in dashboard["panels"] if p["title"] == "Wingman by jet")


def test_the_jet_panel_ranks_wingmans_rounds_and_shows_how_much_it_rests_on():
    sql = _by_jet()["targets"][0]["rawSql"]
    assert "manual_takeovers = 0" in sql and "tags @> ${tags:sqlstring}::jsonb" in sql
    assert '"Mean score" DESC NULLS LAST' in sql
    for column in ('"Jet"', '"Rounds read"', '"± 95%"'):
        assert column in sql


def test_rounds_without_a_jet_are_shown_but_never_ranked_among_the_jets():
    sql = _by_jet()["targets"][0]["rawSql"]
    assert "'not recorded'" in sql
    assert "LEFT JOIN jet_names" in sql
    assert sql.index("ORDER BY jet IS NULL") < sql.index('"Mean score" DESC')


def test_a_jet_with_few_rounds_cannot_top_the_jets_that_have_many():
    """In a trial on real rounds, one lucky round topped the table at 755, above a
    jet with 47 rounds read."""
    sql = _by_jet()["targets"][0]["rawSql"]
    assert sql.index("count(score) < 10") < sql.index('"Mean score" DESC')


def test_the_jet_panels_bars_are_one_colour_not_good_and_bad():
    """A bar's colour must not rank the jets: its length does that."""
    (bars,) = _by_jet()["fieldConfig"]["overrides"]
    assert bars["matcher"]["options"] == "Mean score"
    properties = {p["id"]: p["value"] for p in bars["properties"]}
    assert properties["custom.cellOptions"]["type"] == "gauge"
    assert properties["color"] == {"mode": "fixed", "fixedColor": "blue"}


def _jet_ranges():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    return next(p for p in dashboard["panels"] if p["title"].startswith("Score range by jet"))


def _hide_from(panel):
    """The `custom.hideFrom` each override sets, by its matcher's options."""
    return {o["matcher"]["options"]: q["value"] for o in panel["fieldConfig"]["overrides"]
            for q in o["properties"] if q["id"] == "custom.hideFrom"}


def test_the_jet_ranges_are_one_bar_per_jet_from_its_lowest_to_its_highest():
    """Operator, 2026-11-03: a candlestick of score by jet, the wick from the lowest
    score to the highest and a tick at the mean. Grafana's candlestick panel draws
    time along the bottom, so the bars stack: an empty base up to the lowest, then
    up to the tick, the tick, and on to the highest."""
    panel = _jet_ranges()
    assert panel["type"] == "barchart"
    assert panel["options"]["xField"] == "Jet" and panel["options"]["stacking"] == "normal"
    sql = panel["targets"][0]["rawSql"]
    stack = ['"below the lowest"', '"lowest to mean"', '"mean tick"', '"mean to highest"']
    assert [sql.index(name) for name in stack] == sorted(sql.index(name) for name in stack)
    (base,) = [o for o in panel["fieldConfig"]["overrides"]
               if o["matcher"] == {"id": "byName", "options": "below the lowest"}]
    assert {"id": "custom.fillOpacity", "value": 0} in base["properties"]


def test_the_jet_ranges_put_the_jet_with_the_most_rounds_on_the_left():
    """Operator, 2026-11-03: the most rounds on the left, the fewest on the right."""
    sql = _jet_ranges()["targets"][0]["rawSql"]
    assert "ORDER BY rounds DESC, mean DESC" in sql


def test_the_jet_ranges_leave_out_rounds_with_no_jet():
    """Those rounds flew every jet, so their range would be no jet's."""
    assert "tags->>'jet' IS NOT NULL" in _jet_ranges()["targets"][0]["rawSql"]


def test_hovering_a_jets_bar_shows_its_figures_not_the_stack():
    """The stacked segments' values are lengths, so they stay out of the tooltip; the
    figures ride along hidden from the chart, and the tooltip lists them."""
    hide = _hide_from(_jet_ranges())
    assert hide["/^(below the lowest|lowest to mean|mean tick|mean to highest)$/"] == {
        "legend": True, "tooltip": True, "viz": False}
    assert hide["/^(Highest|Mean score|Lowest|Rounds read)$/"] == {
        "legend": True, "tooltip": False, "viz": True}


def test_the_dashboard_is_at_schema_42_so_hidden_figures_stay_in_the_tooltip():
    """Grafana 12.2 migrates a dashboard below schema 42 by hiding from the tooltip
    every field hidden from the chart (`migrateHideFromFunctionality`). At 39 the jet
    ranges' tooltip showed the jet's name and nothing else."""
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    assert dashboard["schemaVersion"] >= 42


def _colours(name):
    """The colours the name-pattern overrides give a series called `name`."""
    return [p["value"]["fixedColor"] for o in _score_panel()["fieldConfig"]["overrides"]
            if o["matcher"]["id"] == "byRegexp"
            and re.search(o["matcher"]["options"].strip("/"), name)
            for p in o["properties"] if p["id"] == "color"]


def test_the_accounts_have_their_own_colours():
    """The hover series take their account's colour, so the ring on a hovered dot does too."""
    colours = [_colours(a) for a in ("mobius0", "mobius1", "mobius2", "mobius3", "no account")]
    assert all(len(c) == 1 for c in colours) and len({c[0] for c in colours}) == 5
    assert _colours("mobius2, J35 DRAKEN, K/D/A 0/5/0") == _colours("mobius2")
    assert not any(_colours(name) for name in ("main", "acct1", "acct2", "acct3"))


def test_a_taken_over_rounds_hover_series_stays_orange():
    assert _colours("manual takeover, mobius2, J35 DRAKEN, K/D/A 0/5/0") == ["orange"]


# -- Phase 2: the ingest after a session ---------------------------------------------

@pytest.fixture
def switched_on(tmp_path):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, config)
    (tmp_path / "config.local.yaml").write_text("score_dashboard:\n  enabled: true\n",
                                                encoding="utf-8")
    return config


def _after_session(monkeypatch, config, host="veda", up=True, fail=None):
    calls = []

    def fake_ingest():
        calls.append("ingest")
        if fail:
            raise RuntimeError(fail)
        return 0

    monkeypatch.setattr(rs.socket, "gethostname", lambda: host)
    monkeypatch.setattr(rs, "stack_up", lambda: up)
    monkeypatch.setattr(rs, "ingest", fake_ingest)
    return rs.after_session(config), calls


def test_after_a_session_the_ingest_runs_when_every_gate_passes(monkeypatch, switched_on):
    assert _after_session(monkeypatch, switched_on) == (0, ["ingest"])


@pytest.mark.parametrize("host, up", [("laptop", True), ("veda", False)])
def test_after_a_session_nothing_runs_off_veda_or_with_the_stack_down(monkeypatch, switched_on,
                                                                       capsys, host, up):
    assert _after_session(monkeypatch, switched_on, host=host, up=up) == (0, [])
    assert capsys.readouterr().err == "", "silent, so make r on a clone behaves as before"


def test_after_a_session_nothing_runs_with_the_dashboard_off(monkeypatch, tmp_path, capsys):
    config = tmp_path / "config.yaml"
    shutil.copy(CONFIG_PATH, config)
    assert _after_session(monkeypatch, config) == (0, [])
    assert capsys.readouterr().err == ""


def test_a_failed_ingest_after_a_session_is_printed_and_ignored(monkeypatch, switched_on, capsys):
    assert _after_session(monkeypatch, switched_on, fail="docker gone") == (0, ["ingest"])
    assert "after-session ingest failed (ignored): docker gone" in capsys.readouterr().err

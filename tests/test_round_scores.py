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
    ("20261031_023505_acct1", "mobius0"),     # its end screen at 03:16 reads MOBIUS0
    ("20261030_162007", "mobius1"),           # its end screen at 16:25 reads MOBIUS1
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
    # 2026-10-28 00:56: a second click-through of the same end screens.
    _line("03:09:29", "ROUND PERFORMANCE: not read (4 frame(s), no two read alike)"),
]


def test_a_log_gives_one_row_per_round_read_or_not():
    rows = rs.rows_from_log(_LOG, "20261029_025029_log")
    assert [(r.mission_index, r.score, r.kills, r.deaths, r.assists) for r in rows] == [
        (0, 230, 2, 5, 0), (1, None, None, None, None)]
    assert all(r.tags == {"source": "log"} and r.started_at is None for r in rows)
    assert rows[0].ended_at == datetime(2026, 10, 29, 2, 56, 51, 123000, tzinfo=rs.TZ)


def test_a_reading_with_no_end_screen_before_it_is_not_a_round():
    assert len(rs.rows_from_log(_LOG, "x")) == 2, "the 03:09:29 second click-through"


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
    """2026-10-30 07:25: a `make r2` session the shutdown watchdog ended wrote no
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


def test_no_squadron_panel_depends_on_the_account():
    """A filter by account could only hide a reading of the same figure."""
    sqls = [sql for _title, sql in _dashboard_sql() if "squadron_rankings" in sql]
    assert sqls and not any("tags" in sql or "account" in sql for sql in sqls)


def test_the_squadron_charts_sit_right_under_score_per_round():
    dashboard = json.loads(Path("grafana/dashboards/score-per-round.json").read_text(encoding="utf-8"))
    place = {p["title"].split(" (")[0]: p["gridPos"] for p in dashboard["panels"]}
    score = place["Score per round: wingman and manual takeover"]
    below = score["y"] + score["h"]
    assert place["Squadron rank per reading"]["y"] == below
    assert place["Squadron trophies per reading"]["y"] == below


def test_the_schema_keeps_the_trophies():
    schema = Path("grafana/schema.sql").read_text(encoding="utf-8")
    assert "ALTER TABLE squadron_rankings ADD COLUMN IF NOT EXISTS trophies bigint;" in schema


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
    rounds = [sql for sql in sqls if "squadron_rankings" not in sql]
    assert rounds and all("tags @> ${tags:sqlstring}::jsonb" in sql for sql in rounds)
    assert not any("tags:raw" in sql for sql in sqls)


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
    """The dashboard charts it apart from wingman's own: 2026-10-29 17:40, round 6,
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
    for part in ("tags->>'account'", "tags->>'jet'", "K/D/A ", "kills", "deaths", "assists"):
        assert part in hover
    assert "manual_takeovers = 0 AND" not in hover, "an orange dot has its details too"


def test_a_round_with_no_jet_keeps_its_details():
    """In SQL a NULL jet would blank the whole name, and 548 read rounds have none."""
    hover, *_ = _score_per_round()
    assert "coalesce(tags->>'jet', 'jet not recorded')" in hover


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
    assert sql.index("tags->>'jet' IS NULL") < sql.index('"Mean score" DESC')


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

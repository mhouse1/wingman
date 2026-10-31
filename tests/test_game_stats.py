"""Research 017 step 1: the game's own frame rate for each match.

MetalStorm saves its last match's average frame rate, bad frames and ping in
the prefix's registry; wingman logs them as ``GAME STATS:`` so the frame-rate
pass mark (a vision model sharing the Intel chip must leave the game at 50 FPS
or more) is measured a match at a time, from the game's own figures.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from wingman.game_stats import (PREFIX_ENV, GameStatsWatcher, LastMatchStats,
                                parse_last_match_stats)

# Account 1's lines on 2026-10-19 after the last Su-30 match, copied from its
# user.reg: the real key names and hash suffixes, and Wine's hex(4) doubles
# continued over two lines. The Google Analytics section is a decoy holding a
# key of the same name, which must not be read.
REAL = r'''WINE REGISTRY Version 2
;; All keys relative to REGISTRY\\User\\S-1-5-21-0-0-1000-1000

#arch=win64

[Software\\Google\\GoogleAnalytics\\google.pcc.games.test.starforminc.metalstorm] 1790329374
#time=1dcba78c213cbe8
"air-combat-settings--default_lastMatchAvFps_h2984942035"=hex(4):00,00,00,00,00,00,00,00

[Software\\Starform\\Metalstorm] 1792431165
#time=1dd5fefda287130
"air-combat-hangar-default_desktopHangarSortMethod_h807392999"=hex:54,72,6f,70,\
  68,69,65,73,00
"air-combat-settings--default_lastMatchAvFps_h2984942035"=hex(4):00,00,00,a0,\
  21,b6,4b,40
"air-combat-settings--default_lastMatchAvgPing_h3649036353"=hex(4):00,00,00,00,\
  00,00,30,40
"air-combat-settings--default_lastMatchPercentBadFrames_h393952115"=hex(4):00,\
  00,00,a0,0a,ec,84,3f
"air-combat-settings--default_musicVolume_h1560083537"=hex(4):00,00,00,00,00,\
  00,00,00

[Software\\Wine] 1774238072
'''
# The main prefix's average on the same day: 60.01 FPS.
SIXTY = "hex(4):00,00,00,e0,\\\n  38,01,4e,40"


def _next_match(text: str) -> str:
    return text.replace("hex(4):00,00,00,a0,\\\n  21,b6,4b,40", SIXTY)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _write(path: Path, text: str, mtime: float) -> None:
    path.write_text(text, encoding="utf-8")
    os.utime(path, (mtime, mtime))


def test_reads_the_three_figures_from_the_real_format():
    stats = parse_last_match_stats(REAL)
    assert stats == LastMatchStats(pytest.approx(55.42, abs=0.01),
                                   pytest.approx(0.0102, abs=0.0001), 16.0)


def test_only_the_metalstorm_section_counts():
    decoy_only = REAL.split("[Software\\\\Starform\\\\Metalstorm]")[0]
    assert parse_last_match_stats(decoy_only) is None


def test_a_missing_figure_reads_as_none():
    no_ping = REAL.replace('"air-combat-settings--default_lastMatchAvgPing_h3649036353"',
                           '"something-else"')
    assert parse_last_match_stats(no_ping) is None


def test_each_match_is_logged_once_and_the_first_reading_is_the_last_session(tmp_path, caplog):
    reg = tmp_path / "user.reg"
    _write(reg, REAL, 1.0)
    clock = _Clock()
    watcher = GameStatsWatcher(str(tmp_path), interval_s=15.0, clock=clock)
    with caplog.at_level(logging.INFO, logger="wingman.game_stats"):
        assert watcher.maybe_poll().avg_fps == pytest.approx(55.42, abs=0.01)
        _write(reg, _next_match(REAL), 2.0)
        assert watcher.maybe_poll() is None            # not due yet
        clock.t += 15.0
        assert watcher.maybe_poll().avg_fps == pytest.approx(60.01, abs=0.01)
        _write(reg, _next_match(REAL), 3.0)            # re-saved, same figures
        clock.t += 15.0
        assert watcher.maybe_poll() is None
    lines = [r.getMessage() for r in caplog.records if "GAME STATS" in r.getMessage()]
    assert len(lines) == 2
    assert "before this session avg_fps=55.4" in lines[0]
    assert "last match avg_fps=60.0" in lines[1]


def test_an_unchanged_file_is_not_parsed_again(tmp_path, monkeypatch):
    reg = tmp_path / "user.reg"
    _write(reg, REAL, 1.0)
    clock = _Clock()
    watcher = GameStatsWatcher(str(tmp_path), clock=clock)
    watcher.maybe_poll()
    calls = []
    monkeypatch.setattr("wingman.game_stats.parse_last_match_stats",
                        lambda text: calls.append(1))
    clock.t += 60.0
    assert watcher.maybe_poll() is None
    assert calls == []


def test_no_prefix_means_off(monkeypatch):
    monkeypatch.delenv(PREFIX_ENV, raising=False)
    watcher = GameStatsWatcher.from_env()
    assert not watcher.enabled
    assert watcher.maybe_poll() is None


def test_an_unreadable_prefix_warns_once_and_never_raises(tmp_path, caplog):
    clock = _Clock()
    watcher = GameStatsWatcher(str(tmp_path / "missing"), clock=clock)
    with caplog.at_level(logging.WARNING, logger="wingman.game_stats"):
        assert watcher.maybe_poll() is None
        clock.t += 60.0
        assert watcher.maybe_poll() is None
    assert sum("cannot read" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
def test_the_run_targets_tell_wingman_the_accounts_prefix():
    """r1's WINE_PREFIX is target-specific; it has to reach r's recipe, the one
    that logs to wingman.log."""
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run(["make", "-n", "r1", "GAME_LAUNCH_DEPS="], cwd=root,
                         capture_output=True, text=True, timeout=60).stdout
    run = [line for line in out.splitlines() if "-m wingman.main" in line]
    assert len(run) == 1, out
    assert "--log-file wingman.log" in run[0]
    assert 'WINGMAN_WINE_PREFIX="' in run[0]
    assert run[0].split('WINGMAN_WINE_PREFIX="')[1].split('"')[0].endswith("Metalstorm-acct1")


ACCT1_REG = Path.home() / "Games/Heroic/Prefixes/Metalstorm-acct1/user.reg"


@pytest.mark.skipif(not ACCT1_REG.exists(), reason="no account 1 prefix on this machine")
def test_the_live_account_prefix_parses():
    """The whole real file, chat and all, not only the copied lines above."""
    stats = parse_last_match_stats(ACCT1_REG.read_text(encoding="utf-8", errors="replace"))
    if stats is None:
        pytest.skip("account 1 has no match figures yet")
    assert 5.0 < stats.avg_fps < 500.0
    assert 0.0 <= stats.ping_ms < 2000.0

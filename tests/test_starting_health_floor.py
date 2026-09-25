"""ADR 102 rev 2: GAME_STARTING must not launch a mission on lobby digits.

2026-09-25 10:54:29. The game sat at the lobby with PLAY on screen; the
GAME_STARTING health probe read two low digit fragments off it:

    10:54:00  GAME_STARTING health probe #9  (+12.5s since armed): raw=0
    10:54:29  GAME_STARTING health probe #28 (+41.0s since armed): raw=7
    10:54:29  Analyzer: health 7 confirmed in GAME_STARTING -> game_battle_alive=True
    10:54:29  Controller: game_battle_alive detected in GAME_STARTING - launching mission immediately

ADR 063's recurrence filter accepts two of the last three reads that agree
within value_confirm_tolerance (15), and |0 - 7| is 7, so two different garbage
digits confirmed each other. The mission then ran inside the lobby for 25 s
until the operator forced GAME_LOBBY by hand.

Measured across every September log: 1,724 confirmations from this probe
(archive duplicates included), 1,718 of them at 160 to 312 (the aircraft's
full health) and 6 at 7. A spawn cannot start below its aircraft's full
health, so the floor separates the populations by a factor of 20. Four of the
six 7s coincided with a loading screen and were harmless (the mission restarted
when the real health arrived about 45 s later); two coincided with the lobby
(2026-09-25 08:54:06, PLAY seen 3.9 s earlier, and 10:54:29, PLAY seen 0.3 s
earlier).
"""

import copy
from pathlib import Path

import pytest
import yaml

from constants import CONFIG_PATH
from wingman.analyzer import (
    GameState,
    GameStateAnalyzer,
    STARTING_MIN_CONFIRMED_HEALTH,
)


def _make_analyzer() -> GameStateAnalyzer:
    with Path(CONFIG_PATH).open("r", encoding="utf-8") as fh:
        cfg = copy.deepcopy(yaml.safe_load(fh))
    a = GameStateAnalyzer(cfg)
    a.state = GameState.GAME_STARTING.name
    return a


@pytest.fixture
def analyzer():
    a = _make_analyzer()
    try:
        yield a
    finally:
        a.cleanup()


def _verdicts(a, *raws):
    return [a._starting_health_verdict(raw, attempt=i + 1)
            for i, raw in enumerate(raws)]


def test_the_floor_is_far_below_every_real_spawn_and_far_above_the_lobby_digits():
    assert 8 < STARTING_MIN_CONFIRMED_HEALTH <= 100


def test_the_incident_reads_do_not_launch_a_mission(analyzer):
    """0 then 7 is the exact pair from 10:54. Before this change the second
    read returned 7 (confirmed) and launched the mission in the lobby."""
    assert _verdicts(analyzer, 0, 7) == [None, None]


def test_two_agreeing_low_reads_do_not_launch_either(analyzer):
    """Even a genuinely recurring fragment (7, 7) is not a spawn health."""
    assert _verdicts(analyzer, 7, 7) == [None, None]


def test_a_real_spawn_still_launches_on_its_second_agreeing_read(analyzer):
    """160 is what all ten legitimate launches of that session read. The ADR
    063 requirement for a second read is unchanged."""
    assert _verdicts(analyzer, 160, 160) == [None, 160]


@pytest.mark.parametrize("health", [160, 163, 201, 203, 208, 250, 312])
def test_every_health_ever_seen_at_a_real_launch_clears_the_floor(analyzer, health):
    assert _verdicts(analyzer, health, health) == [None, health]


def test_lobby_fragments_before_a_real_spawn_do_not_block_it(analyzer):
    """The window keeps the garbage, and the real reads still confirm through
    it: [0, 7, 160] is unconfirmed, [7, 160, 160] confirms 160."""
    assert _verdicts(analyzer, 0, 7, 160, 160) == [None, None, None, 160]


def test_the_floor_is_applied_to_the_confirmed_value_not_to_the_first_read(analyzer):
    """A read of 7 followed by 160 must not be dragged down by the earlier 7:
    the verdict is about the value that recurred."""
    assert _verdicts(analyzer, 7, 160, 160)[-1] == 160

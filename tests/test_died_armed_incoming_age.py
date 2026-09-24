"""ADR 143 log line: an incoming alert that never fired is "never", not the epoch.

Seen 2026-09-24 12:11: `DIED ARMED ... cause=terrain (incoming 1790266284.2s ago)`.
`_classify_died_armed` subtracted an unset 0.0 timestamp from `time.time()`. The verdict
was right (terrain is checked first, from the behavior tree's hard-emergency timestamp);
the number was meaningless.
"""

from types import SimpleNamespace

from wingman.tick_handlers import RespawnHandler, _fmt_incoming_age


def _classify(last_incoming_ts, now=1_790_266_284.0, hard_emergency_ts=0.0):
    tree = SimpleNamespace(climb_last_hard_emergency_ts=lambda: hard_emergency_ts)
    stub = SimpleNamespace(
        _ammo_events=SimpleNamespace(last_incoming_alert_ts=last_incoming_ts),
        _behavior_tree=tree, _terrain_lookback_s=10.0, _enemy_fire_lookback_s=12.0)
    return RespawnHandler._classify_died_armed(stub, now)


def test_an_unset_alert_timestamp_is_an_infinite_age_not_the_epoch():
    _cause, since = _classify(0.0)
    assert since == float("inf")


def test_the_unset_alert_reads_as_words_in_the_log():
    assert _fmt_incoming_age(_classify(0.0)[1]) == "no incoming alert this session"


def test_a_real_alert_still_prints_its_age():
    assert _fmt_incoming_age(6.04) == "incoming 6.0s ago"
    assert _fmt_incoming_age(350.9) == "incoming 350.9s ago"


def test_a_recent_alert_is_still_enemy_fire():
    now = 1_790_266_284.0
    assert _classify(now - 6.0, now=now) == ("enemy_fire", 6.0)


def test_no_alert_and_no_emergency_is_unclassified():
    assert _classify(0.0)[0] == "unclassified"


def test_a_hard_emergency_is_terrain_even_with_no_alert_ever():
    """The 12:11 case: terrain comes from the emergency signal, not from the absence
    of an alert."""
    now = 1_790_266_284.0
    assert _classify(0.0, now=now, hard_emergency_ts=now - 3.0)[0] == "terrain"


def test_an_old_alert_beyond_the_lookback_is_unclassified():
    now = 1_790_266_284.0
    assert _classify(now - 350.9, now=now)[0] == "unclassified"

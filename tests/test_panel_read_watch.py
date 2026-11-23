"""FR-012: PanelReadWatch warns once per stretch of battle without a usable read."""

import logging

from wingman.panel_read_watch import PanelReadWatch


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _feed(watch, clock, label, value, seconds, step=1.5):
    raised = 0
    for _ in range(int(seconds / step)):
        clock.t += step
        raised += watch.note_count(label, value)
    return raised


def test_warns_once_after_silent_battle_time(caplog):
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    with caplog.at_level(logging.WARNING):
        assert _feed(watch, clock, "AMMO_MISSILE", None, 300) == 1
    assert "AMMO_MISSILE" in caplog.text and "FR-012" in caplog.text


def test_does_not_warn_while_reads_work():
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    for _ in range(200):
        clock.t += 1.5
        assert not watch.note_count("AMMO_MISSILE", None)
        clock.t += 1.5
        assert not watch.note_count("AMMO_MISSILE", 4)


def test_gaps_between_rounds_do_not_count():
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    for _ in range(10):  # ten rounds of 60 s silent battle, 90 s lobby between
        assert _feed(watch, clock, "AMMO_MISSILE", None, 60) == 0
        clock.t += 90
        watch.note_count("AMMO_MISSILE", 4)
    assert _feed(watch, clock, "AMMO_MISSILE", None, 100) == 0


def test_lobby_gap_does_not_add_to_a_silent_stretch():
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    assert _feed(watch, clock, "AMMO_FLARES", None, 90) == 0
    clock.t += 600
    assert _feed(watch, clock, "AMMO_FLARES", None, 25) == 0
    assert _feed(watch, clock, "AMMO_FLARES", None, 10) == 1


def test_warns_again_after_recovering(caplog):
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    assert _feed(watch, clock, "AMMO_MISSILE", None, 150) == 1
    with caplog.at_level(logging.INFO):
        _feed(watch, clock, "AMMO_MISSILE", 3, 3)
    assert "reads again" in caplog.text
    assert _feed(watch, clock, "AMMO_MISSILE", None, 150) == 1


def test_off_at_zero():
    clock = _Clock()
    watch = PanelReadWatch(0.0, clock=clock)
    assert _feed(watch, clock, "AMMO_MISSILE", None, 1000) == 0


def test_labels_are_independent():
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    for _ in range(100):
        clock.t += 1.5
        watch.note_count("AMMO_FLARES", 4)
        assert not watch.note_count("AMMO_MISSILE", 2)


def _names(watch, clock, texts, cls=None):
    raised = 0
    for text in texts:
        clock.t += 1.5
        raised += watch.note_weapon_name(text, cls)
    return raised


def test_clipped_name_misreads_warn():
    # 2026-11-19: the clipped label read 'ee' and 'ae'
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    assert _names(watch, clock, ["ee", "ae", ""] * 40) == 1


def test_a_classed_name_is_usable():
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    assert _names(watch, clock, ["LAU-3 Hydra Rocket Pod"] * 200, cls="rocket") == 0


def test_an_unclassed_name_that_repeats_is_usable():
    # Meteor has no weapon class; it reads the same each time
    clock = _Clock()
    watch = PanelReadWatch(120.0, clock=clock)
    assert _names(watch, clock, ["Meteor"] * 200) == 0

"""Code Review 019 cycle 38: the behavior tree decides on this tick's telemetry.

Every climb trigger used to act on a frame 1.54 s old (409 triggers, 2026-10-18/19)
while its OCR took 0.41 s: the finished reading waited for the next tick. The main
loop now waits, up to ``telemetry.bt_wait_s`` into the tick, for the reading of the
tick's own frame (wingman/main.py, ``_wait_for_tick_telemetry``).
"""

import threading
import time
from pathlib import Path

import yaml

from wingman.main import _wait_for_tick_telemetry


class _Analyzer:
    """The four members the wait reads, driven by the test."""

    def __init__(self, landed=False, in_flight=True):
        self.landed = landed
        self.in_flight = in_flight
        self.telemetry_event = threading.Event()
        self.incoming_event = threading.Event()

    def telemetry_landed_since(self, since_ts):
        return self.landed

    def telemetry_in_flight_since(self, since_ts):
        return self.in_flight

    def land(self):
        self.landed = True
        self.in_flight = False
        self.telemetry_event.set()


class _Ammo:
    def __init__(self):
        self.calls = 0

    def deploy_flares_on_new_incoming(self):
        self.calls += 1
        return True


def test_a_reading_already_in_does_not_wait():
    t0 = time.time()
    assert _wait_for_tick_telemetry(_Analyzer(landed=True), _Ammo(), t0, 0.75)
    assert time.time() - t0 < 0.05


def test_nothing_on_its_way_waits_out_the_start_grace_not_the_deadline():
    t0 = time.time()
    assert not _wait_for_tick_telemetry(_Analyzer(in_flight=False), _Ammo(), t0, 0.75,
                                        start_grace_s=0.1)
    assert 0.09 <= time.time() - t0 < 0.3


def test_a_reading_submitted_after_the_tree_arrives_is_waited_for():
    """Live 04:31 (cycle 38): the tree reached the wait 1 to 2 ms after the pass
    was scheduled, before the pass had submitted the reading, and gave up."""
    a = _Analyzer(in_flight=False)

    def _submit():
        a.in_flight = True

    t0 = time.time()
    threading.Timer(0.05, _submit).start()
    threading.Timer(0.3, a.land).start()
    assert _wait_for_tick_telemetry(a, _Ammo(), t0, 0.75)
    assert 0.28 <= time.time() - t0 < 0.5


def test_it_returns_as_soon_as_the_reading_lands():
    a = _Analyzer()
    t0 = time.time()
    threading.Timer(0.1, a.land).start()
    assert _wait_for_tick_telemetry(a, _Ammo(), t0, 0.75)
    assert 0.08 <= time.time() - t0 < 0.3


def test_it_gives_up_at_the_deadline():
    t0 = time.time()
    assert not _wait_for_tick_telemetry(_Analyzer(), _Ammo(), t0, 0.15)
    assert 0.14 <= time.time() - t0 < 0.3


def test_the_deadline_counts_from_the_tick_start():
    """A tick already 0.7 s in with a 0.75 s budget waits about 0.05 s, not 0.75."""
    now = time.time()
    t0 = time.time()
    assert not _wait_for_tick_telemetry(_Analyzer(), _Ammo(), now - 0.7, 0.75)
    assert time.time() - t0 < 0.15


def test_a_reading_landing_between_the_checks_counts():
    class _Racy(_Analyzer):
        def __init__(self):
            super().__init__(in_flight=False)
            self.asked = 0

        def telemetry_landed_since(self, since_ts):
            self.asked += 1
            return self.asked > 1   # lands after the first look

    assert _wait_for_tick_telemetry(_Racy(), _Ammo(), time.time(), 0.75)


def test_flares_still_go_out_while_it_waits():
    a = _Analyzer()
    ammo = _Ammo()

    def _alert_then_land():
        a.incoming_event.set()
        a.telemetry_event.set()
        time.sleep(0.05)
        a.land()

    threading.Timer(0.05, _alert_then_land).start()
    assert _wait_for_tick_telemetry(a, ammo, time.time(), 0.75)
    assert ammo.calls >= 1
    assert not a.incoming_event.is_set()


def test_the_shipped_config_waits_inside_the_tick():
    cfg = yaml.safe_load((Path(__file__).resolve().parents[1]
                          / "wingman" / "config.yaml").read_text())
    wait_s = cfg["telemetry"]["bt_wait_s"]
    assert 0.5 <= wait_s < cfg["loop_interval_sec"]

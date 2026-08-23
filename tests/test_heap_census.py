"""Tests for HeapCensus (Performance 008 Python-vs-native discriminator).

Like the ResourceSampler, this is a diagnostic: correctness matters, but never
breaking the main loop matters more. These cover the throttle, the off-by-
default contract, the never-raises guarantee, and — the point of the whole
module — that a planted retention actually shows up in the tables.
"""

import numpy as np
import pytest

from wingman.heap_census import HeapCensus, _sizeof


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _census(**cfg):
    clock = _Clock()
    cfg.setdefault("enabled", True)
    cfg.setdefault("interval_s", 100.0)
    cfg.setdefault("tracemalloc", False)  # off unless a test needs it
    return HeapCensus(cfg, clock=clock), clock


def _gc_census(**cfg):
    """The opt-in gc lane. Costs 2-4s on a real heap, hence not the default."""
    cfg.setdefault("gc_census", True)
    return _census(**cfg)


def test_disabled_by_default():
    """It walks the entire heap — it must never switch itself on."""
    c = HeapCensus({})
    assert not c.enabled
    assert c.maybe_census() is None


def test_throttled_to_the_interval():
    c, clock = _census(interval_s=100.0)
    assert c.maybe_census() is not None      # first call always samples
    clock.advance(50)
    assert c.maybe_census() is None
    clock.advance(51)
    assert c.maybe_census() is not None


def test_emits_the_greppable_header():
    c, _ = _census()
    line = c.maybe_census(mi_use_mb=3639).splitlines()[0]
    assert line.startswith("HEAPCENSUS ")
    for field in ("elapsed=", "py_mb=", "objects=", "mi_use_mb=3639", "census_ms="):
        assert field in line, f"missing {field!r} in {line!r}"


def test_first_census_reports_no_deltas():
    c, _ = _census()
    assert "d_py=n/a" in c.maybe_census().splitlines()[0]


def test_gc_lane_is_off_by_default():
    """It stalls the tick for seconds; it must never switch itself on."""
    c, _ = _census()
    line = c.maybe_census().splitlines()[0]
    assert "py_mb=off" in line and "objects=off" in line


def test_second_census_reports_deltas():
    c, clock = _gc_census()
    c.maybe_census()
    clock.advance(200)
    assert "d_py=n/a" not in c.maybe_census().splitlines()[0]


def test_planted_retention_appears_in_the_by_type_table():
    """The whole point: a growing container must be visible and attributed."""
    c, clock = _gc_census()
    c.maybe_census()

    # 64 MB of ndarray, retained — the shape the leak is suspected to have.
    held = [np.zeros((1024, 1024), dtype=np.uint8) for _ in range(64)]

    clock.advance(200)
    block = c.maybe_census()
    rows = [l for l in block.splitlines() if "ndarray" in l]
    assert rows, f"ndarray absent from by-type table:\n{block}"
    assert "+6" in rows[0] or "+5" in rows[0], \
        f"64MB of retained ndarray not reflected as a delta: {rows[0]}"
    assert len(held) == 64  # keep the reference alive until after the census


def test_nbytes_types_are_sized_by_payload_not_header():
    """sys.getsizeof would report ~112 bytes for this and hide the leak."""
    arr = np.zeros((1024, 1024), dtype=np.uint8)
    assert _sizeof(arr) >= 1024 * 1024


def test_sizeof_never_raises_on_a_hostile_object():
    class Hostile:
        @property
        def nbytes(self):
            raise RuntimeError("boom")

        def __sizeof__(self):
            raise RuntimeError("boom")

    assert _sizeof(Hostile()) == 0


def test_census_never_raises_into_the_main_loop(monkeypatch):
    c, _ = _gc_census()
    monkeypatch.setattr("wingman.heap_census.gc.get_objects",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert c.maybe_census() is None  # swallowed, not propagated


def test_tracemalloc_lane_reports_sites_and_stops_cleanly():
    import tracemalloc
    was_tracing = tracemalloc.is_tracing()
    c, clock = _census(tracemalloc=True)
    try:
        first = c.maybe_census()
        assert "first census" in first

        held = [bytearray(1024 * 1024) for _ in range(32)]
        clock.advance(200)
        block = c.maybe_census()
        assert "by-site" in block
        assert "tm_mb=" in block.splitlines()[0]
        assert len(held) == 32
    finally:
        c.stop()
    assert tracemalloc.is_tracing() == was_tracing, "must restore tracing state"


def test_stop_leaves_foreign_tracing_alone():
    """If something else started tracemalloc, stopping must not kill it."""
    import tracemalloc
    tracemalloc.start(1)
    try:
        c, _ = _census(tracemalloc=True)
        c.maybe_census()
        c.stop()
        assert tracemalloc.is_tracing(), "stopped tracing it did not start"
    finally:
        if tracemalloc.is_tracing():
            tracemalloc.stop()


def test_tracemalloc_only_lane_still_finds_a_planted_leak():
    """The default lane must answer the question on its own.

    gc_census is off by default because the walk stalls the tick for seconds.
    That is only acceptable because tracemalloc sees the same retention — numpy
    registers its data allocations with it — and additionally names the line.
    If this ever stops holding, the default configuration is diagnosing nothing.
    """
    import numpy as np
    c, clock = _census(tracemalloc=True, gc_census=False)
    try:
        c.maybe_census()
        held = [np.zeros((1200, 1920, 3), dtype=np.uint8) for _ in range(8)]  # ~55MB
        clock.advance(200)
        block = c.maybe_census()

        header = block.splitlines()[0]
        assert "py_mb=off" in header, "gc lane should be off for this test"
        growth = float(header.split("d_tm=")[1].split()[0])
        assert growth > 40, f"55MB of retained frames not seen by tracemalloc: {header}"
        assert "by-site" in block, "the by-site table is the actionable output"
        assert len(held) == 8
    finally:
        c.stop()


def test_tracemalloc_overhead_is_reported():
    """The instrument's own footprint must be subtractable from the measurement."""
    c, _ = _census(tracemalloc=True)
    try:
        header = c.maybe_census().splitlines()[0]
        assert "tm_overhead_mb=" in header
        assert "tm_overhead_mb=n/a" not in header
    finally:
        c.stop()


def test_census_disarms_itself_when_it_gets_too_slow():
    """take_snapshot() cost grows with accumulated traces — 13 ms at session
    start, 5,515 ms at 95 minutes on the 2026-08-23 run, past the 1.5 s tick.
    A diagnostic must not degrade the thing it is measuring."""
    c, clock = _census(max_census_ms=0.0001)   # anything real exceeds this
    assert c.enabled
    assert c.maybe_census() is not None        # the crossing census still reports
    assert not c.enabled, "must disarm after exceeding max_census_ms"
    clock.advance(10_000)
    assert c.maybe_census() is None, "must stay disarmed"


def test_guard_can_be_disabled_with_zero():
    c, clock = _census(max_census_ms=0)
    c.maybe_census()
    assert c.enabled, "max_census_ms=0 disables the guard"
    clock.advance(200)
    assert c.maybe_census() is not None

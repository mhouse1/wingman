"""Host conditions in the session record (ADR 095, reduced scope).

ADR 092's leak gate and the regression baseline both judge a session against an
archive of prior ones, and today assume conditions were equivalent. These fields
make that assumption checkable.

Scope note: this records, it does not alarm. Three sessions on 2026-08-26 — one
under TRIAL, two with the lab services up — were indistinguishable, so no load
threshold is invented. See the ADR.
"""

import json
import logging
import time
from pathlib import Path

from wingman.performance import PerformanceTracker, _load_run_file
from wingman.resource_monitor import ResourceSampler, read_loadavg

logging.disable(logging.CRITICAL)


def _tracker(tmp):
    return PerformanceTracker(
        {"performance": {"round_histogram": {"enabled": True, "output_dir": str(tmp)}}},
        version="test")


def _write(tmp, **host):
    t = _tracker(tmp)
    if host:
        t.set_host_context(**host)
    t._write_run_file(crops={"incoming": [0.4]}, reaction=[0.25], rounds=1,
                      end_ts=time.time())
    return json.loads(next(Path(tmp, "current").glob("run_*.json")).read_text())


# --- V1: the RESOURCE line ---------------------------------------------------

def test_loadavg_is_readable():
    la = read_loadavg()
    assert la is None or (len(la) == 3 and all(v >= 0 for v in la))


def test_resource_line_carries_load():
    line = ResourceSampler({"enabled": True, "interval_s": 0.0}).maybe_sample()
    assert line and "load=" in line, line


def test_resource_line_still_parses_for_the_leak_gate():
    """ADR 092's field regex reads the line; a new field must not break it."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("lc", "scripts/leak-check.py")
    lc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lc)
    line = ResourceSampler({"enabled": True, "interval_s": 0.0}).maybe_sample()
    fields = dict(lc._FIELD.findall(line.split("RESOURCE ")[1]))
    assert "elapsed" in fields and "rss_mb" in fields


# --- V2/V3: the run-file block ----------------------------------------------

def test_host_block_reaches_the_run_file(tmp_path):
    j = _write(tmp_path, hostname="veda", cpu_count=20, rd_mode="trial",
               load_start=[9.4, 8.0, 7.1])
    h = j["host"]
    assert h["hostname"] == "veda" and h["cpu_count"] == 20
    assert h["rd_mode"] == "trial"
    assert h["load_start"] == [9.4, 8.0, 7.1]


def test_unknown_mode_is_recorded_as_unknown(tmp_path):
    """foundry HLDD 001: `unknown` must never be collapsed to `trial`."""
    j = _write(tmp_path, rd_mode="unknown")
    assert j["host"]["rd_mode"] == "unknown"


def test_context_merges_across_calls(tmp_path):
    """Start and end are recorded at different times."""
    t = _tracker(tmp_path)
    t.set_host_context(rd_mode="rd", load_start=[1.0, 1.0, 1.0])
    t.set_host_context(load_end=[2.0, 2.0, 2.0])
    t._write_run_file(crops={"incoming": [0.4]}, reaction=[], rounds=1, end_ts=time.time())
    h = json.loads(next(Path(tmp_path, "current").glob("run_*.json")).read_text())["host"]
    assert h["rd_mode"] == "rd" and h["load_start"] and h["load_end"]


def test_none_values_are_not_recorded(tmp_path):
    """An unavailable probe must leave the field absent, not write a null that
    reads as a measurement."""
    j = _write(tmp_path, rd_mode=None, load_start=None, hostname="veda")
    assert "rd_mode" not in j["host"] and "load_start" not in j["host"]


def test_absent_context_writes_null_not_a_stub(tmp_path):
    j = _write(tmp_path)
    assert j["host"] is None


def test_set_host_context_never_raises(tmp_path):
    t = _tracker(tmp_path)
    t.set_host_context(**{"weird": object()})     # must not raise


def test_pre_adr095_files_still_load(tmp_path):
    """ADR 092 reads the whole archive; 725 sessions predate this field."""
    j = _write(tmp_path, rd_mode="rd")
    j.pop("host")
    p = tmp_path / "run_old.json"
    p.write_text(json.dumps(j))
    assert _load_run_file(p) is not None


# --- V4: it records, it does not alarm --------------------------------------

def test_nothing_warns_about_load():
    """The alerting half was dropped: no measured threshold affects results, so
    inventing one would be a number with no evidence behind it (ADR 095)."""
    src = Path("wingman/main.py").read_text()
    lowered = src.lower()
    for phrase in ("consider trial", "load is high", "machine is loaded"):
        assert phrase not in lowered, f"startup load warning reintroduced: {phrase!r}"


# --- ordering: load_end must beat the run-file write ------------------------

def test_load_end_is_captured_before_analyzer_cleanup():
    """The run file is written from inside analyzer.cleanup() -> on_session_end().
    A load_end recorded after that call is silently dropped: the file writes
    fine, just without the field. That is what happened on 2026-08-26 14:37,
    which produced a session record with load_start and no load_end."""
    lines = Path("wingman/main.py").read_text().splitlines()
    def find(needle):
        # Code only: prose about analyzer.cleanup() appears in nearby comments.
        return [i for i, l in enumerate(lines)
                if needle in l and not l.strip().startswith("#")]
    end, cleanup = find("load_end="), find("analyzer.cleanup()")
    assert end and cleanup, (end, cleanup)
    assert max(end) < min(cleanup), "load_end is captured after the run file is written"


def test_on_session_end_is_what_writes_the_run_file():
    """Pins the coupling the test above depends on. If the write moves out of
    analyzer.cleanup(), the ordering assertion above is testing nothing."""
    assert "self._tracker.on_session_end()" in Path("wingman/analyzer.py").read_text()
    perf = Path("wingman/performance.py").read_text()
    body = perf[perf.index("def on_session_end"):]
    assert "_write_run_file" in body[:body.index("\n    def ", 1)]

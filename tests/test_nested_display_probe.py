"""The nested-display probe must be bounded. ADR 119.

Measured 2026-09-05 07:09: `make rd` hung with no output whatsoever. The cause
was `display_is_up`, which called `Xlib.display.Display(":3")` with no timeout
against an Xwayland that accepted the connection and never completed the
handshake. The server was alive (pid 324709, the game still running on it) and
simply did not answer, so the probe never returned and neither did make.
"""

import importlib.util
import sys
import threading
import time
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "nested_display",
    Path(__file__).resolve().parent.parent / "scripts" / "nested-display.py")
nested_display = importlib.util.module_from_spec(_spec)
sys.modules["nested_display"] = nested_display
_spec.loader.exec_module(nested_display)


def test_a_wedged_display_returns_rather_than_hanging(monkeypatch):
    """The live failure. A connect that never returns must not block the
    caller — the whole harness stalls behind it."""
    started = threading.Event()

    class _Hang:
        def __init__(self, name):
            started.set()
            time.sleep(30)          # never completes, like the real handshake

    monkeypatch.setattr("Xlib.display.Display", _Hang)
    t0 = time.monotonic()
    state = nested_display.probe_display(":99", timeout_s=0.5)
    elapsed = time.monotonic() - t0
    assert state == "wedged"
    assert elapsed < 5.0, f"probe took {elapsed:.1f}s — it is still unbounded"
    assert started.is_set(), "the connect never actually ran"


def test_a_live_display_reads_as_up(monkeypatch):
    class _Ok:
        def __init__(self, name): pass
        def close(self): pass

    monkeypatch.setattr("Xlib.display.Display", _Ok)
    assert nested_display.probe_display(":99", timeout_s=2.0) == "up"
    assert nested_display.display_is_up(":99") is True


def test_an_absent_display_reads_as_down(monkeypatch):
    class _Refused:
        def __init__(self, name):
            raise ConnectionRefusedError("no server")

    monkeypatch.setattr("Xlib.display.Display", _Refused)
    assert nested_display.probe_display(":99", timeout_s=2.0) == "down"
    assert nested_display.display_is_up(":99") is False


def test_a_wedged_display_is_not_reported_as_up(monkeypatch):
    """`display_is_up` is the decision "can I use this display". A wedged
    server cannot be used, and calling it usable is what caused the hang."""
    class _Hang:
        def __init__(self, name): time.sleep(30)

    monkeypatch.setattr("Xlib.display.Display", _Hang)
    assert nested_display.display_is_up(":99") is False

"""EasyOCR readers are thread-local and expensive; they must not be per-call.

Each reader holds ~300 MB of model weights. On 2026-08-22 the GAME_STARTING
health probe ran on a fresh `threading.Thread` per probe, so 1,138 probes
produced 1,213 reader initialisations against a single 13-worker pool — roughly
350 GB of allocate/free churn in one session, and a prime suspect for the
Performance 008 heap growth.

Usage: uv run pytest tests/test_ocr_reader_reuse.py -q
"""

import re
from pathlib import Path

SRC = Path("wingman/analyzer.py").read_text(encoding="utf-8")


def test_health_probe_runs_on_the_pool_not_a_new_thread():
    """The probe already fetches `executor` as a guard — it must also use it."""
    block = SRC[SRC.index("def _schedule_starting_health_probe"):]
    block = block[:block.index("def arm_starting_health_scan")]
    assert "executor.submit(_probe)" in block, \
        "health probe no longer submits to the OCR pool"
    assert "threading.Thread(target=_probe" not in block, \
        "health probe spawns a per-probe thread; each one loads a ~300 MB reader"


def test_reader_init_is_budgeted_and_warns():
    """A recurrence of the bug class must announce itself rather than hide."""
    assert "_OCR_READER_INIT_BUDGET" in SRC
    block = SRC[SRC.index("def _get_thread_ocr_reader"):]
    block = block[:block.index("def _process_respawn_region")]
    assert "logger.warning" in block, "no tripwire on excessive reader inits"


def test_reader_init_is_not_logged_at_info():
    """It fired 1,213 times at INFO and was read as noise for weeks."""
    block = SRC[SRC.index("def _get_thread_ocr_reader"):]
    block = block[:block.index("def _process_respawn_region")]
    m = re.search(r'logger\.(\w+)\(\s*"OCR thread %d: initialized', block)
    assert m, "the reader-init log line moved or was removed"
    assert m.group(1) == "debug", f"reader init logs at {m.group(1)}, expected debug"


# ---------------------------------------------------------------------------
# Behavioural: the probe must reach the pool, not a new thread
# ---------------------------------------------------------------------------

import copy
import threading

import numpy as np
import pytest
import yaml

from wingman.analyzer import GameStateAnalyzer
from constants import CONFIG_PATH


class _RecordingExecutor:
    """Stands in for the OCR pool and records what is handed to it."""

    def __init__(self):
        self.submitted = []

    def submit(self, fn, *a, **kw):
        self.submitted.append(fn)

        class _F:
            def result(self, timeout=None):
                return None
        return _F()


@pytest.fixture
def analyzer():
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    a = GameStateAnalyzer(copy.deepcopy(cfg))
    try:
        yield a
    finally:
        a.cleanup()


def test_probe_is_submitted_to_the_pool_and_spawns_no_thread(analyzer, monkeypatch):
    """The regression that mattered: a per-probe thread loads a ~300 MB reader.

    Asserts behaviour, not source text — the probe must hand work to the
    executor and must not create a thread of its own.
    """
    rec = _RecordingExecutor()
    monkeypatch.setattr(type(analyzer), "ocr_executor",
                        property(lambda self: rec), raising=False)

    spawned = []
    real_thread = threading.Thread

    def _spy(*a, **kw):
        spawned.append(kw.get("name") or "unnamed")
        return real_thread(*a, **kw)

    monkeypatch.setattr(threading, "Thread", _spy)

    analyzer._starting_probe_last_ts = 0.0
    analyzer._starting_probe_running = False
    frame = np.zeros((1200, 1920, 3), dtype=np.uint8)
    analyzer._schedule_starting_health_probe(frame)

    assert len(rec.submitted) == 1, "probe did not reach the OCR pool"
    assert not any("health-probe" in n for n in spawned), \
        f"probe spawned its own thread: {spawned}"

"""Host-mode reporting (foundry HLDD 001).

wingman announces which mode the host is in at startup. TRIAL is the one that
matters: it latches, nothing restores it, and the indicator is the entire safety
net — so a session can silently run with the lab services down.

The contract rules under test come from foundry HLDD 001's "Interface for other
systems", which names wingman as a consumer.
"""

import json
import subprocess
from pathlib import Path

import pytest

from wingman import host_mode

BASE = {
    "schema": 1, "host": "veda", "mode": "rd",
    "stacks": [{"label": "jenkins", "state": "up"}],
    "transam_active": False, "docker": "running", "docker_reachable": True,
    "swap_used_mb": 0, "since": "2026-08-26T07:40:27-04:00",
}


def _fake_run(payload, returncode=0):
    def run(_cmd, **_kw):
        return subprocess.CompletedProcess(_cmd, returncode, stdout=payload, stderr="")
    return run


@pytest.fixture
def rd_mode(monkeypatch):
    monkeypatch.setattr(host_mode.shutil, "which", lambda _c: "/usr/bin/rd-mode")

    def install(status=None, raw=None, returncode=0):
        payload = raw if raw is not None else json.dumps(status)
        monkeypatch.setattr(host_mode.subprocess, "run", _fake_run(payload, returncode))
    return install


def _mode(**over):
    return {**BASE, **over}


# --- the banner -------------------------------------------------------------

def test_trial_produces_a_banner(rd_mode):
    rd_mode(_mode(mode="trial", stacks=[{"label": "jenkins", "state": "down"}]))
    lines = host_mode.banner(host_mode.query())
    assert lines, "TRIAL must produce a banner"
    assert any("TRIAL" in ln for ln in lines)
    assert any("jenkins" in ln for ln in lines), "the banner should name what is down"
    assert any("latches" in ln for ln in lines), "the latching warning is the point"


@pytest.mark.parametrize("mode", ["rd", "mixed", "none", "unknown"])
def test_only_trial_gets_a_banner(rd_mode, mode):
    rd_mode(_mode(mode=mode))
    assert host_mode.banner(host_mode.query()) == []


def test_banner_lines_are_uniform_width(rd_mode):
    rd_mode(_mode(mode="trial"))
    widths = {len(ln) for ln in host_mode.banner(host_mode.query())}
    assert len(widths) == 1, f"ragged banner: {sorted(widths)}"


# --- contract rules from HLDD 001 -------------------------------------------

def test_unknown_is_not_trial(rd_mode, caplog):
    """The rule the field exists to enforce: a caller outside the docker group
    sees every stack down, and must NOT conclude TRIAL."""
    rd_mode(_mode(mode="unknown", docker_reachable=False,
                  stacks=[{"label": "jenkins", "state": "unknown"}]))
    with caplog.at_level("WARNING"):
        assert host_mode.log_host_mode() == "unknown"
    assert host_mode.banner(host_mode.query()) == []
    assert any("Not assuming" in r.getMessage() for r in caplog.records)
    assert any("docker group" in r.getMessage() for r in caplog.records)


def test_unrecognised_schema_is_refused_politely(rd_mode, caplog):
    rd_mode(_mode(schema=99, mode="trial"))
    with caplog.at_level("WARNING"):
        assert host_mode.query() is None
        assert host_mode.log_host_mode() is None
    assert any("schema" in r.getMessage() for r in caplog.records)


def test_nonzero_exit_is_not_an_error_if_json_was_produced(rd_mode):
    """HLDD 001: exit status is 0 whenever JSON was produced — read `mode`."""
    rd_mode(_mode(mode="trial"), returncode=3)
    assert host_mode.query()["mode"] == "trial"


def test_all_five_modes_are_handled(rd_mode):
    for mode in ("rd", "trial", "mixed", "none", "unknown"):
        rd_mode(_mode(mode=mode))
        assert host_mode.log_host_mode() == mode, f"{mode} not handled"


def test_transam_is_reported(rd_mode, caplog):
    rd_mode(_mode(mode="rd", transam_active=True))
    with caplog.at_level("WARNING"):
        host_mode.log_host_mode()
    assert any("TRANSAM" in r.getMessage() for r in caplog.records)


# --- it must never break startup -------------------------------------------

def test_absent_tool_is_silent_and_not_trial(monkeypatch):
    monkeypatch.setattr(host_mode.shutil, "which", lambda _c: None)
    assert host_mode.query() is None
    assert host_mode.log_host_mode() is None


def test_timeout_does_not_raise(rd_mode, monkeypatch):
    monkeypatch.setattr(host_mode.shutil, "which", lambda _c: "/usr/bin/rd-mode")

    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="rd-mode", timeout=5)
    monkeypatch.setattr(host_mode.subprocess, "run", boom)
    assert host_mode.query() is None
    assert host_mode.log_host_mode() is None


def test_garbage_output_does_not_raise(rd_mode):
    rd_mode(raw="not json at all")
    assert host_mode.query() is None
    assert host_mode.log_host_mode() is None


def test_json_that_is_not_an_object_does_not_raise(rd_mode):
    rd_mode(raw="[1, 2, 3]")
    assert host_mode.query() is None


def test_the_state_file_is_never_read():
    """HLDD 001 is explicit: the state file is not an API and can be stale."""
    src = Path(host_mode.__file__).read_text()
    assert "rd-mode.state" not in src.replace("``rd-mode.state``", ""), \
        "mode must be derived from rd-mode status, never the state file"

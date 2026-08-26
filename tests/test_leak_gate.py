"""ADR 092 Design 2 — the leak gate.

Performance 008's leak ran two months undetected because nothing watched. These
pin the behaviour that makes the gate worth having: that it FAILS on the real
pre-fix sessions, PASSES on the real post-fix ones, and — the part that is easy
to get wrong — refuses to conclude rather than passing when the data cannot
support a verdict.

Corpus assertions run against archived logs. Those are gitignored and
machine-local, so they SKIP when absent; the synthetic fixtures carry the
deterministic cases.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location("leak_check", Path("scripts/leak-check.py"))
leak_check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(leak_check)

PASS, FAIL, INSUFFICIENT = leak_check.PASS, leak_check.FAIL, leak_check.INSUFFICIENT
CFG = dict(leak_check._DEFAULTS)
LOGS = Path("logs")


def _write(tmp_path, name, samples, fields=("rss_mb", "mi_use_mb", "game_rss_mb")):
    """Synthesise a log with RESOURCE lines. samples = [(elapsed, {field: val})]."""
    lines = []
    for elapsed, vals in samples:
        parts = [f"elapsed={elapsed}"] + [
            f"{f}={vals.get(f, 'n/a')}" for f in fields] + [f"n_ocr={vals.get('n_ocr', 800)}"]
        lines.append(f"2026-08-25 00:00:00,000 [INFO] RESOURCE {' '.join(parts)}\n")
    p = tmp_path / name
    p.write_text("".join(lines), encoding="utf-8")
    return p


def _ramp(hours, start, rate, step_s=300, **extra):
    """A linear series: `rate` MB/h from `start`, sampled every step_s."""
    out = []
    for i in range(int(hours * 3600 / step_s) + 1):
        el = i * step_s
        v = start + rate * (el / 3600.0)
        out.append((el, {"mi_use_mb": round(v), "rss_mb": round(v + 900),
                         "game_rss_mb": round(1120 + 165 * el / 3600.0), **extra}))
    return out


# --- outcome logic ----------------------------------------------------------

def test_flat_session_passes(tmp_path):
    p = _write(tmp_path, "wingman_a.log", _ramp(3, 1440, 2))
    assert leak_check.measure(p, CFG)["verdict"] == PASS


def test_leaking_session_fails(tmp_path):
    p = _write(tmp_path, "wingman_a.log", _ramp(3, 1440, 950))
    m = leak_check.measure(p, CFG)
    assert m["verdict"] == FAIL and m["severity"] == "clear"


def test_borderline_leak_fails_but_is_labelled(tmp_path):
    p = _write(tmp_path, "wingman_a.log", _ramp(3, 1440, 200))
    m = leak_check.measure(p, CFG)
    assert m["verdict"] == FAIL and m["severity"] == "borderline"


def test_short_session_does_not_qualify(tmp_path):
    """The trap: a 40-minute run must not report a pass."""
    p = _write(tmp_path, "wingman_a.log", _ramp(0.67, 1440, 950))
    m = leak_check.measure(p, CFG)
    assert not m["qualifies"] and "underread" in m["reason"]


def test_warmup_is_excluded_from_the_rate(tmp_path):
    """Wingman allocates >1GB loading OCR readers in the first five minutes.
    Counting it manufactures a leak on a session that has none."""
    samples = [(0, {"mi_use_mb": 140, "rss_mb": 683, "game_rss_mb": 1120})]
    samples += _ramp(3, 1440, 2)[1:]
    p = _write(tmp_path, "wingman_a.log", samples)
    m = leak_check.measure(p, CFG)
    assert m["verdict"] == PASS, f"warm-up leaked into the rate: {m['rate']:+.0f} MB/h"


def test_game_growth_never_fails_the_gate(tmp_path):
    """MetalStorm leaks ~165 MB/h on its own (Anomaly 002). Not ours."""
    samples = _ramp(3, 1440, 2)
    for _el, v in samples:
        v["game_rss_mb"] = int(v["game_rss_mb"]) + 2000    # a violently leaking game
    p = _write(tmp_path, "wingman_a.log", samples)
    m = leak_check.measure(p, CFG)
    assert m["verdict"] == PASS
    assert m["game"] is not None, "game growth must still be reported"


def test_mi_use_is_preferred_over_rss(tmp_path):
    """RSS includes arena retention that is not a leak — gating on it would
    have failed a clean post-fix build reading +109 MB/h."""
    samples = _ramp(3, 1440, 2)
    for _el, v in samples:
        v["rss_mb"] = 2500 + 300 * (_el / 3600.0)          # rss climbing, live flat
    p = _write(tmp_path, "wingman_a.log", samples)
    m = leak_check.measure(p, CFG)
    assert m["signal"] == "mi_use" and m["confidence"] == "high"
    assert m["verdict"] == PASS


def test_rss_fallback_when_mi_use_absent(tmp_path):
    """Logs predating the mallinfo2 instrumentation still get a verdict."""
    samples = _ramp(3, 1440, 2)
    for _el, v in samples:
        v.pop("mi_use_mb")
    p = _write(tmp_path, "wingman_a.log", samples)
    m = leak_check.measure(p, CFG)
    assert m["signal"] == "rss" and m["confidence"] == "low"


def test_inert_session_is_refused(tmp_path):
    """Anomaly 001: a livelocked session shows no growth because it does no
    work. That is not evidence of health."""
    p = _write(tmp_path, "wingman_a.log", _ramp(3, 1440, 2, n_ocr=0))
    m = leak_check.measure(p, CFG)
    assert not m["qualifies"] and "inert" in m["reason"]


def test_malformed_lines_are_ignored(tmp_path):
    p = tmp_path / "wingman_a.log"
    good = "".join(
        f"[INFO] RESOURCE elapsed={e} mi_use_mb={v['mi_use_mb']} rss_mb=2500 "
        f"game_rss_mb=1200 n_ocr=800\n" for e, v in _ramp(3, 1440, 2))
    p.write_text("garbage\nRESOURCE elapsed=notanumber mi_use_mb=x\n" + good, encoding="utf-8")
    assert leak_check.measure(p, CFG)["verdict"] == PASS


def test_empty_and_unreadable_logs_never_raise(tmp_path):
    (tmp_path / "wingman_empty.log").write_text("", encoding="utf-8")
    assert leak_check.measure(tmp_path / "wingman_empty.log", CFG)["qualifies"] is False
    assert leak_check.parse_session(tmp_path / "nope.log") == []


def test_no_logs_reports_insufficient_not_pass(tmp_path):
    assert leak_check.main(["--log-dir", str(tmp_path)]) == INSUFFICIENT


# --- corpus assertions ------------------------------------------------------

def _corpus(name):
    p = LOGS / name
    if not p.exists():
        pytest.skip(f"{p} not present (logs are gitignored and machine-local)")
    return p


@pytest.mark.parametrize("name,expected", [
    ("wingman_20260823_002829.log", FAIL),          # pre-fix, 6.77h, +1491 MB/h
    ("wingman_20260823_065033.log", FAIL),          # pre-fix, 2.26h, +974
    ("wingman_20260823_230230.log", PASS),          # post-ADR091, 3.01h, +2
    ("wingman_20260824_091431.log", PASS),          # post-ADR091, 4.34h, -0
])
def test_corpus_verdicts(name, expected):
    """The gate must call the real sessions correctly, not just synthetic ones."""
    m = leak_check.measure(_corpus(name), CFG)
    assert m["qualifies"], m["reason"]
    assert m["verdict"] == expected, f"{name}: {m['rate']:+.0f} MB/h"


def test_corpus_short_session_is_insufficient():
    m = leak_check.measure(_corpus("wingman_20260821_083045.log"), CFG)
    assert not m["qualifies"]


def test_corpus_livelock_session_is_refused():
    """Anomaly 001 — half idle, so its flat rate proves nothing."""
    m = leak_check.measure(_corpus("wingman_20260824_033917.log"), CFG)
    assert not m["qualifies"] and "inert" in m["reason"]


# --- the CLI contract -------------------------------------------------------

def test_cli_exit_codes(tmp_path):
    """make tp and make wrelease branch on these."""
    src = _corpus("wingman_20260823_002829.log")
    (tmp_path / "wingman_leak.log").write_bytes(src.read_bytes())
    r = subprocess.run([sys.executable, "scripts/leak-check.py", "--log-dir", str(tmp_path)],
                       capture_output=True, text=True)
    assert r.returncode == FAIL, r.stdout
    assert "FAIL" in r.stdout


# --- the wrelease release-gate branch ---------------------------------------
#
# ADR 092 gives wrelease different exit-code handling from tp: FAIL blocks, and
# so does INSUFFICIENT, because shipping a build whose memory behaviour was
# never measured is how the last leak shipped. That branching lives in Makefile
# recipe shell, so it is otherwise only exercised during an actual release —
# which is the worst moment to discover it is wrong.
#
# These extract the block VERBATIM from the real recipe and run it, so a copy
# here cannot drift from what wrelease actually does.

def _extract_wrelease_gate():
    lines = Path("Makefile").read_text().splitlines(keepends=True)
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("wrelease:"))
    except StopIteration:
        return None
    block = []
    for l in lines[start + 1:]:
        if not l.startswith("\t"):
            break
        block.append(l)
        if l.strip() == "fi":
            break
    gate = "".join(block)
    return gate if "leak-check.py" in gate else None


@pytest.fixture
def release_gate(tmp_path):
    gate = _extract_wrelease_gate()
    if gate is None:
        pytest.skip("wrelease leak-gate block not found in Makefile")
    mk = tmp_path / "Makefile"
    mk.write_text("SHELL := /bin/bash\nPYTHON_RUN := python3\nLEAK_ARGS ?=\n\n"
                  "release-gate:\n" + gate + '\t@echo "REACHED_RELEASE_BODY"\n',
                  encoding="utf-8")
    def run(log_dir):
        # Run from the repo root so scripts/leak-check.py resolves.
        return subprocess.run(
            ["make", "-f", str(mk), "release-gate", f"LEAK_ARGS=--log-dir {log_dir}"],
            capture_output=True, text=True)
    return run


def test_release_gate_proceeds_on_pass(release_gate, tmp_path):
    d = tmp_path / "pass"
    d.mkdir()
    _write(d, "wingman_ok.log", _ramp(3, 1440, 2))
    r = release_gate(d)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "REACHED_RELEASE_BODY" in r.stdout, "a passing gate must not block the release"


def test_release_gate_blocks_on_fail(release_gate, tmp_path):
    d = tmp_path / "fail"
    d.mkdir()
    _write(d, "wingman_leak.log", _ramp(3, 1440, 950))
    r = release_gate(d)
    assert r.returncode != 0
    assert "REACHED_RELEASE_BODY" not in r.stdout
    assert "leak gate failed" in r.stdout


def test_release_gate_blocks_on_insufficient(release_gate, tmp_path):
    """The distinguishing rule: tp warns here, wrelease refuses."""
    d = tmp_path / "insuf"
    d.mkdir()          # no logs at all
    r = release_gate(d)
    assert r.returncode != 0
    assert "REACHED_RELEASE_BODY" not in r.stdout
    assert "never been measured" in r.stdout


def test_release_gate_runs_before_anything_else():
    """Ordering matters: the gate must not run after work that is hard to undo."""
    lines = Path("Makefile").read_text().splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("wrelease:"))
    body = [l for l in lines[start + 1:start + 4] if l.startswith("\t")]
    assert any("leak-check.py" in l or "leak gate" in l for l in body), \
        "the leak gate must be the first step of wrelease"

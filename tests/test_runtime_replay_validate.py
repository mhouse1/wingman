"""ADR 044 replay validator: which eject flows count as complete.

Regression (2026-09-24): `make rr-path1-gate` failed at a clean HEAD with "required
log pattern missing: MISSILES EMPTY - cancelling mission and ejecting", "eject_and_dive
- descent control engaged" and "missing terminal eject outcome". Cause: since commit
eabaa28 (2026-09-23) `pursuit_mode.enabled` is true, so a missiles-empty screen starts
the pursuit (HLDD 015), which the replay's respawn screen stops 13 s later, before the
20 s cap would hand over to eject_and_dive. The validator only knew the old flow.

These tests pin both directions: either complete flow passes, and a flow that starts
but does not finish, or finishes without starting, still fails.
"""

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "runtime_replay_validate", Path(__file__).parent / "runtime_replay_validate.py")
V = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(V)

_GOOD_LUCK = "\n".join(V.POSITIVE_LOG_PATTERNS_REQUIRED)
_LEGACY = "\n".join(V.EJECT_FLOWS["eject_and_dive"]["required"])
_PURSUIT = "\n".join(V.EJECT_FLOWS["pursuit"]["required"])
_LEGACY_END = "Controller: eject_and_dive complete"
_PURSUIT_END = "Controller: pursue_and_engage — stopped externally (reason=respawn_detected)"


def _run(*parts):
    failures = []
    summary = V._validate_log("\n".join(parts), failures)
    return failures, summary


def test_the_legacy_eject_and_dive_flow_still_passes():
    failures, summary = _run(_GOOD_LUCK, _LEGACY, _LEGACY_END)
    assert failures == []
    assert summary["eject_flow_completed"] == ["eject_and_dive"]


def test_the_pursuit_flow_passes_when_a_respawn_stops_it():
    """The situation the replay is in with pursuit mode on."""
    failures, summary = _run(_GOOD_LUCK, _PURSUIT, _PURSUIT_END)
    assert failures == []
    assert summary["eject_flow_completed"] == ["pursuit"]


def test_a_pursuit_that_ran_to_its_cap_and_fell_through_to_a_complete_dive_passes():
    failures, summary = _run(_GOOD_LUCK, _PURSUIT, _LEGACY, _LEGACY_END)
    assert failures == []
    assert set(summary["eject_flow_completed"]) == {"eject_and_dive", "pursuit"}


def test_a_pursuit_without_its_tracking_start_marker_fails():
    failures, _ = _run(_GOOD_LUCK, V.EJECT_FLOWS["pursuit"]["required"][0], _PURSUIT_END)
    assert any("no complete eject flow" in f and "tracking engaged" in f for f in failures), failures


def test_a_pursuit_that_never_ends_fails():
    failures, _ = _run(_GOOD_LUCK, _PURSUIT)
    assert any("pursuit" in f and "no terminal marker" in f for f in failures), failures


def test_a_pursuit_stopped_for_an_unknown_reason_is_not_a_complete_flow():
    failures, _ = _run(
        _GOOD_LUCK, _PURSUIT,
        "Controller: pursue_and_engage — stopped externally (reason=manual_takeover)")
    assert any("no complete eject flow" in f for f in failures), failures


def test_neither_flow_present_fails_and_names_both():
    failures, _ = _run(_GOOD_LUCK)
    message = next(f for f in failures if "no complete eject flow" in f)
    assert "eject_and_dive" in message and "pursuit" in message


def test_the_flows_do_not_borrow_each_others_markers():
    """The legacy start marker plus a pursuit terminal is neither flow."""
    failures, _ = _run(_GOOD_LUCK, V.EJECT_FLOWS["eject_and_dive"]["required"][0], _PURSUIT_END)
    assert any("no complete eject flow" in f for f in failures), failures


def test_the_good_luck_markers_are_still_required():
    failures, _ = _run(_LEGACY, _LEGACY_END)
    assert any("Good Luck" in f for f in failures), failures


def test_forbidden_patterns_still_fail_a_complete_flow():
    failures, _ = _run(_GOOD_LUCK, _PURSUIT, _PURSUIT_END, "Traceback (most recent call last)")
    assert any("forbidden log pattern" in f and "Traceback" in f for f in failures), failures

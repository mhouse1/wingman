"""Validate ADR045 PATH1 live-screen runtime artifacts.

This script validates that Wingman ran in live capture mode (not replay)
and reached expected OCR/FSM action markers from desktop-presented frames.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

NEGATIVE_LOG_PATTERNS = [
    "Traceback",
    "[ERROR]",
    "Replay mode enabled",
    "Replay assertion failure",
    "Replay: injecting FSM trigger",
]

POSITIVE_LOG_PATTERNS_REQUIRED = [
    "Capture mode enabled",
    "Analyzer: 'Good Luck' detected in good_luck crop",
    "Controller: 'Good Luck' detected",
    "MISSILES EMPTY — cancelling mission and ejecting",
    "Controller: eject_and_dive — NOSE_DOWN + AFTERBURNER engaged",
]

TERMINAL_PATTERNS = [
    "Controller: eject_and_dive complete",
    "Controller: eject_and_dive — cancelled during nose-down phase",
]

MANUAL_TAKEOVER_PATTERNS = [
    "GAME_BATTLE → GAME_BATTLE_MANUAL",
    "FSM: entering GAME_BATTLE_MANUAL",
]


def _load_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def _count_patterns(text: str, patterns: list[str]) -> dict[str, int]:
    return {pattern: text.count(pattern) for pattern in patterns}


def _validate_capture_summary(payload: dict, failures: list[str]) -> dict:
    summary = payload.get("summary")
    if summary is None or not isinstance(summary, dict):
        failures.append("capture summary missing root key: summary")
        return {}

    if summary.get("complete") is not True:
        failures.append("capture summary indicates incomplete run")

    if summary.get("has_failures") is not False:
        failures.append("capture summary indicates failures")

    if summary.get("path_name") is None:
        failures.append("capture summary missing summary.path_name")

    steps = summary.get("steps")
    if not isinstance(steps, list):
        failures.append("capture summary missing summary.steps list")
        steps = []

    failed_steps = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            failures.append(f"capture summary step[{idx}] is not an object")
            continue
        if step.get("status") == "failed":
            failed_steps.append(idx)

    if failed_steps:
        failures.append(
            "capture summary has failed steps at indexes: "
            + ", ".join(str(i) for i in failed_steps)
        )

    return summary


def _validate_log(log_text: str, failures: list[str]) -> dict:
    negative_counts = _count_patterns(log_text, NEGATIVE_LOG_PATTERNS)
    required_positive_counts = _count_patterns(log_text, POSITIVE_LOG_PATTERNS_REQUIRED)
    terminal_counts = _count_patterns(log_text, TERMINAL_PATTERNS)
    manual_takeover_counts = _count_patterns(log_text, MANUAL_TAKEOVER_PATTERNS)
    manual_takeover_total = sum(manual_takeover_counts.values())

    for pattern, count in negative_counts.items():
        if count > 0:
            failures.append(f"forbidden log pattern present ({count}): {pattern}")

    for pattern, count in required_positive_counts.items():
        if count <= 0:
            failures.append(f"required log pattern missing: {pattern}")

    terminal_total = sum(terminal_counts.values())
    engaged_count = required_positive_counts[
        "Controller: eject_and_dive — NOSE_DOWN + AFTERBURNER engaged"
    ]
    if terminal_total <= 0 and engaged_count <= 0:
        failures.append("missing terminal eject outcome: neither complete nor cancelled marker found")

    # A respawn-triggered stop during the nose phase is a successful eject —
    # the ADR038 closed loop can extend the phase past the respawn detection.
    # Only cancellations with any other reason still require the manual
    # takeover marker to explain them.
    cancellation_count = terminal_counts["Controller: eject_and_dive — cancelled during nose-down phase"]
    respawn_cancel_count = log_text.count(
        "cancelled during nose-down phase (reason=respawn_detected)"
    )
    if cancellation_count - respawn_cancel_count > 0 and manual_takeover_total <= 0:
        failures.append(
            "cancellation observed but missing required manual takeover marker: "
            + " or ".join(MANUAL_TAKEOVER_PATTERNS)
        )

    return {
        "negative_counts": negative_counts,
        "required_positive_counts": required_positive_counts,
        "terminal_counts": terminal_counts,
        "manual_takeover_counts": manual_takeover_counts,
        "manual_takeover_total": manual_takeover_total,
    }


def main() -> int:
    # Windows consoles default to cp1252, which cannot encode the em-dash
    # and arrow characters in the log markers — a failed print must never
    # mask the validation verdict.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Validate ADR045 live runtime artifacts")
    parser.add_argument("--log-file", default="wingman_live.log", help="Path to runtime log file")
    parser.add_argument("--capture-summary", required=True, help="Path to capture summary JSON file")
    parser.add_argument(
        "--summary-out",
        default="tests/test-output/runtime_live_validation.path1.json",
        help="Path to write validation summary JSON",
    )
    args = parser.parse_args()

    failures: list[str] = []

    log_path = Path(args.log_file)
    capture_summary_path = Path(args.capture_summary)
    summary_path = Path(args.summary_out)

    if not log_path.exists():
        failures.append(f"log file not found: {log_path}")
        log_text = ""
    else:
        log_text = _load_text(log_path)

    capture_payload = {}
    if not capture_summary_path.exists():
        failures.append(f"capture summary not found: {capture_summary_path}")
    else:
        try:
            capture_payload = json.loads(capture_summary_path.read_text(encoding="utf-8"))
        except Exception as exc:
            failures.append(f"failed to parse capture summary {capture_summary_path}: {exc}")

    capture_summary = {}
    if capture_payload:
        capture_summary = _validate_capture_summary(capture_payload, failures)

    log_summary = _validate_log(log_text, failures)

    passed = len(failures) == 0
    summary = {
        "generated_at": time.time(),
        "passed": passed,
        "failure_count": len(failures),
        "failures": failures,
        "inputs": {
            "log_file": str(log_path),
            "capture_summary": str(capture_summary_path),
        },
        "capture": {
            "path_name": capture_summary.get("path_name"),
            "complete": capture_summary.get("complete"),
            "has_failures": capture_summary.get("has_failures"),
            "step_count": len(capture_summary.get("steps", []))
            if isinstance(capture_summary.get("steps"), list)
            else None,
            "not_updated_screenshots": capture_summary.get("not_updated_screenshots"),
        },
        "log_markers": log_summary,
    }

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if passed:
        print("PASS: live runtime validation succeeded")
        return 0

    print("FAIL: live runtime validation failed")
    for reason in failures:
        print(f" - {reason}")
    print(f"Summary: {summary_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())

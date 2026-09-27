#!/usr/bin/env python3
"""Aggregate HLDD 005's shadow-log evidence across sessions.

Both of Design 005's Phase 1 shadow logs are rate-limited (1st/10th/100th
occurrence, then every 500th — ADR 117 D9's pattern), so a plain line count
under-reports the true occurrence count past the 10th event: the counter in
each line's "(N so far)" suffix is the real, per-process cumulative total,
not the number of times a line was printed. This script reads that number
directly (the max seen per file, since it only increases) instead of
counting matches — a naive `grep -c` on any of these markers will silently
lie once a session passes ten occurrences.

Three shadow signals, one file each, all reset per process (a fresh
TargetTracker/TrackingHudHandler per run — no cross-session persistence, so
sessions cannot be summed by taking a max across files; the totals below are
SUMS of each file's own final count):

  SELECT[shadow]: red won, discarded ... selected ... (N so far)
      Selection Hardening Phase 1 (tracker.py) — logged whenever color
      priority discards a green candidate. Fires on the already-live ADR 136
      heatdive path regardless of tracking.enabled.

  SELECT[shadow]: old=... new=... would change (N so far)
      Selection Hardening Phase 2 — logged only when the ranked-pool
      alternative would have picked differently. A subset of the ticks
      above (it can only ever disagree on a tick where Phase 1 also had
      something to discard), so this is reported as a rate against Phase 1's
      total, not as an independent count.

  PITCH[shadow]: would nose_up/nose_down hold=...s err_y=... (N so far)
      Two-Axis Rollout Phase 1 (tick_handlers.py) — logged when the ambient
      path's vertical error is outside pitch_deadband, gated off separately
      by tracking.enabled (must be true; heatdive's own pitch stays excluded
      by design, so this never fires from that path).

Usage:
    make shadow-report                      # logs/*.log + wingman.log
    make shadow-report LOG=logs/foo.log      # one file
    scripts/shadow-report.py logs/*.log wingman.log
"""

from __future__ import annotations

import argparse
import glob
import re
import statistics
import sys

RATIONALE = re.compile(
    r"SELECT\[shadow\]: red won, discarded (\d+) green candidate\(s\).*?"
    r"selected (.+?) \((\d+) so far\)"
)
RANKED = re.compile(
    r"SELECT\[shadow\]: old=(.+?) new=(.+?) would change \((\d+) so far\)"
)
PITCH = re.compile(
    r"PITCH\[shadow\]: would (nose_up|nose_down) hold=([\d.]+)s "
    r"err_y=(-?[\d.]+) \((\d+) so far\)"
)

# Named guess, not from HLDD 005 itself (which deliberately left "often
# enough to matter" unquantified) — same spirit as turn-outcome.py's own
# "n < 60: too few turns to read" bar. Meant to be argued with, not trusted
# blindly; it exists so this script gives a verdict instead of just numbers.
MIN_OPPORTUNITIES_FOR_A_VERDICT = 20


def _first_last_timestamp(path: str) -> "tuple[str, str] | tuple[None, None]":
    first = last = None
    ts_re = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                m = ts_re.match(line)
                if m:
                    if first is None:
                        first = m.group(0)
                    last = m.group(0)
    except OSError:
        return None, None
    return first, last


def parse(path: str) -> dict:
    rationale_max = 0
    ranked_max = 0
    pitch_events: "list[tuple[str, float, float]]" = []  # (direction, hold, err_y)
    pitch_max = 0
    try:
        with open(path, errors="ignore") as fh:
            for line in fh:
                m = RATIONALE.search(line)
                if m:
                    rationale_max = max(rationale_max, int(m.group(3)))
                    continue
                m = RANKED.search(line)
                if m:
                    ranked_max = max(ranked_max, int(m.group(3)))
                    continue
                m = PITCH.search(line)
                if m:
                    pitch_max = max(pitch_max, int(m.group(4)))
                    pitch_events.append((m.group(1), float(m.group(2)), float(m.group(3))))
    except OSError as e:
        print(f"{path}: {e}", file=sys.stderr)
    return {
        "rationale_total": rationale_max,
        "ranked_total": ranked_max,
        "pitch_total": pitch_max,
        "pitch_events": pitch_events,
    }


def report(results: "list[tuple[str, dict, tuple]]") -> None:
    print("Per-session shadow-log evidence")
    print("=" * 72)
    grand_rationale = grand_ranked = grand_pitch = 0
    all_pitch_events: list = []
    for path, r, (first, last) in results:
        span = f"{first} .. {last}" if first else "no timestamped lines found"
        print(f"\n{path}")
        print(f"  session span              : {span}")
        print(f"  SELECT[shadow] rationale  : {r['rationale_total']:4d}  "
              f"(Phase 1 — color priority discarded a green candidate)")
        rate = (f"{100 * r['ranked_total'] / r['rationale_total']:.0f} pct of "
                f"rationale events" if r["rationale_total"] else "n/a — no rationale events")
        print(f"  SELECT[shadow] would-change: {r['ranked_total']:3d}  "
              f"(Phase 2 — ranked pool would have disagreed; {rate})")
        print(f"  PITCH[shadow]             : {r['pitch_total']:4d}")
        grand_rationale += r["rationale_total"]
        grand_ranked += r["ranked_total"]
        grand_pitch += r["pitch_total"]
        all_pitch_events.extend(r["pitch_events"])

    print("\n" + "=" * 72)
    print("TOTALS across all sessions read")
    print(f"  SELECT[shadow] rationale (Phase 1 opportunities) : {grand_rationale}")
    print(f"  SELECT[shadow] would-change (Phase 2 disagreements): {grand_ranked}")
    if grand_rationale:
        print(f"  disagreement rate                                : "
              f"{100 * grand_ranked / grand_rationale:.0f} pct")
    print(f"  PITCH[shadow] events                             : {grand_pitch}")

    if all_pitch_events:
        ups = sum(1 for d, _, _ in all_pitch_events if d == "nose_up")
        downs = len(all_pitch_events) - ups
        errs = [abs(e) for _, _, e in all_pitch_events]
        holds = [h for _, h, _ in all_pitch_events]
        # NOT a sample of all {grand_pitch} events — only the checkpoint
        # ticks that actually got printed (1st/10th/100th/500th, per
        # session), which is a small, systematically-chosen subset, not a
        # random one. Good enough for a sanity check on the formula; not
        # good enough to claim a representative direction/error distribution.
        print(f"\n  from {len(all_pitch_events)} logged checkpoint line(s) "
              f"(not all {grand_pitch} true events — see module docstring):")
        print(f"  pitch direction split      : {ups} nose_up / {downs} nose_down")
        print(f"  median |err_y|             : {statistics.median(errs):.2f}")
        print(f"  hold range                 : {min(holds):.2f}s - {max(holds):.2f}s")
        # Sanity check, not a re-derivation: hold should sit in [0.08, 0.35]
        # for the shipped defaults (tracking.pitch_min_hold_sec/max_hold_sec).
        # A value outside that band would mean the clamp itself is broken,
        # not just under-validated — worth a hard flag, not a footnote.
        out_of_band = [h for h in holds if not (0.08 - 1e-6 <= h <= 0.35 + 1e-6)]
        if out_of_band:
            print(f"  ANOMALY: {len(out_of_band)} hold value(s) outside the "
                  f"expected [0.08, 0.35] clamp band — check pitch_min_hold_sec/"
                  f"pitch_max_hold_sec haven't drifted from these logs' config.")

    print("\n" + "=" * 72)
    print("VERDICT")
    if grand_rationale < MIN_OPPORTUNITIES_FOR_A_VERDICT:
        print(f"  Selection Hardening: too few opportunities ({grand_rationale}) to "
              f"judge Phase 2 — HLDD 005 named no bar, this script's own is "
              f"{MIN_OPPORTUNITIES_FOR_A_VERDICT}. Keep flying; nothing to decide yet.")
    else:
        print(f"  Selection Hardening: {grand_rationale} opportunities observed, "
              f"ranked pool would have changed the pick {grand_ranked} time(s) "
              f"({100 * grand_ranked / grand_rationale:.0f} pct) — enough to weigh "
              f"whether Phase 3 (flip tracking.ranked_lock_priority live) is worth it.")
    if grand_pitch < MIN_OPPORTUNITIES_FOR_A_VERDICT:
        print(f"  Two-Axis Rollout: too few pitch samples ({grand_pitch}) to judge "
              f"Phase 2 — same {MIN_OPPORTUNITIES_FOR_A_VERDICT}-sample bar. Needs "
              f"tracking.enabled: true sessions (or GAME_BATTLE_MANUAL flying) to "
              f"keep accumulating; the ambient path is the only source of this signal.")
    else:
        print(f"  Two-Axis Rollout: {grand_pitch} pitch samples observed — numerically "
              f"self-consistent is not the same as visually sensible; cross-check "
              f"against archived tests/test-output/target_tracking/ frames before "
              f"considering Phase 2 (live actuation).")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="*",
                     help="Log files to read. Defaults to logs/*.log plus wingman.log.")
    args = ap.parse_args(argv)

    paths = args.logs or sorted(glob.glob("logs/*.log")) + ["wingman.log"]
    # wingman.log is the live/most-recent run and is only ever archived into
    # logs/ once that run ends — but guard the boundary case (archived right
    # at the moment this runs) by dropping it if its own first line matches
    # an already-archived file's first line, rather than double-counting.
    seen_first_lines = set()
    results = []
    for path in paths:
        first, last = _first_last_timestamp(path)
        if path == "wingman.log" and first is not None and first in seen_first_lines:
            continue
        if first is not None:
            seen_first_lines.add(first)
        r = parse(path)
        if r["rationale_total"] or r["ranked_total"] or r["pitch_total"]:
            results.append((path, r, (first, last)))

    if not results:
        print("No SELECT[shadow] or PITCH[shadow] evidence found in any given log.")
        return 0
    report(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

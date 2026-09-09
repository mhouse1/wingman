#!/usr/bin/env python3
"""Did the boundary turn actually gain range? (ADR 125 / ADR 133 V7)

ADR 106's tracked metric — confirmed crossings per mission — cannot answer this.
At the pooled rate of 0.132 per mission, an eleven-hour soak yields about fifteen
crossings, and the 95% interval on that is 0.065 to 0.199. The smallest change it
could distinguish is a **halving**; seeing a 24% effect would take roughly
forty-eight hours of flying. Nine days of rows have looked flat partly because the
metric cannot resolve anything smaller than a catastrophe.

The turn's own outcome trace is 13x denser from the same flying — 173 turns
against 13 crossings in the 2026-09-06 overnight soak — and it measures the thing
a detector change actually acts on: not whether the aircraft left the arena, but
whether the turn, once triggered, moved the aircraft away from the edge.

    make turn-outcome LOG=logs/<session>.log

Reads two adjacent lines the run already emits:

    BOUNDARY TURN range: 0.44R -> 0.15R (closest 0.05R, 9 ticks, receded)
    BOUNDARY TURN bearing: +66 deg -> +78 deg (net +12 deg, path 12 deg, 4 samples)

ADR 107's baseline, over 61 turns, was a median range gain of **+0.00R** with 13
turns gaining 0.10R or more against 15 losing it — the signature of a coin flip
between "away" and "into".
"""

from __future__ import annotations

import argparse
import re
import statistics
import sys

RANGE = re.compile(
    r"BOUNDARY TURN range:\s*([0-9.]+)R\s*(?:->|→)\s*([0-9.]+)R"
    r"\s*\(closest\s*([0-9.]+)R,\s*(\d+) ticks,\s*([a-z ]+)\)")
BEARING = re.compile(
    r"BOUNDARY TURN bearing:\s*([+-]?[0-9]+) deg\s*(?:->|→)\s*([+-]?[0-9]+) deg"
    r"\s*\(net\s*([+-]?[0-9]+) deg")


def parse(path):
    turns = []
    pending = None
    with open(path, errors="ignore") as fh:
        for line in fh:
            m = RANGE.search(line)
            if m:
                # A range line with no bearing after it still counts: the range
                # is the outcome, the bearing only explains it.
                if pending:
                    turns.append(pending)
                pending = {
                    "start": float(m.group(1)), "end": float(m.group(2)),
                    "closest": float(m.group(3)), "ticks": int(m.group(4)),
                    "verdict": m.group(5).strip(), "net_deg": None,
                }
                continue
            m = BEARING.search(line)
            if m and pending is not None and pending["net_deg"] is None:
                pending["net_deg"] = int(m.group(3))
    if pending:
        turns.append(pending)
    return turns


def report(turns, label):
    if not turns:
        print(f"{label}: no turn outcomes found")
        return
    gains = [t["end"] - t["start"] for t in turns]
    n = len(gains)
    won = sum(1 for g in gains if g >= 0.10)
    lost = sum(1 for g in gains if g <= -0.10)
    receded = sum(1 for t in turns if "receded" in t["verdict"]
                  and "never" not in t["verdict"])
    swings = [abs(t["net_deg"]) for t in turns if t["net_deg"] is not None]
    print(f"\n{label}")
    print(f"  turns measured            : {n}")
    print(f"  median range gained       : {statistics.median(gains):+.3f}R")
    print(f"  mean range gained         : {statistics.fmean(gains):+.3f}R")
    print(f"  gaining 0.10R or more     : {won:3d}  ({100*won/n:.0f} pct)")
    print(f"  losing  0.10R or more     : {lost:3d}  ({100*lost/n:.0f} pct)")
    print(f"  the log called 'receded'  : {receded:3d}  ({100*receded/n:.0f} pct)")
    if swings:
        print(f"  median heading swing      : {statistics.median(swings):.0f} deg")
    print(f"  median closest approach   : "
          f"{statistics.median([t['closest'] for t in turns]):.3f}R")
    # The ADR 133 decision rule, stated before the run rather than after it.
    med = statistics.median(gains)
    print()
    if n < 60:
        print("  VERDICT: too few turns to read. ADR 107's baseline used 61.")
    elif med >= 0.05:
        print("  VERDICT: range gained is clearly POSITIVE. The turn works when")
        print("           it is triggered early enough — ADR 107's break-even")
        print("           finding was an artefact of late detection.")
    elif med <= -0.05:
        print("  VERDICT: the turn is losing range. It is flying INTO the edge")
        print("           more often than away; the direction choice is wrong.")
    else:
        print("  VERDICT: still break-even, as ADR 107 measured. An earlier")
        print("           trigger did not help, so the defect is in the TACTIC,")
        print("           not in when it fires.")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+")
    a = ap.parse_args(argv)
    for path in a.logs:
        try:
            report(parse(path), path.split("/")[-1])
        except OSError as e:
            print(f"{path}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

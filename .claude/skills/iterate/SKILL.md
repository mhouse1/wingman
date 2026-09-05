---
name: iterate
description: Run one live-fix cycle on wingman — review the newest session log, diagnose from measurement, fix with tests, pass the gates, relaunch, and watch for the failure to recur. Use when the operator says "iterate", reports a live misbehaviour, or asks to review a log and act on it.
---

# Iterate

One cycle: **review → diagnose → fix → gate → run → watch → record.**

Do not skip to the fix. Most of the value is in the diagnosis, and most of the
mistakes come from acting on inference that looked like measurement.

## 1. Review

```bash
ls -lt logs/*.log wingman.log | head -3          # newest first
grep -A18 "Wingman Session Summary" <log> | tail -19
grep -c "BOUNDARY: dist=" <log>; grep -c "BOUNDARY: no reading" <log>
```

`wingman.log` is the **live** session; `logs/wingman_<end-stamp>.log` are archived.
The live file opens with `mode="w"` — a rerun destroys it. If a session matters
and is not yet archived, copy it before doing anything that could restart wingman.

Frames land in `test_screenshots/unknown_anomalies/` — `rtb_*` at confirmed
crossings, `approach_*` at approaches. Read them; they answer questions the log
cannot, and a minimap crop is usually the fastest route to a colour-detection bug.

## 2. Diagnose

**Measure. Never infer a rate from a mechanism you have not counted.**

Normalise per mission, not per hour — sessions run 40 minutes to 10 hours and
raw counts are not comparable.

Say which of these you have:

- **measured** — you counted it in this log
- **inferred** — consistent with the log but not shown by it
- **assumed** — neither

Write the label into the report. "The roll is not reaching the aircraft" and
"the roll reaches it and does nothing" look identical in a log with no attitude
trace; if you cannot separate two explanations, **add instrumentation instead of
tuning**. Force applied to an unmeasured lever is guesswork.

### Small samples mislead, repeatedly

This project has produced 0.00, 0.10, 0.26 and 0.34 crossings per mission **on
code that did not change**. Under 40 missions, treat any rate as noise. A
session that flatters a change you just made deserves more suspicion, not less.

### Domain impossibilities are the best detectors

The operator's "an aircraft never spawns pointing at the boundary" turned a
vague *seems too eager* into one tick with specific numbers. When a report is
qualitative, look for the physical claim inside it and grep for that.

## 3. Fix

**One change at a time.** ADR 101 rev 2 shipped in the same gap as a MetalStorm
minimap update, and that row in ADR 106 is permanently unattributable. If the
game updates mid-investigation, record it as a column, not a footnote.

Write the test that would have caught it, in the same commit as the fix. Then
run the whole suite — the existing tests catch category errors that live data
cannot:

- bounding-box fill looked perfect on curved arcs and **rejects straight lines**;
  the synthetic line test failed in seconds
- two morphological closing passes reconnected the real line and also **bridged
  a speckle grid into a fake one**; the terrain test caught it

When a test you wrote fails, decide which is wrong before editing either. Several
times here the code was right and the test's premise was not.

### A green suite is not coverage — check what the fixture claims to be

`mission_loiter` had nine passing tests and raised `AttributeError` on its first
live tick. Two bugs in one line — `snap.altitude.stable` (the field is
`stable_value`) and `snap.altitude_fresh` without the call (it is a method, so
the expression was always truthy and the stale-read guard never ran) — and the
test fake reproduced BOTH, because it had been written from the caller's
assumptions rather than from `TelemetrySnapshot`.

A fake that mirrors the code it tests proves only that the code is
self-consistent. Build fakes from the real type — import it and construct it if
you can — and prefer the real object wherever it is cheap.

The same shape bit a corpus test the same day: it globbed `approach_*.png`, so
the next session dropped unrelated frames into it and the assertion broke.
**A fixture described loosely will absorb things that do not belong.** Enumerate
a curated corpus; do not pattern-match a directory that something else writes
to.

## 4. Gate

```bash
make lint && make test
```

Both must pass before running. `make test` collects `tests/` directly, so a new
file is picked up automatically.

## 5. Run

```bash
make r1        # account 1; r2 for account 2. Long-running: background it.
```

**Verify the process state you claim.** Stopping wingman means signalling the
*interpreter*, not the `uv run` shim that wraps it:

```bash
pgrep -af "wingman.main"                    # expect the shim AND python3
for p in $(pgrep -f "wingman.main"); do kill -TERM $p; done
until ! pgrep -f "wingman.main" >/dev/null; do sleep 2; done
```

SIGTERM routes through `exit_requested`, so cleanup runs and artifacts are
written. It is **not** an operator stop, so the game and `:3` deliberately stay
up (ADR 105). Never report "stopped" or "running" without a check in the same
turn — a shim died here once and the session ran on for half an hour.

## 6. Watch

Arm a Monitor on the **specific failure**, not on the log generally:

```
tail -F -n 0 wingman.log | grep -E --line-buffered \
  "<the error indicator>|Traceback|\[ERROR\]|LIVENESS GUARD|GAME GONE|Wingman Session Summary"
```

The indicator should be the thing that must not recur, phrased so silence is
meaningful. Include the failure signatures too — a filter that only matches the
happy path is silent through a crash, and silence looks like success.

## 7. Record

Add the row to the tracking ADR **before the next run truncates the log**
(ADR 106 D4). Record wingman's code state *and* the game UI version; both move.

Amend a `Draft` ADR in place; supersede an `Accepted` one with a new ADR. When a
decision turns out wrong, write what the measurement said — ADR 107 D2's premise
was disproved by its own validation, and saying so is worth more than the ADR
looking correct.

## Standing traps

- **The metric can be the bug.** 94% of boundary colour triggers were false
  positives; counting triggers measured the detector's noise, not the aircraft.
- **A fix in the perception layer changes the tactic layer's input**, so its
  numbers are not comparable across that change.
- **Improving one thing exposes the next.** The turn-release defect was invisible
  while the detector was blind 81% of the time.
- **Instrumentation can lie.** A sampler on a 0.25 s timer reading telemetry that
  lands every 3 s reported "swing 0, n=12" from **one** reading — which reads as
  "the aircraft did not rotate" and was not what it measured.

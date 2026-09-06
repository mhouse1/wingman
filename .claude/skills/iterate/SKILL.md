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
make rd        # attaches to a game that is already up
```

### Stopping: finish the round first

**Always stop with `z` (`FINISH_ROUND_THEN_EXIT`, ADR 094), not with a signal.**
Wingman finishes the round in progress, exits at `GAME_LOBBY`, and closes
MetalStorm. It is deferred and reversible — press `z` again to cancel a pending
stop.

The point is the state it leaves behind. **Exiting at the lobby is what lets the
next session enter cleanly.** A signal stops wherever the aircraft happens to
be — mid-battle, mid-respawn, mid-eject — and the next start has to recover the
game from that state instead of clicking PLAY from a lobby it already trusts.

```bash
# 'z' is not in INJECTABLE_KEYS, so a synthetic press on the nested display is
# indistinguishable from the operator's and cannot be filtered as an echo.
uv run --active python - <<'PY'
import time
from Xlib import display as xd, X, XK
from Xlib.ext import xtest
d = xd.Display(":3")
c = d.keysym_to_keycode(XK.string_to_keysym("z"))
xtest.fake_input(d, X.KeyPress, c); d.sync(); time.sleep(0.05)
xtest.fake_input(d, X.KeyRelease, c); d.sync()
PY
```

Then wait for it to land — a round can take minutes, and the stop only fires at
a safe point:

```bash
grep -c "FINISH ROUND" wingman.log            # request acknowledged
until ! pgrep -f "[w]ingman.main" >/dev/null; do sleep 5; done
```

### Signals are the fallback, and they cost something

Use SIGTERM only when there is no round to finish, or when wingman is
unresponsive to the key. It routes through `exit_requested`, so cleanup normally
runs; it is **not** an operator stop, so the game and `:3` deliberately stay up
(ADR 105).

But a mid-round SIGTERM is not free. On 2026-09-05 at 08:02 one produced a
shutdown that hung: logging stopped on the same second, `Exit requested` was
never written, and the process was still alive 5.5 minutes later. It had to be
SIGKILLed, so no summary and no stats were written — and the next start rotated
its log away. The overnight session left the same signature and cost ~6 hours of
soak data.

```bash
pgrep -af "[w]ingman.main"                  # expect the shim AND python3
for p in $(pgrep -f "[w]ingman.main"); do kill -TERM $p; done
until ! pgrep -f "[w]ingman.main" >/dev/null; do sleep 2; done
```

**Verify the process state you claim.** Signal the *interpreter*, not the
`uv run` shim that wraps it — a shim died here once and the session ran on for
half an hour. Never report "stopped" or "running" without a check in the same
turn.

**Bracket the pattern.** `pgrep -f "wingman.main"` and `pkill -f "make rd"` also
match the shell command line that contains them, so an unbracketed `pkill`
kills the calling shell. Three tool calls died that way in one session, each
reported only as an exit code. Write `[w]ingman.main`, or use `pgrep -x`.

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
- **Compare like with like, and check the denominator.** Two numbers that both
  say "threads" were `threading.active_count()` (Python threads, 24) and
  `/proc/<pid>/task` (OS threads, 325, mostly EasyOCR's native pools) — that
  looked like a 13x leak and was two different quantities. The same mistake gave
  "23% readability" by dividing boundary reads over *every* tick including lobby
  and loading screens, where there is no minimap to read; in `GAME_BATTLE` it
  was 56%. Both were reported before being checked, and both were wrong.
- **Reproduce with the real function, not a re-implementation.** A hand-rolled
  copy of `detect_map_boundary` said a frame passed every gate; the real one
  returned None. A harness missing `_minimap_circle_cache` then printed 23
  `None` results that were exception handlers, and looked exactly like data.
  Build the real object, and treat "no exception logged" as part of the result.
- **A fix in the perception layer changes the tactic layer's input**, so its
  numbers are not comparable across that change.
- **Improving one thing exposes the next.** The turn-release defect was invisible
  while the detector was blind 81% of the time.
- **Instrumentation can lie.** A sampler on a 0.25 s timer reading telemetry that
  lands every 3 s reported "swing 0, n=12" from **one** reading — which reads as
  "the aircraft did not rotate" and was not what it measured.

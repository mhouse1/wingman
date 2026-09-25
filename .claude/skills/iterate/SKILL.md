---
name: iterate
description: Run one live-fix cycle on wingman — review the newest session log, diagnose from measurement, fix with tests, pass the gates, LAUNCH wingman live, and watch for the failure to recur. Every cycle ends with wingman running (or a named blocker); invoking this skill is the operator's authorization to run it, so never close a cycle by asking whether to. Use when the operator says "iterate", reports a live misbehaviour, or asks to review a log and act on it. Also supports a self-directed continuous mode — triggered by phrasing like "iterate with your recommendations", "keep looping", or "until I tell you to stop" — that picks its own target each cycle, keeps a running ELI5 changelog, and reschedules itself until the operator stops it or the session ends.
---

# Iterate

One cycle: **review → diagnose → fix → gate → run → watch → record.**

Do not skip to the fix. Most of the value is in the diagnosis, and most of the
mistakes come from acting on inference that looked like measurement.

## A cycle is done when the report can say all of this

1. what the newest log showed, each claim labelled measured / inferred / assumed;
2. the one change, and the test that would have caught it;
3. `make lint && make test` green;
4. **wingman is running**: `pid <n>, started <HH:MM>, log wingman.log` — or a
   named blocker from step 5, written as `blocked: <reason>`;
5. a Monitor is armed on the specific failure (step 6);
6. the tracking document has the row (step 7).

**Invoking `/iterate` is the authorization to run wingman**, in a single cycle
exactly as in loop mode. Items 4 and 5 are not optional and are not a question
for the operator. A report that ends "want me to start a live run?" has skipped
both: on 2026-09-24 a cycle closed that way, with the gates green and the
documents written, and the operator had to point out that this skill says to
run. The only time step 5 lets you ask instead of run is the one case it names
(a session that is not yours is already running).

## Loop Mode

Triggered by phrasing like *"iterate with your recommendations"*, *"keep
iterating"*, *"keep looping until I tell you to stop"*, or *"loop this until
credits run out"*. This runs the same seven-step cycle below, repeated
automatically, with these differences from a single manual cycle:

**Pick your own target.** Don't wait for the operator to name a bug. At the
top of each cycle, choose the next-highest-value thing to work on and state it
in one line before starting Review:

- the newest unresolved row in a tracking ADR (grep `docs/adr/` for open items)
- a "Standing traps" entry below that the current log shows is still live
- the most significant anomaly in the newest session log
- if nothing stands out, re-run `make tp` and treat any regression as the target

State it as a recommendation ("going after X because Y"), not a question —
loop mode exists so the operator doesn't have to steer each cycle.

**Keep an ELI5 changelog.** After every fix (step 3) or notable decision (a
hypothesis rejected, a fix deferred, a gate that failed and why), append one
entry to `iterate-eli5.md` in your scratchpad directory:

```
## Cycle <n> — <YYYY-MM-DD HH:MM:SS> — <one-line title>
<2-4 plain-English sentences: what changed, why, what it should do differently
now. No jargon, no function names unless a name IS the point.>
```

Get the timestamp from `date '+%Y-%m-%d %H:%M:%S'` at the moment you write the
entry — never guess or reuse the previous entry's time. The gap between
consecutive headings is the whole point: it's how the operator tells a loop
that's grinding through cycles in seconds from one that's waiting hours on
live runs.

Post the same entry into your reply to the operator — the file is a durable
backup for when the conversation compacts, not the only copy. If you're unsure
what earlier cycles did, read the whole file back rather than trusting memory
of a compacted transcript.

**Watch (step 6) does not pause the changelog.** This requirement is not
scoped to Fix — a live trial under Watch can run for hours and produce real
findings with no code change at all: a crash investigated and its true cause
found, a false-positive pattern confirmed from real evidence, a capture
budget quietly exhausted. Those are exactly the "notable decisions" this file
exists to capture, on the same footing as a code fix.

2026-09-16: a live trial ran 4.5 hours under Watch, producing dozens of
Monitor notifications. Every one got a full analysis posted to the operator
in chat — and not one made it into the file, because "reply in chat" and
"write to the file" quietly split into two separate habits once
notifications started arriving faster than deliberate cycles did. The
operator had to notice the file had gone quiet and ask why. Chat is not the
durable copy — it is the thing that gets compacted away; a finding that only
exists in a chat reply is one compaction away from gone. Write the file entry
as part of producing the finding, not as a follow-up after the chat reply. If
a reply is substantial enough to explain a finding to the operator, it is
substantial enough to earn one line in the file, in the same turn. When a
run of Monitor notifications turns repetitive (the same pattern confirming
itself again), a short chat acknowledgment without a file entry is fine —
but the moment one of them is actually new, log it before moving on, not
"later, once things settle down."

**Keep going without being asked.** Run steps 1-7 exactly as below — don't
skip Gate or Run to go faster. Permission to run is the invocation itself
(the contract above, step 5): loop mode only repeats it, so never ask "should I
launch a run?" in any cycle. Do stop and ask if a gate fails in a way you can't
diagnose, or a decision genuinely needs the operator (ambiguous requirement,
destructive action, anything the git rule below reserves to them).

At the end of a cycle, call `ScheduleWakeup` to queue the next one instead of
looping synchronously — a live run needs wall-clock time to produce evidence.
Pass the operator's original loop request back as `prompt` so re-entry repeats
correctly. Once Watch has a Monitor armed, use a long fallback delay
(1200s+) — the Monitor notification is the real signal that something
happened; the scheduled wakeup is just the safety net if it doesn't.

Never stop yourself after N cycles "to check in." The only valid stops are:
the operator explicitly says stop (call `ScheduleWakeup` with `stop: true` and
post a final ELI5 summary), or you hit a decision point per the bullet above.
Running out of credits ends the session on its own — it is not something to
plan around or announce in advance.

**The git rule still applies.** Loop mode is not a standing commit
authorization. `git commit`, `git push`, `git tag`, `make p`, and `make
wrelease` still require the operator to ask in the current request — see the
rule at the top of this repo's CLAUDE.md, which outranks this skill. The ELI5
changelog is what the operator reviews before deciding to commit any of it
themselves.

## 1. Review

```bash
ls -lt logs/*.log wingman.log | head -3          # newest first
grep -A18 "Wingman Session Summary" <log> | tail -19
grep -c "BOUNDARY: dist=" <log>; grep -c "BOUNDARY: no reading" <log>
```

`wingman.log` is the **live** session; `logs/wingman_<end-stamp>.log` are archived.
A start rotates the previous log into `logs/` (`main.py` renames it, never
truncates it), so relaunching keeps it — unless the launch prints `could not
rotate previous log`, in which case copy it first. The live file itself opens
with `mode="w"`, so never point anything that must survive at it.

**Does the newest log postdate the code you are judging?** Compare its first line
with `git log -1 --format=%ci` and the mtimes of what `git status --short`
lists. If not, it is no evidence for that code — on 2026-09-24 three changes sat
unrun on top of a log that predated all of them, and the review turned into
offline replay while nothing ran. Say so in the first line of the review, and if
the diagnosis will need live evidence, **start the run now** (step 5) instead of
after the fix: a run costs wall-clock time, offline replay does not. Replay on
archived frames is diagnosis, never the cycle's verdict.

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

**The gate is step 4 of 7, not the end of the cycle, and running wingman is not
a question to put to the operator.** An anomaly-detector fix landed 2026-09-13:
tests went green and the report called running it live "worth doing" — advice
for later, not a step taken. On 2026-09-24 the step offered "launch it, or ask"
and the cycle closed on "want me to start a live run?", with the gates green and
the documents written; the operator had to point out that the skill says to run.
The ask branch was the trap, so it is gone: invoking `/iterate` already said
yes, and a live session on the operator's account is exactly what this skill
exists to run (an unattended run is the normal case, not a special one).

**Do all four of these in the same turn the gate passes:**

1. **Pre-flight, ten seconds.**

   ```bash
   pgrep -af "[w]ingman.main"        # is a session already running?
   make nested-status                # the nested display :3 and the game, if any
   df -h . | tail -1                 # capture budget floor is 10 GB free
   ```

   - **Nothing running** → launch. Do not confirm.
   - **A session you started** (this conversation, or the ELI5 changelog says
     so) → if it started after your last change it already is the run: go to
     Watch. If it started before, it cannot exercise the change: stop it with
     `z` (below), wait for it to exit, then launch.
   - **A session you did not start is the operator's own.** Do not start a
     second one — both want `:3` and the account, and `make r1` kills and
     relaunches the game — and do not stop it. Copy its log into your scratchpad
     and Watch the copy. If it started before your change it cannot exercise
     it: report `blocked: your session (started HH:MM) predates the fix; stop
     it with z when a round ends and I will launch`, and Watch it anyway. That
     is the only situation where you ask instead of run.

2. **Launch**, backgrounded (`run_in_background: true`), never in the foreground:

   ```bash
   make r1                       # account 1 (r2: account 2). Kills and relaunches the game,
                                 # then wingman with DEBUG to wingman.log
   make r1 v                     # the same plus --record-session: session video and BT
                                 # trace, and the only mode that arms the eject-stuck check
   make r1 GAME_LAUNCH_DEPS=     # attach to a game that is already up. Never put
                                 # nested-setup in it: it closes a running game
   ```

   `make r` writes no log file, so it is no use here. Read a target's recipe
   before running one you have not used; `make tp`, `tp-full`, `wrelease` and
   `rr-live-path1-gate` are operator-run and are not launched by this skill.

3. **Verify it came up**, in the same turn:

   ```bash
   pgrep -af "[w]ingman.main"        # the shim AND python3
   sleep 45; tail -5 wingman.log | cut -c1-160
   ls -t logs/*.log | head -2        # the previous log was rotated here
   ```

   A launch that dies at startup is a finding to report, with the log lines
   that show it — not a reason to stop the cycle.

4. **Go to Watch (step 6) and arm the Monitor** before you write the report.

**Not launching is legitimate only for these reasons**, and each is reported as
`blocked: <reason>` in the closing report, never as a question about whether to
run: the operator's own session is running (above); a gate failed and you cannot
diagnose it; the operator said in this request not to run; the game or display
is unavailable (`make nested-status` and the game executable say so).

**A fix for a rare or intermittent failure is not an exception.** "Small
samples mislead" (above) means a short check afterward cannot prove the fix
worked — it does not mean skip running. Background a long session anyway;
Watch (step 6) is what accumulates evidence toward a real verdict, across
this run and the ones after it, not a single same-turn check.

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

Two things seen on 2026-09-24. Check the acknowledgement within about 15 s: 2 of
7 synthetic `z` presses were never acknowledged (cause not found; `v` was ignored
in one of them too). Press it once more, and read the once-a-minute DEBUG line
`XKey[:3]: N KeyPress events in the last 60s` — N of 0 means the events are not
arriving — before falling back to a signal. And never signal wingman or close the
game during matchmaking or loading: the server still forms the match, and the next
launch drops straight into a live round with nobody flying. `z` is deferred to the
lobby by design; a signal is not.

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

`Monitor` is a deferred tool: fetch its schema with ToolSearch (`select:Monitor`)
before calling it, or the call fails. Arm it in the same turn as the launch, then
write the report — the run is up and something is already watching it.

**In a single (non-loop) cycle the report goes out once the run is up and the
Monitor is armed; it does not wait for the run to end**, which can take hours.
The report says what the run is expected to show and how you will know. The
verdict arrives as Monitor notifications and is handled as they land, each new
finding written to the tracking document (step 7) and, in loop mode, the changelog
as it is produced. The run stops when the operator says so, or when the failure you
were watching for has been confirmed or refuted — then stop it with `z`.

## 7. Record

Add the row to the tracking ADR **before the next run truncates the log**
(ADR 106 D4). Record wingman's code state *and* the game UI version; both move.

Amend a `Draft` ADR in place; supersede an `Accepted` one with a new ADR. When a
decision turns out wrong, write what the measurement said — ADR 107 D2's premise
was disproved by its own validation, and saying so is worth more than the ADR
looking correct.

## Standing traps

- **A question is not a step.** Step 5 once offered "launch it, or ask", and the
  ask branch swallowed the run: the cycle closed on "want me to start a live run?"
  with the gates green (2026-09-24). Wanting to be careful with the operator's game
  account is real, and it is already answered — the operator invoked this skill.
  Check for a session that is not yours, and otherwise launch. Carefulness belongs
  in the pre-flight and in reading a recipe before you run it, not in withholding
  the run.
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

---
name: check
description: Review the newest wingman.log against the file currently open in the IDE and the most recent conversation context to judge whether the last edit actually worked, then recommend what to do next. Read-only — no fixes, no relaunching, no commits. Use when the operator says "/check", "check the log", "did that work", or asks whether the last edit/fix took effect.
---

# Check

One question, answered from measurement: **did the last edit do what it was
supposed to do, according to the newest log — and what should happen next?**

This is a read-only diagnostic, not a fix cycle (that is the `iterate` skill).
Do not edit files, run `make` targets, restart wingman, or commit anything
here — report and recommend only.

## 1. Establish what "the last edit" is

Before touching the log, pin down what you are actually checking:

- The most recent code change made in this conversation — the file(s)
  Edited/Written and why. Pull this from your own preceding turns, not a
  re-guess from the diff alone; the reasoning behind the edit is part of what
  you're checking.
- The file currently open in the IDE (`<ide_opened_file>`), if one is
  present. It often names the subsystem the operator wants checked, but it is
  not always the same thing as the last edit — an ADR or doc can be open
  while the edit touched the code it describes, or the open file can be
  unrelated leftover context. If the open file and the last edit point to
  different areas, say so explicitly rather than silently picking one; check
  both if that's cheap.
- If nothing was edited yet this conversation and nothing else pins down a
  specific change, say there is nothing to check rather than inventing a
  target.

## 2. Find the newest log and confirm it postdates the edit

```bash
ls -lt logs/*.log wingman.log 2>/dev/null | head -5
```

`wingman.log` is the **live** session; `logs/wingman_<end-stamp>.log` are
archived. The live file opens with `mode="w"` on each run, so its mtime marks
when the run *started*, not when it last wrote — check that against when the
run actually started, not just "is this file recent."

**The log must postdate the edit.** If the newest log started before the edit
was made, there is no evidence yet — say exactly that ("no run since the fix
— nothing to check yet") and recommend running it, rather than reviewing a
stale log and reporting unrelated behavior as if it bore on the edit.

## 3. Look for the specific evidence, not the general shape

Grep for what the edit was actually supposed to change: the log lines,
states, error signatures, or counters tied to the code path you touched.
General session health (`Wingman Session Summary`, resource lines) is
supporting context, not the verdict.

```bash
grep -n "<specific message or marker introduced or changed by the edit>" <log>
grep -A18 "Wingman Session Summary" <log> | tail -19   # context only
```

If the edit added or changed a log message, that message is the fastest
signal — whether it appears, how many times, and what surrounds it says more
than inference from adjacent behavior ever will.

## 4. Label the verdict, don't just assert it

Same discipline the `iterate` skill uses for diagnosis:

- **confirmed** — the log shows the specific behavior the edit targeted,
  working as intended
- **contradicted** — the log shows the edit's target path ran and did *not*
  do what was intended
- **no evidence** — the log never exercises the path at all this run, or
  predates the edit
- **inconclusive** — some evidence, but not enough to call it either way —
  say precisely what is missing

Do not round "no evidence" up to "confirmed" because nothing looks wrong. A
code path that never ran is not a code path that worked.

## 5. Recommend, tersely

Close with one concrete next step:

- **confirmed** — say so, and name anything that is now safe to consider
  closed
- **contradicted** — name the specific thing still wrong, pointed at the
  actual log lines that show it
- **no evidence / inconclusive** — say what run or condition would produce
  the evidence (e.g. "needs a session that reaches STANDBY and then Ctrl-C
  within a few seconds", "rerun and watch for `<marker>`")

Keep the report short: what you checked, the verdict with its label, the
evidence line(s) it rests on, and the one recommended next step. Skip
re-explaining the edit itself — the operator already knows what they asked
for.

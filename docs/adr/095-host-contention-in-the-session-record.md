# ADR 095 — Record Host Contention in the Session Record

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-26 | 1.8.7           |

## Context

Wingman shares VEDA with Jenkins, Redmine, Docker, a GNOME desktop, a browser,
an IDE, and the game it is playing. Every performance number the project
records — the 725-session release baseline, ADR 092's leak gate, the missile
evade A/B — was measured with some unrecorded mixture of those also running.

On 2026-08-26 a session felt slow and the cause could only be established by
inspecting the machine **after the fact**:

| | |
|---|---|
| load average, 20 cores | **9.43** 15-min |
| Jenkins JVM | 1,772 MB RSS, 58 threads |
| Brave / VS Code | ~3.0 GB / ~2.0 GB across renderers |
| system swap | 3.2 GB, with `gnome-shell` 114 MB paged out |

Wingman's own numbers over the same window were clean — live heap flat at
1525→1536 MB, OCR median 0.25 s, no OCR backlog — so nothing in wingman's record
indicated a problem, and nothing in it indicated the machine was loaded either.

That is the gap. **A session cannot currently say what else was running.**
`RESOURCE` lines already carry `sys_swap_mb`, and that was the one clue that
survived; there is no load average anywhere in the codebase, and `run_*.json`
carries no host context at all — only `version`, `run_id`, `account`, timestamps,
`rounds`, `ocr_crops`, `reaction`.

This matters more now than it did before ADR 092. That gate compares a session's
growth against an archive of prior sessions, and the missile-evade tactics are
judged on survival percentages across sessions. Both assume sessions are
comparable. A soak run against a loaded machine and one run against a quiet one
are not, and today nothing records which was which.

foundry ADR 021 adds the lever — **TRIAL**, a one-gesture stand-down of Jenkins,
Redmine and Docker before a session. This ADR is the wingman half: make the
session record say whether that lever was pulled.

## Decision

Record host contention alongside wingman's own resource figures, so a session's
performance data can be judged against the conditions it was measured in.

### 1. Load average in the `RESOURCE` line

Add the three `/proc/loadavg` figures to the existing periodic line, beside
`sys_swap_mb`. It is a single cheap read with no new dependency, and it is the
one number that summarises total machine contention regardless of which
co-tenant is responsible.

Load is *not* attributed per-process. Naming the noisy neighbour is a different
job, and enumerating other people's processes is a larger scope than this needs.

### 2. A host-context block in `run_*.json`

The per-session record gains a small block: load average at start and end, peak
load observed, system swap at start and end, and CPU count so the load figures
are interpretable on other hardware.

`RESOURCE` lines live only in the log, which is rotated and gitignored. The run
JSON is the artifact that survives and that the aggregators read, so context
that matters for comparability belongs there.

### 3. A startup note when the machine is already loaded

If load average or swap is high at startup, log it once, at INFO, naming TRIAL:

```
Host: load 9.4/20 cores, swap 3.2 GB in use at startup — consider TRIAL
(foundry ADR 021) before a measurement soak
```

**A note, not a gate.** Wingman must never refuse to run because the machine is
busy: unattended operation is the point, and a session on a loaded machine is
still a valid session — it is simply one whose timings should be read with that
in mind.

### What this deliberately does not do

- **No automatic TRIAL invocation.** Wingman does not stop other people's
  services. ADR 094 already gave it the ability to terminate the game it
  launched, which was a deliberate and bounded escalation; reaching further into
  the host is not.
- **No gating of the leak gate on host load.** ADR 092's thresholds are about
  wingman's memory growth, which tonight's session showed is unaffected by
  co-tenant CPU load. Adding a load condition would couple two things that the
  evidence says are independent.
- **No retroactive annotation.** The 725-session baseline has no host context and
  cannot acquire any. New sessions become comparable to each other; old ones stay
  as they are.

## Consequences

- A slow session can be diagnosed from its own record instead of by inspecting
  the machine afterwards, which only works if someone is present and looks in
  time.
- Sessions become sortable by host conditions — the question "were the clean
  runs also the quiet runs?" becomes answerable across the archive rather than
  anecdotal.
- One more field group in `run_*.json`. The schema is additive, so
  `_load_run_file`'s `ocr_crops` requirement (ADR 092) is unaffected and old
  files stay readable.
- Load average is a coarse instrument. It does not distinguish CPU contention
  from IO wait, and on a 20-core machine a load of 9 is not obviously bad. It is
  recorded as context, not as a verdict.

## Alternatives considered

**Record per-process co-tenant footprints.** Rejected for now: it means
enumerating and naming other users' processes in wingman's own artifacts, which
is a privacy and scope expansion for a marginal gain over load average. The
diagnosis that mattered tonight — "the machine was loaded" — needs only the
aggregate.

**Have wingman invoke TRIAL itself before a soak.** Rejected. Stopping Jenkins
could discard a running Yocto build (foundry ADR 009/010), and the guard for
that belongs with the thing that owns the services, not with a game automation
rig.

**Gate measurement sessions on a quiet machine.** Rejected: it would make
unattended operation conditional on the desktop being idle, which inverts the
purpose.

## Validation

- **V1** — the `RESOURCE` line carries load average, and parses in the existing
  `leak-check.py` field regex without change.
- **V2** — `run_*.json` carries the host-context block, and `_load_run_file`
  still accepts both new and pre-ADR-095 files.
- **V3** — the startup note fires on a loaded machine and stays silent on a
  quiet one.
- **V4** — a loaded machine never prevents a session starting.
- **V5** — reading `/proc/loadavg` never raises into the tick; a platform
  without it degrades to `n/a`, as the other probes already do.

## References

- foundry ADR 021 — TRANSAM up, TRIAL down: the operational lever this records
- ADR 092 — the leak gate that assumes sessions are comparable
- ADR 094 — the bounded precedent for wingman terminating a process it owns
- Performance 008 — the investigation whose baselines this makes interpretable
- `wingman/resource_monitor.py` — the periodic line being extended

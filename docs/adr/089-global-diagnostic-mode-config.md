# ADR 089 — Always-On Diagnostic Logging

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-22 | 1.8.5           |

## Context

Diagnostic logging is selected by which Makefile target is invoked. The two run
targets differ by exactly one argument:

```make
r:  $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main
rd: $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(PYTHON_RUN) -m wingman.main --log-file wingman.log
```

`--log-file` writes DEBUG records to the named file (console keeps
`--log-level`, default INFO) and rotates the previous log into `logs/` rather
than truncating it.

Research 005 added per-account targets, wired to `rd`:

```make
r1: ensure-virtual-desktop rd
r2: ensure-virtual-desktop rd
```

Three consequences, all accidents of the wiring rather than decisions:

1. **Per-account runs are permanently diagnostic.** There is no per-account
   equivalent of plain `make r`; the choice was made for those targets.
2. **Modes multiply targets.** Offering both modes per account means `r1`/`r1d`,
   `r2`/`r2d` — 2N targets encoding a one-argument difference.
3. **`make r` produces no forensic record at all.** A session run that way
   leaves nothing behind.

Diagnostic logging is a property of the session the operator wants, not of the
account being flown, yet it is encoded in the same dimension as the account.

### What the volume actually is

The original framing assumed DEBUG logging was expensive enough to want off by
default. Measured on the 3h 18m session of 2026-08-22:

| | |
|---|---|
| Log size | 10.0 MB (~3 MB/hour) |
| DEBUG share | 78,988 of 94,444 lines (84%) |
| Write rate | ~6.6 lines/sec |
| `logs/` archive, all time | 298 MB |

Three MB an hour is not a cost worth a mode, and 6.6 formatted lines/sec is not
a plausible per-tick overhead. The premise that made "off" the sensible default
does not hold.

### What the logs are actually for

Every diagnosis in this project's recent history read a DEBUG log: the
ADR 087 blackout chain, the ADR 086 `ttg`/`alt_rate` trigger evidence, the
Performance 008 `anon_mb` heap attribution, and the ADR 088 finding that 4 of 85
dives still completed carrying missiles. None of those were reproducible on
demand — each was a property of a long unattended session that had already
happened.

For that workflow a mode which silently discards the evidence is a hazard, not
a saving.

## Decision

### d1 — The DEBUG log is always written; the mode is removed

`wingman.main` writes the DEBUG log unconditionally. There is no
`diagnostic_mode` config key, no environment variable, and no per-mode target.
Every run target — `r`, `r1`, `r2`, and any future `rN` — produces a log,
so a new account adds one target rather than two.

This deletes machinery rather than adding it: no config key to validate, no
schema entry, no comment-preserving config writer, no toggle that can be left
in the wrong state or committed as `true`.

`rd` is kept as an alias of `r` so existing muscle memory and documentation
keep working.

### d2 — `--log-file PATH` still overrides the default path

The ADR 044/045 replay and live-capture lanes pass `--log-file` explicitly and
must keep their stated paths. An explicit argument continues to win.

Console verbosity remains independently controlled by `--log-level`, which is
unchanged: this decision is about the *file*, which is written at DEBUG
regardless of what the console shows.

### d3 — One log file, identified by a startup banner

**Not** one file per account. Accounts cannot run concurrently — Research 005's
XTest and single-capture-region constraints are unchanged — so the existing
rotation into `logs/<stem>_<mtime>.log` already separates sessions. A second
filename would add a dimension without adding information.

Instead the log identifies itself in its first lines:

```
Wingman 1.8.5 | account=acct1 | prefix=Metalstorm-acct1 | config=wingman/config.yaml
```

Today the account appears only in the session-summary path
(`run_20260822_021823_acct1_stats.json`) at the very end of a 10 MB file — so
identifying a rotated log means reading to its bottom, and **a session that
crashes before the summary has no account marker at all.** A startup banner
costs one line and survives a crash.

### d4 — Every bound is expressed in bytes, not in sessions

`logs/` has no retention policy and holds 298 MB. Always-on logging raises the
rate at which it grows, so d1 requires bounding it — but the bound must not be
calibrated to today's line volume, or it becomes a decision that has to be
revisited the first time a hot path starts logging.

Three bounds, all volume-invariant:

| Bound | Setting | Behaviour if volume grows 10x |
|-------|---------|-------------------------------|
| Single log size | `debug.log_max_mb` (default 100) | Rotates 10x more often; no file is ever larger |
| Archive total | `debug.log_archive_budget_mb` (default 2000) | Keeps 10x fewer archives; disk is unchanged |
| Verbosity | `debug.file_log_level` (default DEBUG) | A dial to turn down, without turning logging off |

A **count** of archives ("keep the last 50") is deliberately rejected: it is the
one policy that fails exactly when volume grows, because it holds sessions
constant and lets bytes float. A byte budget holds the resource constant and
lets history float, which is the correct trade for a disk-bounded archive — you
would rather have three days of history than an out-of-disk error.

Pruning runs at rotation, oldest first, until the archive is under budget.

`debug.file_log_level` is a **dial, not a switch**: it changes how much is
recorded, never whether a record exists. If a future subsystem is chatty, the
answer is to lower this or fix that subsystem's level — not to stop logging.
The always-on property from d1 is preserved at every setting.

### d5 — Log growth is measured, so a volume change is noticed

The RESOURCE line already samples wingman's own resource use every five
minutes. It gains the current log size and its growth rate:

```
RESOURCE ... log_mb=42 d_log=+7 log_rate_mb_h=21
```

Volume growth then shows up in the same place every other resource trend does,
in the session where it started, rather than being discovered as a full disk
weeks later. This is what makes the byte budgets in d4 safe to leave alone:
they bound the damage, and this reports the change.

The session-end RESOURCE SUMMARY reports the same figure, so a session that
logs abnormally is self-flagging.

## Consequences

Every session leaves a forensic record. The class of loss that motivated this —
"the interesting thing happened, and the log was not being written" — is gone.

Disk grows by about 3 MB per hour of operation at today's volume, hard-bounded
by the d4 archive budget regardless of what that rate becomes. At the default
2 GB the archive holds roughly four weeks of continuous operation now, and
proportionally less if volume rises — bytes stay fixed, history shortens.

**`make r` changes behaviour.** It previously wrote no file and now writes one.
That is the intent, but anyone relying on `r` for a no-side-effects run should
know the working tree gains `wingman.log` and a rotation into `logs/`. Both are
already gitignored.

**Pruning deletes data.** d4 removes old archives automatically, which is a
destructive operation on forensic material. The 2 GB default is chosen so that
a weeks-old investigation is still possible at current volume, not to minimise
disk. If volume grows sharply the retained *window* shrinks silently — d5 is
what makes that visible rather than surprising, and raising the budget is the
response.

## Alternatives considered

**Global config boolean, toggled by `make rd`** (the original proposal for this
ADR). Delivers mode-independent-of-account, but puts session state into a
tracked file: `make rd` dirties the working tree and `diagnostic_mode: true`
can be committed by accident, leaving DEBUG on for everyone until noticed. It
also needs a schema entry and a comment-preserving writer for the comment-dense
`config.yaml`. Rejected once measurement showed there is no volume problem for
the toggle to solve — the machinery has a cost and the thing it manages does
not.

**Environment variable `WINGMAN_DEBUG`.** Mechanically clean and composes with
the existing `WINGMAN_ENV` exactly as `WINGMAN_ACCOUNT` does. Rejected for the
same reason: it is a flag spelled differently, and there is no longer a reason
to want the flag.

**Untracked `config.local.yaml` overlay.** The best version of the config-key
idea — persistent mode without git noise. Rejected as the same unnecessary
machinery, but it remains the right answer if an off switch is ever genuinely
needed.

**One log file per account.** Rejected per d3: accounts are sequential, so
rotation already separates them, and the startup banner supplies the identity a
filename would have carried.

**Retention by archive count (\"keep the last 50\").** The obvious policy, and
the one this ADR carried in its first draft. Rejected because it is calibrated
to current line volume: it holds *sessions* constant and lets *bytes* float, so
the first chatty subsystem multiplies disk use without changing the policy's
appearance of correctness. That is precisely the kind of decision that has to
be undone later, which is what d4 exists to avoid.

## Validation

**V1 — every run target logs.** `make r`, `make r1`, `make r2` each produce
`wingman.log` containing DEBUG records, with the previous log rotated into
`logs/`.

**V2 — precedence holds.** `make tp` writes to the paths its lanes specify;
ADR 044/045 validation is unaffected.

**V3 — a rotated log is self-identifying.** The first lines of any archived log
name the version and account, including for a session killed before its
summary.

**V4 — the archive stays under budget in bytes.** After enough rotations to
exceed `log_archive_budget_mb`, `du -sm logs/` remains at or below the budget
and the oldest archives are the ones gone.

**V5 — the bounds survive a volume change.** Artificially raising log volume
(for example by lowering `file_log_level` to DEBUG on a chatty subsystem)
changes how *many* archives are kept and how often rotation occurs, but not the
bytes on disk. `log_rate_mb_h` on the RESOURCE line reflects the new rate.

## References

- Research 005 — multi-account run targets; the sequential-only constraint d3 relies on
- ADR 044 / ADR 045 — replay and live-capture lanes that pass `--log-file`
- Performance 008, ADR 086, ADR 087, ADR 088 — diagnoses that depended on a DEBUG log already existing

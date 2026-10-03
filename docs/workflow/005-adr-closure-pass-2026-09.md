# Workflow 005 — ADR Closure Pass, September 2026

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-27 | 1.8.12          |

## Purpose

[Code review 018](../code-review/018-2026-09.md) CR-018-18 found 45 of the 51 ADRs
numbered 100 to 150 still `Draft`, including shipped ones. A `Draft` ADR may be
amended in place indefinitely, so the no-modify rule for `Accepted` records protects
almost nothing recent.

This pass does not change any ADR's status. `CLAUDE.md` makes `Accepted` a review
outcome, and a wrong flip locks a record against correction. It lists the evidence
for each ADR so the operator can accept the ready ones in one sitting.

## Method, and what it cannot see

For each ADR from 100 to 150, a script recorded:

- its status;
- how many times `wingman/*.py` cites it (`ADR NNN`), as a sign it shipped;
- how many open markers its `## Validation` section holds ("not yet observed",
  "pending", "not yet", "open", "TODO"), or that it has no such section.

This is a first pass by keyword. A citation shows the code refers to the ADR, not
that every decision in it shipped, and a Validation section can be complete without
saying so in words the scan knows. Each recommendation below still needs the
operator's read of the ADR.

## Recommendations

### Accept: cited in code, no open validation markers

| ADR | Citations | Lines | Note |
|-----|----------:|------:|------|
| [100](../adr/100-repository-growth-and-generated-artifacts.md) | 1 | 334 | |
| [101](../adr/101-boundary-aware-climb.md) | 4 | 211 | |
| [109](../adr/109-eject-yields-to-the-survival-hold.md) | 7 | 142 | |
| [111](../adr/111-the-hold-pre-empts-the-running-mission.md) | 9 | 107 | |
| [117](../adr/117-capture-what-blindness-looks-like.md) | 28 | 503 | long: move trial evidence out when accepting |
| [121](../adr/121-a-hung-shutdown-must-leave-evidence.md) | 10 | 475 | long: as above |
| [126](../adr/126-the-turn-was-flying-a-circle.md) | 1 | 166 | its 5 s cap was reverted by ADR 127: accept as superseded, not as live |
| [133](../adr/133-a-shorter-fragment-corroborated-by-the-void.md) | 4 | 232 | |
| [134](../adr/134-cruise-afterburner-above-fuel-floor.md) | 5 | 284 | |
| [144](../adr/144-mission-su30-scripted-sequence.md) | 24 | 385 | |

### Keep as Draft: validation still open

The Validation section names an unmet check, usually a live one. Accept each when
that check is recorded.

102 (1 open), 103 (1), 105 (2), 107 (2), 108 (2), 110 (1), 112 (1), 113 (1), 114 (1),
115 (2), 116 (1), 118 (1), 120 (1), 122 (1), 123 (1), 124 (1), 125 (1), 127 (1),
128 (2: V8 survival split, V12 emergency-window check), 135 (2), 138 (1), 143 (3),
145 (1), 150 (2).

### Review by hand: no Validation section

The scan cannot judge these. Several are large and shipped, so they are the likeliest
to be ready once their evidence is read:

106 (the return-to-battle tracking record: stays `Draft` while rows are added),
136 (483 lines), 137 (1,492 lines), 140 (834 lines), 142, 146, 147, 148, 149.

### Not cited in code

104 and 119 are not referenced from `wingman/`. 104 concerns the nested display's
launch flags and 119 a bounded display probe, so their code may live in `scripts/` or
the Makefile. Confirm where before accepting.

## For new ADRs

`CLAUDE.md` now carries the shape CR-018-18 recommends: the decision and its
consequences on the first page, live-trial evidence in `docs/anomaly/` or
`docs/performance/` documents that the ADR links, and a superseding ADR, rather
than appended sections, once a decision is live.

# ADR 082 — Spawn-Crash Instrument: Minimum Window and Immediate-Redetect Split

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-19 | 1.8.4           |

## Context

ADR 076 defined the spawn-crash instrument as *a death within 10 s of a
post-respawn mission restart* — the before/after measure for the
spawn-attitude guard. It has an upper bound but no lower bound.

The 2026-08-19 12-hour session (102 missions, 267 respawns) reported **22
spawn crashes**. Their timings:

```
0.0, 0.0, 0.0, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.2, 0.2, 0.2, 0.2, 0.2,
0.5, 0.6, 1.2, 1.3, 1.3, 1.4, 1.4, 2.5
```

Median 0.2 s; 17 of 22 under 0.6 s; none above 2.5 s.

None of these can be the anomaly the instrument exists to measure. The
aircraft respawns **airborne with forward speed**; ADR 076's premise was
that flying straight ahead from a bad spawn "for a few seconds results in
crashing" — a physical lower bound of several seconds. A death recorded
0.1 s after the restart is a second `respawn_detected` event arriving on
the heels of the restart stamp, not an aircraft hitting terrain.

So the instrument is reporting **22 false positives and zero true
positives** on a session with, by this reading, no genuine spawn crashes
in 267 respawns. Worse, it reports them under a name that implies the
ADR 076/078 guard is failing, which would misdirect exactly the decisions
the instrument was built to inform.

**The sub-floor events are not merely noise.** Each `respawn_detected`
runs the full respawn flow — `cancel_mission()`, spawn-guard start,
restart-on-health — so 22 of them means 22 freshly restarted missions
were cancelled and restarted again. The session still finished 102/102
missions, so the churn is not fatal, but it is real behavior and its
mechanism is unknown: that session logged to console only
(`make r`), so no log survives to diagnose it. Discarding these events
silently would erase the only remaining evidence that it happens.

## Decision

### d1 — A minimum window: `_SPAWN_CRASH_MIN_S` = 3.0 s

A death counts as a spawn crash only when it falls in
**[3.0 s, 10.0 s]** after a post-respawn restart. The lower bound encodes
the physics the instrument measures: an airborne spawn cannot reach
terrain faster. 3.0 s sits above the entire observed artifact
distribution (max 2.5 s) with margin below ADR 076's "a few seconds"
failure mode.

### d2 — Sub-floor events are counted, not dropped

Deaths inside the window but under the floor are recorded separately as
**`immediate_redetects`**, with their timings, in the same stats block and
the session summary. They are a distinct signal — respawn re-detection
churn — and the codebase rule against silent truncation applies: a
measurement that quietly discards inputs reads as "clean" when it is not.
Their presence is what will let a future logged session find the cause.

### d3 — The instrument keeps its name and its role

`spawn_crashes` remains the ADR 076/078 acceptance measure, now
measuring only what it claims to. The historical readings it produced
(0 across the 2026-08-17/18 sessions) are unaffected: those sessions
recorded no events at any latency, so the floor changes nothing
retroactively.

## Consequences

- The instrument stops reporting guard failures that did not happen. On
  the 12-hour session it would read **0 spawn crashes, 22 immediate
  redetects** — the same data, correctly named.
- The redetect count becomes a standing signal for the respawn-flow churn
  described above; if it stays non-zero, the next logged session
  (`make rd`) should trace `respawn_detected` timing against
  `restart_last_mission` to find the source.
- A genuine spawn crash at, say, 2.8 s would now be booked as a redetect.
  Accepted: the artifact class sits an order of magnitude below the floor,
  and a real crash that fast would also be visible as a redetect rather
  than lost.
- No new events enter the replay/capture streams; the change is confined
  to `MissionStatsTracker` accounting and its reported block.

## Verification

- Unit tests: a death inside [3, 10] s counts as a spawn crash; deaths at
  0.1 s / 2.5 s count as immediate redetects and NOT as crashes; a death
  outside 10 s counts as neither; the restart stamp is consumed exactly
  once per life in every branch; the summary block reports both counts
  (`test_mission_stats.py`).
- `make test` green.
- Live: next logged session reports the split, and its redetect count and
  timings match the log's `respawn_detected` / `restart_last_mission`
  sequence.

## References

- ADR 076 — spawn-attitude guard and the original instrument definition
  (window only; superseded here by the windowed-with-floor definition)
- ADR 078 — pulsed guard and telemetry handoff (the acceptance decisions
  this instrument informs)
- ADR 079 — the precedent for this failure class: a queued respawn event
  landing just after a restart, counted as a sub-second "crash"
- 2026-08-19 05:41 session stats — the 22-event distribution above

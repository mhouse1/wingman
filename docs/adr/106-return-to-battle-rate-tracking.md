# ADR 106 — RETURN TO BATTLE Rate Tracking

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-03 | 1.8.8           |

## Context

Design 010 instrumented the map boundary; ADR 101 added the first thing that
acts on it. Both changes are judged by one outcome: how often the aircraft
leaves the arena and the game shows its RETURN TO BATTLE banner.

That number has been measured ad hoc, per session, in conversation. The
measurements are not comparable unless the counting rule is fixed, and they are
not durable — `wingman.log` opens with `mode="w"` and is destroyed by the next
run, so a session's figures exist only if someone wrote them down. Two of the
three sessions below can no longer be re-derived from a log.

This ADR fixes the counting rule, records the series, and states what would
count as done.

## Decision

**D1. The tracked metric is confirmed crossings per mission.**

Per mission, not per hour or per session: sessions run from 3 to 10 hours and
missions vary from 4 to 30 minutes, so raw counts are not comparable. Missions
are the unit of exposure — each one is an opportunity to fly out of the arena.

**Confirmed** means OCR read the banner. The colour trigger alone is 94% false
positives (125 of 133 in the 2026-09-02 session), so counting triggers would
measure the detector's noise rather than the aircraft's behaviour. ADR 103 D8
already moved the count behind the OCR arbiter for this reason.

> **Revised 2026-09-06 — a crossing with an empty rack does not count.** The
> metric exists to score `BoundaryTurn`, and with an empty rack the tactic is
> structurally unable to act. `is_idle` is `game_state != GAME_BATTLE` and Idle is
> child 0 of a `memory=False` selector, so from the moment missiles run dry and
> the FSM enters `GAME_BATTLE_EJECT`, **every** tactic below Idle — BoundaryTurn
> included — is unreachable. `eject_and_dive` commands pitch and afterburner
> only; it has no heading control, so the aircraft holds whatever course it had.
>
> That is intended: Eject exists to trade an empty airframe for a rearmed one,
> and dying is the point. But an exit that occurred while nothing could have
> turned is not evidence about the turn. Observed 2026-09-06 10:21: the edge
> closed 0.814R to 0.103R over thirteen seconds, six of them inside
> `turn_frac` 0.50 with the boundary dead ahead, and the tree read `Idle`
> throughout.
>
> **The tracked metric is therefore crossings per mission with missiles
> available.** Measured effect on rows whose logs still exist: the 2026-09-06
> 04:28 session drops from 5 crossings to **2** (3 had an empty rack); the
> 2026-09-05 night session is unchanged at 7 (0 had an empty rack). Rows whose
> logs have rotated away cannot be re-derived and are left as recorded — the
> denominator is unaffected, so they remain readable, but a row predating this
> revision is an upper bound rather than a like-for-like figure.
>
> This does **not** rescue the flat series. Pooled post-update, the largest
> cohort loses none of its crossings, so the reading in "nine sessions on, the
> series is flat" stands.

**D2. The figures come from a fixed set of commands**, so entries added months
apart stay comparable:

```bash
grep -c 'RETURN TO BATTLE (confirmed crossing'  wingman.log   # crossings
grep -c 'colour trigger not confirmed'          wingman.log   # unconfirmed triggers
grep -c 'BOUNDARY TURN — banking and pulling'   wingman.log   # turns (= actuated)
grep -c 'boundary turn — 12s cap reached'       wingman.log   # turns that hit the cap
grep -A6 'Wingman Session Summary'              wingman.log   # missions started
```

> **Corrected 2026-09-06 — three of these commands had gone stale, and a stale
> command reads as a zero.** Recording the 2026-09-05 (night) row with the
> original strings gave Turns 0, Actuated 0, Budget 0 for a session that in fact
> turned 134 times. The messages had been renamed: `MAP BOUNDARY: turn requested`
> and `map boundary ahead, rolling away` are now the single line
> `BOUNDARY TURN — banking and pulling away from the edge`, and `turn budget
> spent` is now `boundary turn — 12s cap reached`.
>
> This is D2's own failure mode. A fixed command set keeps rows comparable only
> while the strings still match the code, and nothing fails loudly when they stop
> — the grep returns 0 and 0 is a plausible number. **Any row whose Turns column
> is 0 while crossings are non-zero should be re-derived, not believed.** Turns
> and Actuated are now necessarily equal because one message covers both; the
> separate Actuated column is kept for the older rows and should be read as "same
> value" from 2026-09-05 (night) onward.

**D3. Record the code state with every row.** The point of the series is to
attribute movement to a change. A row without the ADR revision that was live is
a number nobody can act on.

**D4. Copy the row out of the log before the next run.** `wingman.log` is
truncated on every start. A session not recorded here is a session lost.

**D6. Capture the frame on every confirmed crossing.** The counts say how often;
they say nothing about where. The suspicion worth testing first is that
crossings cluster on particular maps — a map whose arena edge sits close to the
action, or whose terrain reads differently to the boundary detector, would
produce a rate that has nothing to do with wingman's code.

The whole frame is saved, not the minimap crop: a map is identifiable from its
terrain and scoreboard, not from a 320 px disc. Frames land in
`test_screenshots/unknown_anomalies/` as `rtb_<timestamp>_crossing<n>.png`,
alongside the ADR 074 anomaly captures, and the folder is gitignored.

**Confirmed only, and capped.** Capturing on the colour trigger would bury the
eight frames that matter under a hundred that do not, at 94% false positives.
`boundary_capture_max` (20) bounds a bad night: the folder is gitignored, but the
frames are ~2 MB each and the disk is not. The cap suppresses the capture, never
the count — the count is the metric.

Losing a frame must never lose a crossing. The writer runs on an OCR pool
thread, where an exception is swallowed, so it never raises.

## The series

> **2026-09-04: the metric is input-limited.** In `GAME_BATTLE` the detector
> produced a reading on 385 of 682 ticks — **56%**. (An earlier note here said
> 23%; that averaged in lobby and loading ticks, where there is no minimap to
> read and None is correct. See ADR 117.) Every row below measures a tactic that is
> blind on nearly half its battle ticks, so differences between rows are influenced by
> when the detector happened to see something. ADR 117 captures the failure
> case; until those frames are read, no row here should be attributed to a
> tactic or detector change.

Crossings per mission is the column that matters; the rest are there to explain
its movement.

| Date | Dur | Missions | Crossings | **per mission** | Turns | Actuated | Budget spent | Unconf. | Code state | Game UI |
|------|----:|---------:|----------:|----------------:|------:|---------:|-------------:|--------:|------------|---------|
| 2026-09-01 (day) | 4h30m | 40 | 4 | **0.10** | 36 | 18 | 0 | 91 | ADR 101 rev 1 | pre-update |
| 2026-09-01 (night) | 9h42m | 95 | 32 | **0.34** | 201 | — | 19 | 262 | ADR 101 rev 1 | pre-update |
| 2026-09-02 | 7h21m | 77 | 8 | **0.10** | 58 | — | 11 | 125 | ADR 101 rev 2 | **post-update** |
| 2026-09-03 | 43m | 7 | 1 | **0.14** | 14 | **4** | 3 | 59 | ADR 101 rev 2 | post-update |
| 2026-09-03 (pm) | 7h10m | 74 | 8 | **0.11** | 65 | **65** | — | 232 | **ADR 107** | post-update |
| 2026-09-03 (eve) | 1h29m | 15 | 0 | **0.00** | 38 | 38 | — | — | ADR 107 + **108** | post-update |
| 2026-09-03 (night) | 4h22m | 46 | 12 | **0.26** | 150 | 150 | — | — | ADR 107 + 108 | post-update |
| 2026-09-04 | 4h26m | 44 | 6 | **0.14** | 129 | 129 | — | 92 | ADR 107 + 108 rev 2 | post-update |
| 2026-09-04 (eve) | 56m | 9 | 2 | **0.22** | 19 | 19 | — | — | ADR 111 + **112** | post-update |
| 2026-09-04 (late) | 49m | 10 | 1 | **0.10** | — | — | — | — | ADR **113** + 114-116 | post-update |
| 2026-09-04 (night) | 2h21m | 21 | 2 | **0.10** | 83 | 83 | — | — | ADR 113-117 | post-update |
| 2026-09-05 (am) | 1h55m | 18 | 3 | **0.17** | 32 | 32 | — | — | ADR 118-121 | post-update |
| 2026-09-05 (mid) | 24m | 3 | 2 | **0.67** | 17 | 17 | — | — | ADR **122** + 123 | post-update |
| 2026-09-05 (soak) | **6h16m** | **64** | 6 | **0.094** | 169 | 169 | — | — | ADR 122-125 | post-update |
| 2026-09-05 (pm) | 2h28m | 25 | 4 | **0.160** | 167 | 167 | — | — | ADR 126 (5s cap, **reverted**) | post-update |
| 2026-09-05 (eve) | 1h02m | 11 | 2 | **0.182** | 59 | 59 | — | — | ADR 127 (12s cap restored) | post-update |
| 2026-09-05 (night) | **4h36m** | **48** | 7 | **0.146** | 134 | 134 | 57 | 224 | ADR 127 + 128 | post-update |
| 2026-09-06 (day) | **3h52m** | 32 | 1 | **0.031** | 64 | 64 | 36 | 32 | ADR 130 + 131 | post-update |
| 2026-09-06 (eve) | 3h10m | 32 | 7 | **0.219** | 129 | 129 | 75 | 82 | ADR 130 + 131 | post-update |
| 2026-09-06 (overnight) | **10h55m** | **113** | 13 | **0.115** | 332 | 332 | 162 | 476 | ADR **132** | post-update |
| 2026-09-07 (am) | 1h52m | 20 | 3 | **0.150** | 77 | 77 | 49 | 91 | ADR 132 | post-update |
| 2026-09-07 (mid) | 45m | 7 | 0 | 0.000 | 7 | 7 | 2 | 2 | ADR **133** | post-update |
| 2026-09-07 (pm) | 2h52m | 29 | 5 | 0.172 | 118 | 118 | — | — | ADR 133 | post-update |
| 2026-09-07 (18:14) | 12m10s | 2 | 0 | 0.000 | 0 | 0 | 0 | 1 | ADR 133 + **134** D8 (WIP) | post-update |
| 2026-09-07 (18:46) | 4m37s | 1 | 0 | 0.000 | 1 | 1 | 0 | 1 | ADR 133 + 134 D8-D9 (WIP) | post-update |
| 2026-09-07 (19:00) | 33m07s | 6 | 0 | 0.000 | 0 | 0 | 0 | 1 | ADR 133 + 134 D8-D10 (WIP) | post-update |
| 2026-09-07/08 (overnight) | **8h57m** | **92** | 24 | **0.261** | 394 | 394 | 170 | 209 | ADR 133 + 134 D8-D10 | post-update |
| 2026-09-08 (04:53) | 2m28s | 0 | 0 | n/a | 0 | 0 | 0 | 0 | ADR 134 D8-D10 + SIGHUP fix; never reached GAME_BATTLE | post-update |
| 2026-09-08 (05:00) | 20s | 0 | 0 | n/a | 0 | 0 | 0 | 0 | ADR 134 D8-D10 + SIGHUP fix; the standby Ctrl-C race session (ADR 121) | post-update |
| 2026-09-08 (05:18) | 26m34s | 4 | 0 | 0.000 | 2 | 2 | 0 | 6 | ADR 134 D8-D10 + SIGHUP fix + standby Ctrl-C race fix | post-update |
| 2026-09-08 (soak) | **13h05m** | **134** | 20 | **0.149** | 367 | 367 | 173 | 349 | ADR 134 D8-D10 + SIGHUP fix + standby Ctrl-C race fix | post-update |

**Actuated** counts turns that reached the aircraft — `grep -c 'map boundary
ahead, rolling away'`. Added 2026-09-03, when the gap became visible: 14 requests
produced 4 rolls, because ADR 101 D7 scoped the turn to a *running climb* and no
climb was running for the other ten. That is the column ADR 107 exists to close,
and it is measured on wingman's own behaviour rather than on an outcome the game
shares.

*Provenance: the two 2026-09-01 rows were measured live at the time. Those logs
have since been overwritten and the figures cannot be re-derived — which is what
D4 exists to prevent. Their **Actuated** cells are blank for the same reason: the
count was not taken while the log existed.*

**The 2026-09-03 row is 7 missions and one crossing.** At that size the rate is
one event divided by a small number, and it belongs in the table as the last
pre-ADR-107 reading rather than as evidence of anything. It is listed because a
row not written before the next run is a row lost, not because 0.14 means
something.

**D5. Record the game UI version too.** MetalStorm shipped a minimap change
shortly before the 2026-09-02 session — `test_screenshots/AMMO_MISSILE.png` and
`AMMO_MISSILE_1.png`, captured at 19:14 and 19:21, thirteen minutes before that
session started at 19:27. The game is an input to this measurement and it moves
without warning, so a row that names only wingman's code state is not
attributable.

### Reading the series so far

**Three points are not a trend, and the third one is confounded.**

The rev 2 session came in at 0.10, a 69% drop from the night session that
exposed the rev 1 defect. Two separate reasons not to bank that:

**The pre-fix day session was also 0.10.** Under identical rev 1 code the rate
varied 0.10 to 0.34, so between-session variance is at least as large as the
effect being claimed. Map, opponent mix and session length all differ.

**The game UI changed in the same gap.** ADR 101 rev 2 and MetalStorm's minimap
update both landed between the night session and 2026-09-02, so the two cannot
be separated by these rows. The minimap is the detector's only input, which
makes this a change to the measurement instrument at the same moment as a change
to the thing being measured.

What *is* direct evidence for rev 2 is the 64% fall in turn **requests** (2.12 to
0.75 per mission). That is the chattering hold being fixed, and it is measured on
wingman's own behaviour rather than on an outcome the game shares. The crossing
rate is not yet demonstrated either way.

### What changed in the minimap

From the two screenshots, against the archived `MINIMAP.png`:

| | before | after |
|---|---|---|
| terrain | dark monochrome grey | coloured — tan land, blue water |
| field-of-view wedge | pale grey | green |
| rim | plain | coloured arcs |
| rotates with heading | yes | yes (unchanged) |

Enemy-contact detection survives: the archived frame still reproduces its
hand-verified 2/0/3 ring occupancy, and both new frames return plausible counts
(5/0/0 and 0/1/1). Ring binning keys on the red icons, which did not change.

**The boundary detector is the exposure.** It keys on hue 8-28 with saturation
and value at or above 120, and its docstring justifies that as map-independent
because "the boundary is a HUD overlay drawn at a constant colour - hue 16.9-18.5
while the map background ranged V 66.6-118.4". That premise was written against a
dark grey background. The new background contains tan terrain in the same hue
family.

On these two frames the saturation and value thresholds still exclude it — the
mask covers 0.3% of the crop, largest component about 100 px — so the detector is
not obviously broken. But neither frame is near an arena edge, so `detect_map_boundary`
returns None for both AND for the old frame, and nothing here exercises the case
that matters. The margin that made the mask map-independent is smaller than it
was, and untested.

### What the residual looks like

Six of the eight 2026-09-02 crossings were under Climb — the case ADR 101
targets. In five of six budget exhaustions the distance at give-up equalled the
closest approach of that turn: after a full 8 s of rolling, the aircraft was no
nearer to escaping than when it started.

Two readings fit that equally, and the current logging cannot separate them:

- the roll is not reaching the aircraft, or rolling during a climb does not turn
  the flight path enough;
- the roll IS turning it, but `dist` is the range to the nearest point on a
  boundary *line*, so heading changes first and distance only follows once the
  velocity vector points away — and 8 s expires in between.

Raising `boundary_turn_max_s` is the obvious next move and would be a guess
until that is settled. The discriminator is cheap: the trace buffer dumps only
on a **confirmed crossing**, so the ~50 turns per session that succeed are
invisible. Logging `dist` and `fwd` at turn release would make them visible.

## Consequences

Every session that runs the boundary instrumentation owes a row here. That is a
small manual cost, and it is the price of the log being destroyed on each run.

The series will read poorly for a while. Three points cannot separate a 3x
change from ordinary variance, and pretending otherwise is how a tuning change
gets adopted on noise.

### ADR 107, first full session

The two claims came apart exactly as the section below anticipated.

**The mechanical claim held.** 65 turns engaged, 65 actuated — 100%, against 29%
and 50% on the rows above. The 10-of-14 delivery gap ADR 107 was written to close
is closed.

**The outcome claim did not.** 0.11 crossings per mission against a 0.10
baseline: no movement at all.

So the tactic now fires every time and does not work. Two measurements say why
it is the geometry and not the timing:

- **46% of turns still ran to the 12 s cap** — the aircraft does not recede.
- Where a turn was running, range went **0.45R to 0.06R** and **0.27R to 0.03R**.
  Bank *and* pull, and it closed anyway. ADR 107 D2 argued adding pitch was what
  would make the turn effective; on this evidence it is not.

A second and separate gap: only **3 of 8** crossings had a BoundaryTurn anywhere
in the preceding 20 ticks. Five had no turn at all in the last ~30 seconds —
crossed too fast to catch, no reading, or suppressed by the D4 emergency yield.
That is a different question from why a running turn fails, and worth keeping
apart from it.

### ADR 108, and why the 0.00 row means nothing

The 1h29m row read **zero crossings in 15 missions** and was tempting. The 4h22m
row that followed, on identical code, read **0.26** — inside the pre-existing
0.10-0.34 band.

At a 0.10 baseline, 15 missions expects about 1.5 crossings, so zero is well
within chance. This is the third time the table has shown a small sample
flattering a change, and it is why the target below asks for five sessions
rather than one good one.

What the larger session did establish is where the remaining failure is. Nine of
its twelve crossings happened with **BoundaryTurn actively running**, and all
twelve had a turn in the preceding 20 ticks — so the tactic is present and
firing. The release rule was the defect: over 105 turns the median release was
0.34R, 40% released inside 0.30R and 27% inside 0.20R, one of them at 0.06R.
Recession alone was letting go of aircraft still on the edge. ADR 107 D5 now
carries a hysteresis band.

Detection itself is healthy after ADR 108 — 31% readable with a median reading
of 0.42R, against 19% and a median of 0.07R when the detector was reading desert
terrain.

### 2026-09-04 — the tactic is break-even, on 61 measured turns

0.14 crossings per mission, inside the 0.10-0.34 band the metric has occupied
since before ADR 107 existed. Six sessions of work on this tactic have not moved
it out of that band.

With the sampler corrected, the turn could finally be measured. Pairing the
controller's attitude summary with the handler's range line — adjacent in the
log, no new instrumentation needed — over 61 turns:

| | |
|---|---:|
| median swing | 27 deg |
| median range gained | **+0.00R** |
| turns gaining 0.10R or more | 13 |
| turns losing 0.10R or more | 15 |

Near-symmetric. The aircraft rotates hard and the range does not respond. See
ADR 107's negative-result section, including two readings retracted there.

**Crossings clustered rather than arriving steadily:**

```
02:13  02:30  02:52  02:58  ·········(2h13m)·········  05:11  05:36
```

Four in 45 minutes, then two hours clean, then two in 25. That shape argues the
rate is driven by circumstance — map, opponents, where the fighting happens —
more than by per-tick tactics, and it is the strongest support yet for the map
question below.

**The evidence needed to test it was not captured.** All six crossings were
suppressed by the shared capture cap, which had already filled with approach
frames: 18 approach frames saved, 0 crossing frames, 125 suppressed. ADR 108 D4
named this risk and then implemented a single FIFO counter, which gives the
frequent event priority — the opposite of what it argued for. Fixed by giving
crossings a reserved budget.

### After ADR 107

The rows above are the pre-107 baseline. ADR 107 replaces the roll-during-climb
with a first-class tactic that owns pitch and roll, so two columns should move
together if it works:

- **Actuated** should approach **Turns**. A tactic is selected on its own
  condition, so the ten requests that reached no aircraft on 2026-09-03 have no
  equivalent — that is the mechanical claim, and it is the one to check first.
- **Crossings per mission** should fall. That is the outcome claim, and it needs
  the five sessions the target below asks for, because the observed spread under
  fixed code was already 3.4x.

If Actuated tracks Turns and the crossing rate does not move, the tactic is
firing and not working — which would point at the turn geometry rather than at
when it fires, and is worth knowing separately.

### The map question

Untested. The hypothesis is that the residual crossings concentrate on a subset
of maps, in which case the next move is per-map tuning or a per-map turn
threshold rather than more work on the turn itself. D6 exists to answer it, and
the answer needs a handful of sessions' worth of frames before it means
anything — eight per session is not a sample.

Two things to look for once frames accumulate: whether the same terrain recurs,
and whether the crossings on a given map share an approach geometry (the trace
is in the log beside each capture).

### The metric's resolution limit (2026-09-07)

Something that should have been computed on day one. At the pooled post-update
rate of 0.132 crossings per mission, and the observed ~10.4 missions per hour:

| Missions | Hours | Expected crossings | 95% CI on the rate | Smallest drop detectable |
|---:|---:|---:|---:|---:|
| 40 | 3.8 | 5.3 | 0.019 - 0.245 | **85%** |
| 113 | 10.9 | 14.9 | 0.065 - 0.199 | **51%** |
| 250 | 24 | 33 | 0.087 - 0.177 | 34% |
| 500 | 48 | 66 | 0.100 - 0.164 | 24% |
| 1000 | 96 | 132 | 0.109 - 0.155 | 17% |

**Even the best-powered row in this series — the 113-mission overnight soak —
can only resolve a halving.** A 24% improvement would take about forty-eight
hours of flying to see. The 40-mission floor this ADR sets is enough to stop a
row being pure noise; it is nowhere near enough to attribute a change.

That reframes the whole series. "Nine days and the rate has not moved" is true,
and it has always been compatible with real improvements of up to about half the
rate passing undetected. The series is a **regression alarm** — it would catch a
doubling — and it is not, and never was, an instrument for judging a change.

Changes to boundary handling should be judged on denser signals from the same
flying. The turn-outcome trace gives 173 samples where this table gives 13, and
`make turn-outcome` reports it. ADR 133's V7 was re-specified onto it for exactly
this reason.

Rows continue to be recorded here under D4 — the series still catches
regressions, and the denominator is needed to normalise anything else.

### 2026-09-07 (mid) — first row with ADR 133, and far too small to read

Seven missions. The row is recorded because D4 requires it and omitting sessions
would bias the series, **not** because 0.000 crossings per mission means anything
at seven missions — the floor is 40, and the same code produced 0.031 and 0.219 on
32-mission samples three days ago.

Two things about it are worth writing down anyway.

**The relaxed path was live, and it was verified rather than assumed.** Config and
analyzer were last modified at 10:01-10:02 and the session began at 10:26. The
check that settles it: the seven blind frames this session captured still return
None when re-run through the current detector, so they are genuine misses under
the new rule and not frames the old rule discarded.

**Live readability fell, and it is not a regression.** 34% on the 09:32 session
against 15% here. Both the code and the session differ, so the comparison is
confounded in two directions at once; turns also fell from 2.9 per mission
(overnight) to 1.0 here, which is what less time near an edge looks like. The
controlled evidence for ADR 133 is the corpus measurement — 86% to 91% recall on
88 banner-confirmed crossings, and 161 of 509 archived blind frames now reading —
not a between-session readability figure. **inferred**, on the session-variance
reading; **measured**, on the corpus.

ADR 133's V7 is not satisfied by this session and needs a run with enough
missions to compare turns per mission and crossings per mission against the
series.

### 2026-09-06/07 overnight — the best-powered row yet, and the series still has not moved

An 10h55m unattended soak, **113 missions** — the largest single sample in the
series and the first row comfortably clear of the 40-mission floor. It is also
the first row run with ADR 132's turn guard live.

| | |
|---|---:|
| Crossings per mission | **0.115** |
| Post-update pooled, excluding this row | 0.134 |
| Post-update pooled, including it | **0.131** |

**0.115 is unremarkable.** Against the seven rows with 40 or more missions —
0.104, 0.108, 0.261, 0.136, 0.094, 0.146, 0.115 — it sits mid-pack, above the
best figure the series has recorded (0.094 on 2026-09-05) and well inside the
range those rows already occupy. Pooling it moves the post-update series from
0.134 to 0.131, which is nothing.

That is the correct expectation, not a disappointment: **ADR 132 is a circling
fix, not a crossing fix.** It stops the aircraft orbiting its spawn point; it was
never argued to reduce crossings, and it did not.

The two rows either side of it are a warning about reading small samples. The
same day produced **0.031** (32 missions) and **0.219** (32 missions) on
essentially the same code — a sevenfold spread from sampling alone. The 0.031 row
was written up here as "not yet believable"; the 0.219 row that followed settles
that, and the 113-mission row lands between them near the long-run average. Any
future row under 40 missions should be read the same way.

Nine days, 649 post-update missions, and the crossing rate has not moved.

### 2026-09-06 (day) — the lowest row in the series, and not yet believable

0.031 crossings per mission: one crossing in 32 missions, the lowest figure the
series has recorded and a third of the previous best (0.094). It is the first row
scored under the revised D1, and no crossing in it had an empty rack, so the
revision did not produce the number.

**It should not be believed yet, for three reasons.**

*The sample is under-powered.* 32 missions is below the 40 this ADR treats as the
floor for a rate, and the numerator is **one**. A single crossing either way moves
the row from 0.031 to 0.000 or 0.062 — a third of the series' whole range — so the
row carries almost no information on its own.

*Exposure fell with it.* Turns dropped 134 to 64 and unconfirmed colour triggers
224 to 32, on a session only 44 minutes shorter. The aircraft was near an edge far
less often, which lowers the crossing count without anything about the tactic
having improved. Whether that is map mix or something else is not established
here.

*Nothing that shipped should have moved it.* The changes live in this session were
diagnostic or logging only — blind-frame capture coverage, takeover attribution,
the expected-teardown flag — plus ADR 130 and ADR 131, neither of which touches
boundary handling. A row that improves threefold with no plausible mechanism is a
row to be suspicious of, not one to bank. Per this ADR's own standing note, a
session that flatters recent work deserves more scrutiny, not less.

Treat it as one point. It takes another two or three sessions at this rate before
anything can be said.

### 2026-09-06 — nine sessions on, the series is flat

The 09-04 entry above called the tactic break-even on 61 turns and noted that
six sessions had not moved the metric out of its band. With three more sessions
and the 4h36m run of 2026-09-05 (night), the series is large enough to pool:

| Cohort | Missions | Crossings | Per mission |
|--------|---------:|----------:|------------:|
| Pre game update (09-01) | 135 | 36 | **0.267** |
| Post game update (09-02 onward) | 472 | 64 | **0.136** |
| — of that, 09-02 to 09-03 | 219 | 29 | 0.132 |
| — of that, 09-04 to 09-05 | 253 | 35 | **0.138** |

Read plainly: **the only halving in the whole series sits exactly on the game's
minimap update, and nothing since then has moved.** 0.132 against 0.138 across
472 missions is not a change; it is the same number twice. D5 exists for exactly
this reason — the one large step in the data is the one step we did not make.

This is consistent with, and explained by, the 61-turn measurement above: median
range gained **+0.00R**, 13 turns gaining against 15 losing. A tactic that is
break-even per turn cannot move a per-mission rate however often it fires, and
the sessions since have fired it a great deal more.

**What this does not say.** Turns per mission appears to rise sharply across the
series (0.75 on 09-02 to 2.79 on 09-05 night), but the boundary-turn message was
renamed on 2026-09-03 06:51 in commit `1d593ea`, and the new line merges the
former "requested" and "actuated" messages into one. Counts either side of that
timestamp are not the same quantity, so the apparent rise is **not** safe to read
as "the tactic intervenes more". The crossings string never changed, so the rate
column — the column that matters — is comparable throughout.

**The metric may also be unable to show an improvement.** The banner at the top
of this series still holds: the detector reads on 56% of battle ticks. A tactic
blind on nearly half its ticks is being scored on outcomes it could not have
influenced, and ADR 117 covers the failure case. Flat here is evidence that this
tactic has not helped; it is not yet evidence that the approach cannot.

**Scope.** This series tracks boundary crossings and nothing else. The same
2026-09-05 (night) session recorded 48 of 48 missions click-to-finish, zero spawn
crashes, and missile-engagement survival of 81% with evade against 68% without.
Those are not in this table, and "flat" here is a statement about one tactic, not
about the sessions.

### 2026-09-07/08 overnight — the highest post-update rate in the series, and a new confound

**0.261 crossings per mission, 92 missions — the highest reading since the
pre-update era (0.267)** and well above the post-update band this series has
otherwise occupied (0.10-0.22). 92 missions clears the 40-mission floor, so
this is not automatically dismissible as noise the way the smaller rows are.

It is still one row. The series has already shown a 7x spread session to
session on **identical** code (0.031 and 0.219 the same day, three days ago),
so a single high reading — however well-powered — is not yet a trend. It
needs corroboration the same way every other row here has.

**A new confound, not present in any earlier row: ADR 134 (cruise
afterburner) was live for a full session for the first time**, holding the
aircraft near-continuously above ~90% or below ~40% fuel rather than at
normal throttle. Nothing in ADR 134 touches boundary handling, but sustained
higher speed is exactly the kind of change that could plausibly raise
crossings independent of anything about `BoundaryTurn` itself — a faster
aircraft covers more ground before the turn tactic can react to the same
boundary reading. That is a hypothesis, not a finding; this table cannot
separate "cruise made crossings worse" from "this was a high-variance night"
on one row, the same limitation D5 already states for game-UI changes landing
in the same gap as a code change. Worth watching specifically on the next few
rows, since it is the first genuinely new mechanism this series has not
already accounted for.

### 2026-09-08 — the cruise-afterburner confound did not repeat

The very next full session with cruise afterburner live (13h05m, 134
missions — the best-powered row in the series after the 2026-09-06/07
overnight): **0.149 crossings per mission**, squarely back inside the
long-standing 0.10-0.22 post-update band, not near the 0.261 outlier the
session before it produced. Cruise afterburner was live and, per that
session's own ADR 134 soak data, doing considerably *more* of its job this
time (fuel sitting at 100% fuel dropped from roughly a third of ticks to
6.7%, the direct signature of the ADR 134 D9/D10 fixes actually working) —
so this is not "cruise barely ran." One high reading followed by a normal one
is exactly what the 7x same-code spread already on record predicts; it does
not confirm the hypothesis, but it also gives it nothing further to stand on.
Treat the 2026-09-07/08 row as the high-variance night it always might have
been, not as early evidence of a real effect — the confound stays open only
in the weak sense that one non-repeat cannot retire it, not because this row
supports it.

## Target

**Under 0.05 crossings per mission, sustained across at least five sessions on
the same code.**

Five because the observed spread under fixed code was already 3.4x; fewer rows
cannot distinguish a real improvement from a quiet night. 0.05 is half the
current best-case rate, chosen as a step rather than an aspiration — zero is not
the target, because an aircraft chasing a contact to the edge is sometimes right
to.

This ADR stays Draft until the series supports a conclusion either way.

## References

- [Design 010](../hldd/010-mini-map-detection/010-mini-map-detection-hldd.md) —
  HLDD 010, the boundary instrumentation these figures come from
- ADR 101 — the boundary-aware climb; rev 1 and rev 2 are the code states in the
  table
- ADR 103 D8 — why the count is gated on OCR confirmation rather than the colour
  trigger
- ADR 028 — Regroup, which steers inward but sits below Climb in the selector
- `test_screenshots/AMMO_MISSILE.png`, `AMMO_MISSILE_1.png` — the post-update
  minimap, captured 2026-09-02 19:14 and 19:21
- `test_screenshots/MINIMAP.png` — the pre-update minimap the crops and the
  boundary hue range were calibrated against

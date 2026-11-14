# ADR 168 — Read the Jet Again Through the Wait for a Match

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-01 | 1.9.4           |

Operator, 2026-11-01: "if a user switches jets mid session it can cause wingman to log wrong jet,
this can be fixed by having wingman double check the jet type during wait, this way a user can
switch jets multiple times per session and it'd auto update the logging". This extends
[ADR 167](167-read-each-rounds-jet-from-the-lobby.md), whose D1 and D4 read the jet once, at the
click.

## Decision

**D1. The label is read again through the wait for a match.** In `GAME_WAITING` and `GAME_STARTING`
wingman reads it every `jet_name.recheck_s` (3 s), starting one interval in, after the click's own
read has finished. Each read has a thread of its own and goes through the OCR pool, so the main
loop never waits for it, and only one runs at a time. It runs live only, as the click's does.

**D2. A new jet needs two reads in a row that agree.** One misread must not rename a round's jet.
Only letters and digits count when names are compared: OCR read one jet as both `A-6 INTRUDER` and
`A-6-INTRUDER` on 2026-10-31, and neither is a change from the other.

**D3. A read with no name changes nothing.** The label can be covered for a moment, and a round
under the last jet read beats a round with none. So the wait logs no `JET NAME: not read` line,
which the ingest would take for no jet. The click still clears the last reading (ADR 167 D4), and
the wait can now recover a round whose click read failed.

**D4. A confirmed change is booked as a click's reading is.** It goes through `_note_jet`: a
`JET NAME: <name>` line, the mission's tag and the saved label. A `Jet recheck:` line comes first
and holds the old name. It must not contain `JET NAME: `, because the ingest reads any line that
does as a reading. The ingest needs no change.

**D5. A reading now holds until the next click or the next confirmed change.** This replaces the
first sentence of ADR 167 D4. The rest of that decision stands.

## Why

- A jet read once at the click cannot see a change made after it. The wait is long: a median of
  4.5 s in `GAME_WAITING` and 60 s in `GAME_STARTING`, up to 153 s, over the 319 waits since jets
  were first read.
- A queue does not always start with a click. Of 332 queue entries since jets were first read, 74
  had no `JET NAME` line within 4 s of them, mostly `GAME_STARTING` returning to the lobby and
  queuing again, or `CANCEL` seen with no click. Each of those kept the last reading. Rereading
  covers them without a separate path.
- The label is on screen for all of it. Over one live queue on 2026-11-01, sampled from `:3` once a
  second, all 43 frames from 08:36:50 to 08:37:33 read `F-14 TOMCAT`. The load screen then replaced
  the lobby, with no menu to change jets from, and all 31 frames to battle read nothing, with no
  false name.

## Consequences

- **Cost.** One OCR of one crop every 3 s in the wait, about 20 in a median wait where there was
  one. A read takes 240 ms (median of 262 clicks, p90 289 ms). Nothing else is timed in the wait
  but the 1 Hz `CANCEL` scan, which shares the pool.
- **A late change can be missed.** Two reads take up to 6 s, so a change in the last seconds before
  the load screen books the wrong jet for that round, as it does today. The next click corrects it.
- **The window closes at mission start.** A mission takes its tag when it starts, so a change
  confirmed after that reaches only later rounds. The label had gone 26 s before battle in the
  round sampled, so no read should land there.
- **A misread repeated twice still gets through.** The panel's reader has the same limit
  ([Design 018](../hldd/018-score-per-round-in-grafana-hldd.md), Risks). None of the 43 live frames
  misread, and 262 live reads of 16 jets gave one variant spelling, which D2 covers.
- **Implementation.** `JetRecheck` and `same_jet` in `wingman/jet_name.py`, ticked from
  `wingman/main.py`, and the `jet` property in `wingman/mission_stats.py`.

## Validation

- **V1, tests (met).** `tests/test_jet_name.py` pins D1 to D4, including that a failed read keeps
  the jet, that a spelling is not a change, and that the tick comes before the fallback that can end
  it. `tests/test_mission_stats.py` and `tests/test_round_scores.py` pin the tag and the log line.
- **V2, the real path on real frames (met).** The reader, `_note_jet` and `MissionStatsTracker` ran
  on the frames of the 08:36:50 queue with an SU-27 frame swapped in at t+12 s. The jet stayed
  `F-14 TOMCAT` on the first SU-27 read, changed on the second at t+15 s, held through the load
  screen, and the mission's tag was `SU-27 FLANKER`.
- **V3, live (open).** Change jets during a wait in a real session. Expect a `Jet recheck:` line
  and a `JET NAME:` line within about 6 s, the label kept, and the round under the new jet on the
  dashboard. Whether the game lets a jet change while queued has not been tried: the menu stays on
  screen, but no one has recorded it working. A switch in the lobby after a hand-clicked PLAY is
  also worth checking.

## Trial record

| When | Session | Code | Result |
|---|---|---|---|
| 2026-11-01 08:36 | the operator's `make r`, mobius0, F-14 TOMCAT, frames of `:3` grabbed read-only | none: the running session predates this ADR | The label's timing under Why and the frames V2 used. |

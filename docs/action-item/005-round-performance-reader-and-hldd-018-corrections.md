# Action Item 005 — Round Performance: the Panel Reader and the HLDD 018 Corrections

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-29 | 1.9.3           |

## Request

Operator, 2026-10-29: "review hldd18", then "document the stuff that needs to be resolve in an
action-items doc".

## Where this starts

[Design 018](../hldd/018-score-per-round-in-grafana-hldd.md) charts each round's score from the
end-of-round PERFORMANCE panel. Its figures for 2026-10-27 05:45 to 2026-10-28 03:09 (182 rounds,
118 read, 49,727 points, a mean of 421, a standard deviation of 212) match the stats files exactly.
The review found two things the design says about the data that the logs contradict, and some
design details to change. Every figure below is measured unless marked.

The evidence is the 402 `ROUND PERFORMANCE` lines in `logs/wingman_20261027_*.log`,
`logs/wingman_20261028_*.log` and `wingman.log` (2026-10-27 05:50 to 2026-10-29 01:18): 234 read,
168 not read.

**An unread panel never showed a KDA.** The reader (`wingman/round_performance.py`) reads the KDA
crop first, and reads the score only when the KDA parses. A frame without a KDA costs one OCR call
and a frame with one costs two, so the time per frame tells the two apart:

| | Read | Not read |
|---|---|---|
| Readings | 234 | 168 |
| Frames read | not logged: the line carries no frame count | 4, in every case |
| First click to the log line, median | 1.73 s | 2.24 s |
| Time per frame (inferred from the medians and the fixed 0.5 s and 0.3 s waits) | about 0.46 s, two OCR calls, if read on frames 1 and 2 | about 0.21 s, one OCR call |

- **Not read:** the KDA failed to parse on all four frames (inferred from the time per frame). The
  frames did not disagree. The log line `not read (4 frame(s), no two read alike)` prints in both
  cases, so the message misled the design.
- **Read:** the median time fits a read on frames 1 and 2 (inferred). The first version of this
  document said every read panel was read on its first two frames; that came from a script that
  assumed 2 frames for a line without a count, and is withdrawn. In the trial session the first two
  read rounds were read on frames 2 and 3 (see the trial record).

**The first click's timing does not explain it.** The read rate is 53% to 66% however soon the
first click follows the GAME_END_B entry, with no trend (385 readings that have both):

| First click after GAME_END_B entry | Read |
|---|---|
| 0 to 0.2 s | 31 of 58 (53%) |
| 0.2 to 0.5 s | 84 of 136 (62%) |
| 0.5 to 1 s | 67 of 101 (66%) |
| Over 1 s | 52 of 90 (58%) |

**Read rate by session** (stats files; rounds are the click-to-finish missions):

| Session | Rounds | Read |
|---|---|---|
| 20261027_060741_acct3 | 94 | 62 (66%) |
| 20261027_183245_acct2 | 51 | 32 (63%) |
| 20261027_233750_acct1 | 10 | 6 (60%) |
| 20261028_004906_acct1 | 24 | 16 (67%) |
| 20261028_052534 | 13 | 8 (62%) |
| 20261028_070126_acct3 | 97 | 62 (64%) |
| 20261028_170643_acct2 | 93 | 46 (49%) |

**18 readings belong to no round.** They are all unread, and none follows a
`Game state: … → GAME_END_B` transition. 17 of them came 4.1 to 14.7 s after wingman switched from
GAME_END_B to GAME_LOBBY while the end screens were still up. The click-to scan, which
[ADR 087](../adr/087-blackout-evidence-capture.md) re-enables in the lobby, found the prompt again
and started a second click-through, and that one took a second reading. At 2026-10-28 00:56:14,
for example, the first click-through clicked the prompt 3 times and handed over to the lobby. The
scan found the prompt at 00:56:25, and the second reading came at 00:56:29. The 18th came 283 s
after a lobby entry, and its cause was not looked into. These readings inflate the session
summary's "of Y rounds" count: the 06:07 session printed 62 of 96 against 94 rounds, and the 00:49
session 16 of 28 against 24.

## Action items

In order: AI-005-1 finds the cause, AI-005-2 fixes it, and V0 of Design 018 (Phase 0) is their
measure. AI-005-3 is independent and small. AI-005-4 and AI-005-5 are edits to Design 018 before
Phase 1 starts.

### AI-005-1 — Log what the KDA crop reads on an unread panel (recommended first)

**Why.** All 168 unread panels failed at the KDA, on every frame, and the logs keep nothing of what
OCR saw. Design 018's Phase 0 plans to log "each of the four readings", but every one of them is
None, so that would show nothing.

**Change** (operator, 2026-10-29: capture the panels in shadow sessions, then measure the reader
against them). Shadow throughout: what is booked and how the clicking runs do not change.
- **The text.** Each frame's read keeps the text OCR returned (`PanelRead`). The `not read` line
  says which case it was, with that text for every frame:
  `ROUND PERFORMANCE: not read — no KDA on any of 4 frames: kda '…'; …`, or
  `no two of N frames read alike: …`.
- **Later pages.** When the first click's panel was not read, the frame the click-through already
  grabs before each later click is read too, and logged. A KDA there logs
  `ROUND PERFORMANCE: shadow — page N shows a KDA: …` at INFO. It is not booked. This tests the
  staged reading the operator proposed (click, look for the KDA, then go on) without changing the
  clicking.
- **The corpus.** With `round_performance.capture` on (shipped on for this trial, schema default
  off), the frames are saved in `test_screenshots/round_performance/` as JPEG at quality 95,
  within the capture budget. A read round saves the first frame that read its figures, to check
  accuracy against (frame 1 until 2026-10-29 05:43, which was the podium page in half the rounds).
  An unread round saves all four frames and each later page. Names: `<stamp>_p<page>[_f<frame>]_<what
  was read>.jpg`.
- **Afterwards.** Label the saved frames with the true score and K/D/A and replay the reader over
  them offline. Each reader change is then scored against the corpus without flying; the corpus
  becomes a regression test, like the telemetry corpus in `make ocr`.

**Success.** One long session (about 90 rounds, about 40 unread at the 2026-10-28 rate) gives:
every unread round's OCR text in the log; whether a later page shows the KDA; and saved frames to
label. The cause can be named from them.

**Status.** Cause found, 2026-10-29 (trial record below): the panel's height follows the medals
earned, and the fixed crops read only one height. Implemented on `f286e2a` plus the working tree
(uncommitted); the game's UI version is not reported in the log. The capture stays on until
AI-005-2's corpus has every height, read rounds included.

**Trial record.**

| Session | Rounds | Read | Not read: no KDA / differed | KDA on a later page | Frames saved |
|---|---|---|---|---|---|
| Baseline: 20261028_170643_acct2 (before the change) | 93 | 46 of 98 readings | not logged | not looked for | none |
| 20261029_025029_acct2, 02:50 to 03:25 (operator stop, Backspace, during the 7th matchmaking) | 6 | 2 | 4 / 0 | once, a false one: the scoreboard (round 5) | 36 frames, 15 MB |

Findings as they land (session 20261029_0250_acct2, started 02:50:22):

- **02:56:49, round 1, read (score 230, K/D/A 2/5/0) on frames 2 and 3.** Frame 1, about 0.5 s after
  the first click, still showed the page before the panel: MVP, 2ND and 3RD, the top three players'
  cards, each with a SCORE and a KDA (saved: `20261029_025649_p1_f1_nokda.jpg`). The page the first
  click leaves takes more than 0.5 s to go (measured, one round). Its KDAs sit at about y 0.64 to
  0.67 of the frame, below the KDA crop (0.36 to 0.43), which there covers the third player's
  aircraft picture, so the reader cannot book another player's figures from it.
- **03:03:11, round 2, read (387, 1/2/3), frame 1 again without a KDA**
  (`20261029_030311_p1_f1_nokda.jpg`). First click to the reading: 2.09 s and 2.21 s for the two
  rounds, against the 1.73 s median before, which fits a third frame (inferred). Two rounds; the
  read rounds' frame counts are not logged, so the share of rounds the podium outlasts frame 1 in
  needs the saved frames to count.
- **03:09:14, round 3, not read: the cause.** `not read — no KDA on any of 4 frames: kda ''; kda '';
  kda ''; kda ''`. All four frames show the PERFORMANCE panel, score 100 and K/D/A 1/5/0
  (`20261029_030914_p1_f1_nokda.jpg` to `_f4_`), but lower on the screen: this round earned no
  medals, the panel has no medals row and is shorter, and it is placed about centred (its middle
  near y 585 px in both this frame and the 2026-10-26 screenshot; inferred from two layouts).
  Measured on the frames:

  | Panel | Header underline | Score and KDA figures | Read by the fixed crops (y 435 to 515) |
  |---|---|---|---|
  | With medals (2026-10-26 18:45:10, "4 MEDALS") | about y 418 (read off the image) | y 448 to 502 | yes |
  | No medals (2026-10-29 03:09:14) | y 554 to 556 | about y 585 to 640 | no: the crop sees the sky above the panel, OCR returns nothing |
  | 5 medals (2026-10-29 03:14:23, round 4, below) | y 300 to 302 | about y 330 to 385 | no: the crop sees the KDA label and the medals bar, read as `'415'` |

  The later pages (2 to 5) showed no KDA; page 5 read `'1245/3/31/15'`, another page's figures,
  which the KDA pattern rejected. So the staged reading would not have helped this round: the
  panel was up the whole time, in a place the crop does not look.
  This fits the deaths link (Where this starts): a round with more deaths is less likely to earn a
  medal, so more likely to get the short panel (inferred). Whether the panel takes more heights
  than these two (one row of medals against two, say) needs more rounds.
  For AI-005-2: a brightness test for the underline found it on this dark frame, but on the
  2026-10-26 frame, over bright cloud, it caught the sky instead, so locating the panel needs more
  than brightness.
- **03:14:23, round 4, not read: a third height.** `no KDA on any of 4 frames: kda '415'` on every
  frame. The panel shows score 800 and K/D/A 5/4/3 with "5 MEDALS" and a taller medals area (panel
  from about y 215 to 950), so the figures sit higher, about y 330 to 385, and the KDA crop holds
  the KDA label and the medals bar, which the digit allowlist turns into `'415'`
  (`20261029_031423_p1_f1_nokda.jpg`). Across the three layouts (measured): the figures always
  sit 30 to 85 px below the header underline, and the panel stays centred near y 583 (panels
  about 215 to 950, 330 to 835 and 470 to 700). The panel's height follows its medals rows, so
  the fixed crops read only the height they were measured on. AI-005-2 has to find the panel's
  height on each frame.
- **03:18:34, round 5, not read: the no-medals height again** (underline at y 554 to 556,
  measured). **And a false reading on a later page.** The shadow check logged
  `page 5 shows a KDA: kda '63/4/040' score '05,.,.'`, which the parsers accept as score 5 and
  K/D/A 63/4/40 (`20261029_031834_p5_read-5-63-4-40.jpg`). Page 5 is the full scoreboard (SCORE,
  EQUIPMENT and RANK tabs; both teams, each player's aircraft, KDA and score); the crops landed on
  Team Bravo's rows. Our own row, outlined (`[ISAF] MOBIUS2`, F-5), shows 0/3/1 and 50.
  Two consequences:
  - Reading the later pages, the staged reading proposed on 2026-10-29, would have booked
    another player's figures. Whatever AI-005-2 does must confirm the page is the PERFORMANCE
    panel (its header, or its SCORE and KDA labels), and the parsers should refuse what a KDA
    cannot be: a leading zero (`040`), or more digits than a round's counts reach.
  - The scoreboard carries our own row's KDA and score. It is a cross-check for labelling the
    corpus, and a second source if the panel ever fails.

### AI-005-2 — Fix the reader so that it reads nearly every panel

**Why.** A third of the rounds go unread, and the unread ones are most likely the worse ones (Design
018, Context; inferred from their 3.4 respawns against 2.9). The latest session read only 49%.

**Change.** AI-005-1 found the cause: the PERFORMANCE panel is drawn centred, and its height
follows the rows of medals earned, so its figures sit at different heights (three seen: about y
330, 448 and 585 px), always 30 to 85 px below the header underline. Find the height on each
frame instead of assuming it. Proposed: OCR a taller band of the KDA column (about y 0.17 to 0.60),
take the text box that matches the KDA pattern, and read the score column on that box's row.
Constraints from the trial:
- Confirm the page is the PERFORMANCE panel. The scoreboard page (page 5) has KDAs and scores in
  the same columns and gave a false reading in shadow.
- Tighten the parsers: no leading zeros, and no more digits than a round's counts reach.
- The page before the panel (MVP, 2ND, 3RD) can still be up at frame 1, so the reader keeps
  reading later frames.
- Test on the saved frames of every height, and on the scoreboard frame as a must-not-read case.

Earlier hypotheses, for the record:
- **The score counting up:** not shown either way. The read rounds' frame counts were never
  logged (see Where this starts).
- **Two-digit counts overflowing the KDA crop:** unlikely. The crop is about 395 px wide, about 10
  characters of this font, and "10/10/10" needs about 300 px.
- **A first click too early:** tested, no trend (table above).

Still open (unverified): the panel not yet drawn in the 0.5 to 2 s the reader looks, a different
layout of the panel in some rounds, or the KDA's slashes read as digits.

**Success.** Design 018 V0: at least 95% of the panels read over a session of at least 50 rounds.

**Status.** Implemented 2026-10-29, in live trial. As built:
- `PERF_KDA` and `PERF_SCORE` are tall bands (y 0.06 to 0.62). One OCR read of the KDA band (no
  allowlist, one box per line) finds a box that parses as a KDA with a "KDA" label box 0.03 to
  0.075 of the frame's height below it; the score is then read on that row (±0.033) with the digit
  allowlist. No label below, no reading: the scoreboard's header is above its KDAs.
- The KDA pattern takes one or two digits per count, without a leading zero, and the score no
  leading zero; the letter O is read as zero.
- Tested on a curated corpus, `tests/fixtures/round_performance/` (local, like the other
  fixtures): the panel at all three heights, the podium and two scoreboards, enumerated with their
  labels, and two frames at half size.

Offline, on all 36 frames saved in the AI-005-1 trial (measured): the 16 panel frames of the 4
unread rounds all read, each round's four frames alike (100 1/5/0, 800 5/4/3, 50 0/3/1, 200 1/4/2;
the 50 0/3/1 agrees with that round's scoreboard row); the 2 podium frames and the 18 later pages,
the 4 scoreboards among them, read nothing. A frame's read took 0.46 s (median, GPU). This is
replay on archived frames, not the verdict: the live session decides.

| Session | Rounds | Read | Not read | Notes |
|---|---|---|---|---|
| 20261029_043913_acct3, from 04:39 (running; first 5 rounds, to 05:08) | 5 | 5 | 0 | 3 of the 5 saved the panel as frame 1, and all 3 match it by eye: 275 1/4/1 ("5 MEDALS"), 105 1/3/0 ("1 MEDALS"), 350 3/5/1 (no medals). The other 2 saved the podium (read on later frames). The "5 MEDALS" panel here has its figures near y 477, not y 358 as at 03:14, so the height does not follow the medal count alone (the medals area may still be filling in); searching the band handles it either way |
| same session, rounds 6 to 10 (to 05:37) | 10 in all | 10 | 0 | 2 more saved the panel and match it by eye (655 2/2/5, 500 1/1/2): 5 of 5 checkable readings correct. The other 5 saved frame 1, the podium, so they cannot be checked: the capture now keeps the frame that was read (from the next launch) |
| same session, final (operator stop, Backspace, 05:54) | 12 | 12 | 0 | `run_20261029_043918_acct3_stats.json` |
| 20261029_060125_acct3, 06:01 to 06:53 (the operator's own session; stopped with `z`) | 9 | 8 | 1 | Ran the 05:13 and 05:43 edits (AI-005-3, the capture keeping the frame read). All 8 read rounds saved the frame they were read on. The miss: score **0**, K/D/A 0/3/0. Frames 2 to 4 found the KDA with its label, but OCR found no text in a lone "0" in the score row (`20261029_065319_p1_f2_noscore.jpg`). Fixed 2026-10-29: the score row is read at twice the size; that reads the "0" on all three frames, and the 36 other panel frames read the same either way (measured). No second click-through happened, so AI-005-3 is not yet tested live |
| 20261029_071302_acct3, from 07:13 (zero-score fix, AI-005-3; first 5 rounds, to 07:43) | 5 | 5 | 0 | All 5 saved the frame they were read on |
| same session, to 08:37 | 14 | 14 | 0 | The zero-score fix confirmed live: 08:37:34, score 0, K/D/A 0/4/0, matches its saved panel (`20261029_083734_p1_f1_read-0-0-4-0.jpg`), over pale sky. The first four-digit score, 1,095 (07:57), read as 1095, comma dropped, matches its panel. No second click-through yet, so AI-005-3 is still untested live |
| same session, to 09:50 | 26 | 26 | 0 | A second zero-score round (09:24, 0/4/0) read too. Second click-throughs skipped (AI-005-3): 0 |

**Accuracy (measured by eye, 2026-10-29 07:45):** every reading with a saved panel frame matches the
panel: 5 in the 04:39 session, all 8 in the 06:01 session, all 5 in the 07:13 session, 18 of 18.
They include panels with 0, 1, 2, 5, 15 and 18 medals; the 15- and 18-medal panels put the
figures high on the screen and still read. The other 7 readings of the 04:39 session have no
saved panel frame (frame 1 was the podium) and are unchecked.
Since then (to 10:53): the 1,095 round (07:57), the zero-score round (08:37) and the 12 newest
readings (09:43 to 10:45), over orange sunset, blue sky and night, all match: **32 of 32**
checked readings correct.

**10:59:25, the first miss after the zero-score fix (38th round of the 07:13 session).** The page
before the panel (MVP, 2ND, 3RD) was still up on frames 1 to 3, and the panel came on frame 4,
the last of four, reading 400 and 2/3/0 (`20261029_105925_p1_f4_read-400-2-3-0.jpg`); a reading
takes two frames alike, so the round went unread. Of the 46 rounds since 06:01 that showed the
panel, it was first read on frame 1 in 32, frame 2 in 10, frame 3 in 3 and frame 4 in 1
(measured from the saved frames' names). Fix, built 10:59 and not yet live: `MAX_READS` 6, so a
podium as long as this one still leaves two panel frames; a round with no panel costs two more
frames, about 1 s. It ships after this session reaches 50 rounds, so V0 is measured on one code
state.

**11:15 to 11:21, the session ended by MetalStorm server maintenance.** After round 41 the lobby
showed "Server Maintenance in Progress" in place of PLAY (`test_screenshots/unknown_anomalies/
blackout_20261029_112032_stuck271s.png`); the lobby blackout handling pressed ESC and cancelled
Exit to Desktop until the liveness guard fired at 11:20:50, and wingman was stopped with `z` at
11:21 (it exited in the lobby and closed the game). Final: **40 of 41 rounds read (97.6%)**, 9
short of V0's 50-round session. Since the height fix, across three sessions: 12 of 12, 8 of 9 (the
zero score, since fixed) and 40 of 41 (the long podium, fix built): 60 of 62.

**11:35, blocked: MetalStorm update required.** With `MAX_READS` 6 gated (3,483 passed) and
launched on account 3 at 11:35, the game stopped at its login screen with "Update Required — A
new update is available, please download it" (`test_screenshots/unknown_anomalies/
unknown_20261029_113547_stuck31s.png`; the client on screen is version 194, the one every
reading above was taken on). Wingman cannot classify that screen (GAME_UNKNOWN startup timeout,
logged every 1 to 2 s) and was stopped with `z` at 11:37. Updating the client is the operator's
step. **The update is a new column for this record:** the panel, its medals area and the end
screens may move with it, so the first rounds after it are checked against their saved frames
before the 6-frame fix is judged, and the corpus tests are re-run on frames from the new version.

**2026-10-30, the first wrong reading: a 1,255 booked as 25513.** The operator found a 25,513
taken-over round on the dashboard (account 3, round ended 09:10:11). Its saved frame
(`20261030_091011_p1_f2_read-25513-6-2-2.jpg`) shows **1,255** and 6/2/2; the KDA was right.
Cause (measured): OCR returns a four-digit score in two boxes, split at the comma, and lists the
boxes in no set order, and the reader joined them in the order listed. The live read's boxes were
not logged; re-read now, the same frame gives `1` and `255` in order. The 03:48 frame of a 1,295
(read right live) gives `295` before `1,`, so the old join gives 2951. Over all 146 saved frames
re-read on the CPU reader, 3 split the score, all four-digit (3 of 10); the old rule got 1 of them
wrong, the new rule 0, and the new rule lost no frame. Fix: the boxes are joined left to right
with a separator between them, and a score must be one run of digits or thousands groups of
three (`1,3,255` and `1,63` no longer read). The two frames are in the corpus. The stats file's
figure was corrected by hand to 1,255 with a `corrected` note and reloaded. Left uncaught: a
comma read as a digit inside one box (`13` and `255` would read 13,255); the read line does
not log the OCR text, so the next one would need its saved frame.

### AI-005-3 — Book no unread round for a second click-through

**Why.** The 18 readings that belong to no round (Where this starts) count as unread rounds in the
session summary, and in Design 018's log fallback as written.

**Change.** Read the panel only in the click-through that follows a battle → GAME_END_B transition.
The click-through that ADR 087's lobby scan starts then reads nothing. Alternatively, the tracker
could count an unread reading only while a click-to-finish mission is waiting for its reading.

The handover to the lobby with the end screens still up is the underlying cause. ADR 087 recovers
it within 15 s, so it is noted here, not made an item.

**Success.** The summary's "of Y rounds" equals the session's click-to-finish missions, and no
`ROUND PERFORMANCE` line follows a lobby entry without a new battle → GAME_END_B transition between
them.

**Status.** Implemented 2026-10-29, not yet live: it deploys at the next launch, after AI-005-2's
live trial (one live change at a time). As built, the two options combined:
`MissionStatsTracker.round_awaits_reading()` is true while the last mission ended on the end screen
and its panel has not been tried; the click-through asks it before reading, and when no round
waits it reads, saves and books nothing, and logs `End-of-round click-through: no round waits for
its PERFORMANCE panel, so none is read …`, a line without the `ROUND PERFORMANCE` prefix, so
Design 018's log fallback does not count it either. `record_round_performance` books a reading
against the waiting round only, once. Wingman started on the end screen no longer books that
round. Tested on the 2026-10-28 00:56 case (one round, two click-throughs).

### AI-005-4 — Correct Design 018's account of the data

**Why.** Design 018 is a draft. Three of its statements about the data are wrong or incomplete, and
two of them shape its Phase 0.

**Change.**
- **Context, "A third of the rounds are not read".** Replace "Each failed after all four frames
  the reader takes, 0.3 s apart, disagreed" with the measured finding: no frame of an unread panel
  read a KDA. Update the Phase 0 hypotheses in Rollout to match AI-005-2.
- **Open question 4.** Mark it answered: the readings with no mission are second click-throughs of
  one end screen, not rounds, and they do not belong among the rounds flown.
- **The log fallback.** Replace "A log cannot tell apart the readings with no mission behind them,
  so it counts them all" with a rule. A `ROUND PERFORMANCE` line with no
  `Game state: … → GAME_END_B` since the last `GAME_END_B →` line is not a round. Add the case to
  V1.
- **Context, "One round says little".** The example given is not noise. Account 3 averaged 490
  from 09:00 to 12:00 on 2026-10-27 and 291 from 12:00 to 15:00, with 21 rounds each. If the
  standard deviation of 212 holds within those blocks, that gap is about t = 3 (inferred).
  Something at the block level, such as the time of day or the opponents, moves the score. The
  figure of about 400 rounds on each side assumes independent rounds, and clustered rounds need
  more. Rounds compared on either side of a change marker also differ in those block effects. Say
  so in "Reading the dashboard".

**Success.** Design 018 states the measured cause of the unread panels, answers open question 4,
gives the log fallback a rule that V1 tests, and qualifies the sample-size advice.

**Status.** Done 2026-10-29 07:50, in Design 018 (still Draft): the Overview and Context give the
measured cause and the fix; the block-level caveat is in Context and "Reading the dashboard"; open
question 4 is answered; the log fallback (What a round is, The ingest, V1) skips a
`ROUND PERFORMANCE` line with no battle → GAME_END_B transition before it; Phase 0 and its risk
row say what was done.

### AI-005-5 — Design changes to Design 018 before Phase 1

**Why.** Details of the store and the dashboard that would fail quietly or invite trouble once
built.

**Change.**
- **The tag filter's quoting.** Use `${tags:sqlstring}::jsonb` in place of
  `'${tags:raw}'::jsonb`. As written, a quote or malformed JSON typed in the box breaks every panel
  and lets a viewer insert SQL. The read-only role limits that, but does not prevent it.
- **The read-only role's future tables.** `ALTER DEFAULT PRIVILEGES` covers only the tables
  created by the role that ran it. State that the ingest creates the tables as the same superuser
  that the init script runs as, or Grafana's grants go missing without an error.
- **Phase 2's ingest after `make r`.** A session stopped with Ctrl-C or SIGTERM may end the
  recipe before the ingest runs. The next ingest makes up for it, because the ingest keeps no
  state. Say so, and word V4 to match.
- **Log times across the change from daylight time.** Log lines carry no UTC offset, so log
  fallback rows between 01:00 and 02:00 on 2026-11-01 are ambiguous. They come only from sessions
  without a stats file, so they are few; record the ambiguity rather than guess the offset.
- **The squadron ranking (Phase 3)** changes what wingman does in the lobby, where an ESC press
  opens Exit to Desktop and stall recovery acts. Move it to its own design or ADR, and leave
  `squadron_rankings` out of `grafana/schema.sql` until then. The schema is applied idempotently,
  so adding the table later costs nothing. The operator decides.

**Success.** Design 018 carries each change, or records why not.

**Status.** Four of five done 2026-10-29 08:20, in Design 018 (still Draft): the tag filter is
`${tags:sqlstring}::jsonb` in the Tags section and all three SQL sketches; the read-only role's
future grants name the superuser that creates the tables; Phase 2 and V4 say a missed ingest is
made up by the next; the log fallback reads the repeated daylight-time hour as its first
occurrence. Open: the Phase 3 split, the operator's decision.

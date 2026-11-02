# ADR 167 — Read Each Round's Jet from the Lobby

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-31 | 1.9.3           |

Operator, 2026-10-31: "i want to detect which jet is flying and track it on the dashboard, the idea
is to monitor wingman performance per jet type some jets will perform better than others in wingman
i want to be able to view the dashboard and see which". This reverses
[Design 018](../hldd/018-score-per-round-in-grafana-hldd.md)'s non-goal of no panel per jet.
[ADR 168](168-read-the-jet-again-through-the-wait-for-a-match.md) extends D1 and D4.

## Decision

**D1. The jet is read from the frame PLAY or READY was clicked on.** The lobby labels the selected
jet above it. `GameEvent.LOBBY_PLAY_CLICK` already carries that frame, so the name read is the jet
that round flies. The read runs on a thread of its own through the OCR pool, so the click does not
wait for it. It runs live only.

**D2. The label is cut at its trophy icon before it is read.**
- The label is centred, so its name starts anywhere from x 793 to 881 of 1920 px. `JET_NAME` is a
  wide band for that reason.
- OCR can run the name into what follows it, as in `Su-35 SUPER FLANKER '/683` and
  `JAGUAR GR3 Y 0 @`. Each such text would be a jet of its own on the dashboard.
- The trophy icon is gold (hue 22 to 23) on every frame, and at least 8 columns wide. The band ends
  at the first such run.
- No icon means no reading. It is the check that the label is there, and a round with no jet is
  better than a round under the wrong one.

**D3. The name is kept in capitals and checked for shape.**
- Capitals stop OCR's case from splitting one jet in two.
- A name is words of letters and digits, with `-`, `/` or `.` inside them (J-10, F/A-18F, M.III),
  and at least one letter. There is no list of jets, so a jet wingman has not met needs no change.
- OCR reads the slash of F/A-18 as a letter, on all 12 scalings and thresholds tried on live frames
  (`FIA-18F SUPER HORNET`). So a name starting `F` with `I`, `L` or `1`, then `A-`, is corrected to
  `F/A-`.

**D4. A reading holds until the next click.** The click clears the last reading before the new read
starts, so a failed read leaves its round with no jet instead of the previous round's. A mission
that starts with no click keeps the reading, because a jet changes only in the lobby. This replaced
a first rule that cleared the reading at every mission start. On 2026-10-31 07:09:39 the operator's
double `n` forced `GAME_LOBBY` in a battle, and the mission that went on to get the round's score
would have had no jet.

**D5. Records.**
- A reading is logged as `JET NAME: <name>`, and a failed one as `JET NAME: not read — <why>`.
- The round's mission record gets `tags: {"jet": "<name>"}`, which the ingest already copies to the
  round. For a session with no stats file, the ingest takes the jet from the log line.
- With `jet_name.capture` on, the label each name was read from is kept in
  `test_screenshots/jet_name/`, and the whole frame when no name came of it.

**D6. Wingman by jet.** This dashboard table lists wingman's own rounds per jet, the best mean score
first, with rounds flown and read, mean and median score, and kills, deaths and assists per round.
It follows the time range and the tags box.
- A round's score has a standard deviation of about 200, so the table shows the margin on each mean
  (`± 95%`).
- A jet with under 10 read rounds ranks after the rest. In a trial on real rounds with made-up jets,
  one lucky round topped the table at 755, above a jet with 47 rounds read.
- Rounds with no jet mix several, so they come last, as `not recorded`.
- The bars are one colour. Their length ranks the jets, and red or green would claim more than the
  margin allows.

## Why

- The game keeps the selected jet in none of the PlayerPrefs wingman reads
  ([`wingman/game_stats.py`](../../wingman/game_stats.py)), so it is read off the screen.
- The click's frame needs no extra capture, no hold on PLAY and no clicks.
- Without a list of jets, a misread shows as a stray row, or as a wrong name for a real jet, as
  `FIA-18F` would have. The saved frames had no such case and the first live frames did, so V3 has
  to look at the names, not only count them.

## Consequences

- **Past rounds have no jet.** The saved end-of-round frames do not name it, so all 698 rounds of
  2026-10-28 to 10-31 show as `not recorded`.
- **Cost.** One OCR of one crop per round, off the quick-scan's thread.
- **Jet and pilot can be tangled.** If an account stays on one jet for a while, the table compares
  accounts as much as jets, and the jets' levels differ too. The tags box narrows it to one account.
  An account does change jets: the main prefix showed the F/A-18F at 06:13 and the KF-21 at 06:52 on
  2026-10-31 (cause not known), so the jet is read each round and never assumed from the account.
- **New risk.** A game update can move the label or recolour the icon. The read then gives no name,
  says why in the log, and keeps the frame; rounds go on, as `not recorded`.
- **Implementation.** `wingman/jet_name.py` reads the name, `wingman/main.py` wires it to the click,
  and `wingman/mission_stats.py` books it. `scripts/round-scores.py` reads the log line.

## Validation

- **V1, the reader on real frames (met).** The band cut at the icon, on every saved frame with the
  icon: 29 of 29 read the right name, over 10 jets from 2026-09 to 2026-10, in the same crop. 98
  other frames gave no name. On the operator's client, 16 of 16 frames at two lobbies read
  `F/A-18F SUPER HORNET`. One frame of each of the 11 jets is pinned by `tests/test_jet_name.py`.
  M.III has been seen only on the scoreboard, so its lobby label is not proven.
- **V2, the dashboard's SQL (met).** The panel's query ran on the live rows with made-up jets, in a
  transaction that was rolled back, then on the first real jets (run of 06:51). The panel has not
  been seen rendered.
- **V3, live (met for two jets).** Each round of a session logs a `JET NAME:` line, the name matches
  the label kept for it by eye, and the rounds show under that jet on the dashboard: the runs of
  06:51 (KF-21 BORAMAE) and 07:37 (F-111 AARDVARK; one label of 11 checked by eye). A restart inside
  a round has not happened on the second D4. The FSM reaches
  `GAME_LOBBY` about 15 s before PLAY is clicked, with the scoreboard still up at first, so only the
  click's frame is a lobby frame.
- **Open case.** A frame of the F-22 RAPTOR from 2026-09-25 has no trophy icon, so it gives no
  reading. V3 will show whether a current lobby can do that.

## Trial record

| When | Session | Code | Result |
|---|---|---|---|
| 2026-10-31 06:00 | the operator's, main prefix, started 05:25 | predates this ADR | Cannot exercise it: no `JET NAME:` line can appear. A live run waits for that session to end. |
| 2026-10-31 06:02 to 06:18 | the same, frames of `:3` grabbed read-only | the reader, run offline | Found by these frames: the scoreboard names the jet as F/A-18F, which the first reader could neither read (`/` left out of the allowlist and the name) nor save (the slash made a directory of the file name). Then OCR read it as `FIA-18F`. All three fixed, and 16 of 16 frames read right. |
| 2026-10-31 06:51 | `make r1`, main prefix (mobius1), KF-21 BORAMAE | this ADR, uncommitted, on branch `acs-terrain-avoidance`. Gate: 3681 passed, 1 failed, a test still pinning `r1` to `Metalstorm-acct1` from before the Makefile swap. That test was corrected and now covers all four targets; no wingman code changed after the gate started, and the suite was not re-run in full. | Running (V3). First live read at 06:52:13: `KF-21 BORAMAE`, a jet not met before, with no code change. It came 245 ms after PLAY was detected, and the game reached `GAME_WAITING` 310 ms later. Stopped by the operator at 07:33. Over the session: 9 PLAY clicks, 9 names, 0 failed, 0 tracebacks, and 9 of 9 saved labels match by eye, four of them dimmed. The after-session ingest carried 6 rounds to the dashboard under the jet. Wingman by jet shows 5 of them; the sixth (2,535, 14/4/0, matching its frame) was a manual takeover, so the panel leaves it out. Round 4 (605) has no jet, as predicted, because this run had the first D4. |
| 2026-10-31 07:37 | the operator's `make r2`, mobius2, F-111 AARDVARK | this ADR in full. Gate at 07:46: 3686 passed, `make test` exit 0. | Stopped by the operator at 08:37. Reads a 13th jet with no code change. 11 PLAY clicks, 11 names, 0 failed, 0 tracebacks, and all 9 rounds tagged and on the dashboard. Wingman by jet has its first two jets: F-111 AARDVARK 579 ± 247 over 9 rounds, KF-21 BORAMAE 547 ± 207 over 5. The ranges overlap, each jet flew on one account only, and both are under the 10 rounds, so this says nothing yet about which is better. |

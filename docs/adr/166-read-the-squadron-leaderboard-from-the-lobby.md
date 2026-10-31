# ADR 166 — Read the Squadron Leaderboard from the Lobby

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-31 | 1.9.3           |

Operator, 2026-10-31: "review the last 4 'v' screenshots it shows how to get to the squad ranking by
click in GAME_LOBBY on the top right > Leaderboards > SQUADRONS > then screenshot_20261031_014502.png
shows squad ranking as 659 with trophy count 358032, Update the tracking to track the ranking and
trophy count, iterate until we have accurate readings". This is
[Design 018](../hldd/018-score-per-round-in-grafana-hldd.md)'s Phase 3. The design left the menu
navigation to its own ADR (open question 6).

## Decision

**D1. The route is the operator's, by the screens' own controls.** It runs in `GAME_LOBBY` between
rounds:

1. Click the menu button at the top right (`SQ_MENU`).
2. Click LEADERBOARDS in the menu (`SQ_MENU_LEADERBOARDS`). The leaderboards open on TOTAL TROPHIES.
3. Click the SQUADRONS tab (`SQ_TAB_SQUADRONS`).
4. Read our squadron's row, pinned at the bottom of the list: its rank (`SQ_OWN_RANK`) and trophies
   (`SQ_OWN_TROPHIES`).
5. Go back with the page's back arrow (`SQ_BACK`).

ESC is never pressed: on the lobby it opens Exit to Desktop. The code that navigates is given
clicks only.

**D2. Each click waits for the screen before it.** Wingman clicks the menu only when PLAY shows and
the menu's LEADERBOARDS entry does not. The open menu leaves most of PLAY in view, so PLAY alone
would mistake it for the lobby. Wingman clicks LEADERBOARDS only once the menu shows it. It reads
the row only once the page title reads LEADERBOARDS. Each step waits up to
`squadron_ranking.step_timeout_s` (8 s) for its screen, looking every 0.4 s.

**D3. A reading needs the SQUADRONS tab, our squadron's name, and two frames alike.**
- The list header must read CURRENT TOP SQUADRONS. TOTAL TROPHIES pins our own player's row in
  the same place (17,366 and 20,094 on the operator's screenshot), so a reading without this check
  could take the player's figures.
- The row must carry `squadron_ranking.squadron` (Mobius Squadron).
- Rank and trophies are read on the crop doubled in size, with a digit allowlist. OCR's boxes
  are joined left to right, and the digit groups must be whole. Action Item 005 found the same
  split of "1,255" booked as 25513.
- Up to 6 frames are read, 0.5 s apart.

**D4. Going back is checked.**
- On the leaderboard, wingman clicks the back arrow. On an open menu, it clicks the menu button
  again (unverified, see Validation).
- It tries three times. Then it logs `SQUADRON RANKING: not back in the lobby` and leaves recovery
  to the lobby's own stall handling.

**D5. The rest of the lobby holds still while it runs.**
- While a reading is due, the lobby quick-scan holds the PLAY click. READY is not held, so a squad
  lobby never waits.
- The navigation starts only after the quick-scan has read PLAY in the last 3 s. That means a solo
  lobby with nothing over it.
- While the navigation runs, the following wait:
  - the quick-scan's whole lobby cycle: no click, popup dismissal or stall count;
  - the lobby escape loop's ESC;
  - the guards' safe point and the deferred `z` exit.
- A pending `z`, standby or exit ends the navigation at its next step, and none starts then.

**D6. Schedule and switch.**
- The setting is `squadron_ranking.enabled`. It ships `false` and is turned on per machine in
  `config.local.yaml`, like the score dashboard.
- A reading is due at the first lobby of a session, and `every_min` (60) after the last attempt,
  read or not, and at most once in a lobby.
- VEDA sets `every_min` to 0 in `config.local.yaml`: a reading at every lobby, so one point per
  round on the dashboard (operator, 2026-10-31).
- The navigation runs live only, with every crop calibrated.

**D7. Records.**
- A reading is logged as `SQUADRON RANKING: rank N, trophies T`. A navigation that gives up logs
  `SQUADRON RANKING: not read — <why>`.
- Each reading goes into the stats file's `squadron_rankings` list (`ts`, `rank`, `trophies`), and
  the session summary prints the last one.
- `scripts/round-scores.py` loads the readings into `squadron_rankings`, which gains a `trophies`
  column. For a session without a stats file, it reads them from the log.
- The dashboard charts them over time (operator, 2026-10-31): Squadron rank per reading and
  Squadron trophies per reading sit under Score per round, each one line with a dot at every
  reading. Squadron now shows the latest of each. Both figures are the squadron's, so they do not
  depend on the account that read them: the three panels ignore the tags box.
- With `capture` on, the frame of each reading, and the last frame of a navigation that gave up,
  are kept in `test_screenshots/squadron_ranking/`.

## Why

- The operator showed the route. Clicking the screens' own controls, each only after the screen
  before it has been confirmed, keeps wingman from acting on a screen it has not seen.
- The lobby's automation would otherwise fight the trip:
  - the quick-scan clicks PLAY within a second of reaching the lobby;
  - after 10 s without a lobby crop it counts a stall and runs the stall-crop scan;
  - the escape loop presses ESC.

  D5 holds each of them for the length of the trip only.

## Consequences

- **Time.** Each reading delays the next round by the length of the trip: 6.7 to 12.2 s from
  `opening` to the PLAY click, median 8.6 s, over the 5 trips of 2026-10-31 02:35 to 02:58. VEDA
  reads at every lobby, so each round pays that.
- **New risks.**
  - A game update can move the menu or the list. A step whose screen does not come gives up and
    goes back, and the crops are recalibrated with `make calibrate-crop`.
  - A trip that cannot get back hands the lobby to its stall handling, which can press ESC. V2
    counts the returns.
- **Implementation.**
  - `wingman/squadron_ranking.py` holds the reader, the navigation and the schedule.
  - `wingman/main.py` starts the navigation and records its result.
  - `wingman/analyzer.py` adds `set_lobby_errand` and the quick-scan holds.
  - The crops are `SQ_*` in `wingman/config.yaml`, measured on the operator's four screenshots
    (`tests/fixtures/squadron_ranking/`).

## Validation

- **V1, accuracy.** Each reading matches its saved frame by eye, over at least 10 readings.
- **V2, the route.** Over a session with the reading on:
  - every navigation ends in the lobby. There are as many `SQUADRON RANKING` lines (read or not)
    as `opening the squadron leaderboard` lines, and no `not back in the lobby`;
  - no ESC press and no stall-recovery action happens while one runs;
  - no match start is missed because of it.
- **V3, the trip's cost.** The time from `opening the squadron leaderboard` to the PLAY click,
  measured from the log.
- **V4, the open menu.** Not yet seen: whether a second click on the menu button shuts the menu
  (D4).
- **V5, the dashboard.** The readings show on the two squadron charts and on Squadron now.

## Trial record

Code: branch `acs-terrain-avoidance` at f286e2a plus the uncommitted ADR 166 work (3598 tests
passed, 2026-10-31 02:16). Game client: the build the operator's screenshots were taken on
(2026-10-31 01:44). Readings at every lobby (`every_min` 0 in `config.local.yaml`).

| Session | Account | Navigations | Read | Matched the frame by eye | Back in the lobby | Notes |
|---|---|---|---|---|---|---|
| 20261031_0217, `make r1` | acct1 | 2 | 2 | 2 of 2 (659, 358,046) | 1 of 1 checked | stopped at 02:18 with `z`: two bugs, below |
| 20261031_0235, `make r1` | acct1 | 5 so far (to 03:00) | 5 | 5 of 5 | 5 of 5 | with both fixes; running |

**02:17, the first live run.**
- The first navigation (measured) went lobby, menu, leaderboards, SQUADRONS tab, read, back arrow,
  lobby. It took 8.3 s from `opening` to the reading at 02:17:50, and every step's screen came on
  the first look. Rank 659 and 358,046 trophies match the saved frame by eye; the operator's
  screenshot at 01:45 showed 358,032.
- The reading reached the stats file, the session summary (`Squadron ranking : rank 659, trophies
  358,046`) and, through the after-session ingest, the `squadron_rankings` table, tagged `acct1`.
- **Bug 1, readings back to back.** With `every_min` 0 a reading was due again the moment one
  ended, so the second navigation opened at 02:17:55 in the same lobby; no round would ever have
  started. Fix: at most one reading per lobby visit (`SquadronRanking.lobby_left`, called when the
  FSM leaves `GAME_LOBBY`).
- **Bug 2, PLAY clicked during a navigation.** At 02:17:56.099 the quick-scan clicked PLAY 0.15 s
  after the second navigation started. Its hold check runs at the top of each cycle, and the PLAY
  hold asked `due()`, which is false while a navigation runs. The two clicks raced: "XTest click
  ... STILL OFF TARGET". The second reading (02:18:03, 659 and 358,046, matching its frame) was
  taken on the leaderboard over the matchmaking screen. Fix: the PLAY hold asks `holds_play()` (due
  or running); the navigation is marked active before its attempt is booked; and the quick-scan
  checks `lobby_errand_active()` again before any PLAY or READY click.
- The session was stopped with `z` in `GAME_WAITING`, which closes the game at once (the run's own
  error: the press was not made conditional on the state).

**02:35, the second run, with both fixes (measured to 03:00).** Five lobbies, five navigations, one
each. Every one read, came back by the back arrow, and was followed by the PLAY click and a battle.
There was no `STILL OFF TARGET`, no ESC press, no stall count and no give-up. All 7 readings so far
match their frames by eye. They follow the count both ways: 358,046, 358,039, 358,047, 358,059. The
rank went from 659 to 660 between the runs with the trophies unchanged, which the frames confirm.
From `opening` to the reading took 5.6 to 8.3 s, and back arrow to lobby 0.8 to 1.8 s.

# ADR 170 — A Missed Squadron Reading Falls to the Next Session

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-01 | 1.9.4           |

Supersedes, in [ADR 166](166-read-the-squadron-leaderboard-from-the-lobby.md) D6, "Nothing is saved
between sessions", the rule that a session started after `queue_at` waits for the next day's
reading, and the rule that a navigation that gives up is not tried again that day. The rest of D6
stands: one reading a day at most, queued at `queue_at` and taken at the next lobby.

Operator, 2026-11-01: "sometimes wingman may not be running at 9:38:30 so we need to implement
mechanism that if we miss a logging the next session during that day should record the rank and
trophy count", and after the first two live tries: "read the latest log if it fails it should
retry to get rank and trophy".

## Decision

**D1. The day of the last reading is saved.** `~/.config/wingman/squadron_ranking.json` holds
`{"last_read": "2026-11-01"}`, beside the portal's restore token. It is per machine, and one file
serves every account: the figures are the squadron's, whoever reads them. It needs no lock, because
only one game process runs at a time
([Research 005](../research/005-multi-account-run-targets.md)).

**D2. A session starts from that day.**
- Today already read: the next reading queues tomorrow at `queue_at`.
- Otherwise it queues today at `queue_at`. If that time has passed, it is due at once: the session
  logs `SQUADRON RANKING: today's reading is still owed` and its first solo lobby takes it.

**D3. A day is a calendar day, local time.** A reading is never owed before that day's `queue_at`.
A day that was missed is not read the next morning at 02:00: it would give the next day two
readings. A navigation counts for the day it started on, even if it ends after midnight.

**D4. Only a reading counts.**
- The day is saved only for a reading. A navigation that gave up or was stopped leaves the day owed.
- The day is saved after the reading is logged and booked. In the other order, a session killed
  between the two would lose the reading and also stop the next session from taking it.

**D5. A navigation that read nothing is tried again.**
- It is tried again at once in the same lobby, with PLAY held, up to 3 tries in a row
  (`TRIES_PER_LOBBY`). The menu closes and the lobby comes back, so the next try starts within a
  few seconds, once the quick-scan has read PLAY again.
- After those, PLAY goes ahead, so a failing route holds up no round for long. The first lobby at
  least 120 s later (`RETRY_AFTER_S`) gets 3 more. Rounds run longer than that, so this is in
  practice the next lobby.
- A session stops at 9 tries (`MAX_TRIES`) and waits for the next day, so a route a game update has
  broken costs a session about a minute and a half, not a share of every round. A new session starts
  its count again, because the day is still owed.
- Every try still needs ADR 166 D5's fresh PLAY read. A failure in a lobby that is not on screen
  waits for PLAY to come back, and cannot spin.
- The next queue is set before the navigation stops counting as active. The other way round, a
  reading just taken would look due for an instant, and the main loop could start a second trip.

**D6. A bad file costs a reading, not a day.** A missing or damaged file reads as "not read today",
and a file that cannot be saved logs a warning and keeps the reading. The worst of either is a
second reading that day.

## Why

The daily schedule has not taken a reading yet. On both days since it went in, no session was flying
at 09:38:30:

| Day | Before 09:38:30 | Next session | That session booked |
|---|---|---|---|
| 2026-10-31 | wingman went to standby at 09:24:26 | 09:41:14 | 2026-11-01 |
| 2026-11-01 | the 08:03:43 session had ended by 09:16:28 | 09:42:57 | 2026-11-02 |

The last navigation on record is 2026-10-31 04:56:55, under the every-lobby schedule that the daily
one replaced. Each next session started less than five minutes late. ADR 166's start-time rule
existed only to keep a restart from reading twice. A saved day does that job, and no longer costs
the day.

## Consequences

- **Time.** The first round of a late session waits for the trip: 6.7 to 12.2 s, median 8.2 s (ADR
  166 V3). A failed trip costs about 11 s more, as at 11:02 below, so 3 in a row cost about 30 s,
  and only in a lobby where the reading has not been taken.
- **Gaps.** A day still has no point if no session runs after `queue_at` that day, if every session
  runs out of tries, or if a session stays in a party. A squad lobby shows READY, not PLAY, so no
  navigation starts there (ADR 166 D5). The 10:33 session below had no solo lobby after its first
  try.
- **A new path.** A reading can now be due from the start of a session, not only part way through
  one. The PLAY hold cannot stick in `GAME_UNKNOWN`: the quick-scan does not read lobby crops there,
  and in `GAME_LOBBY` it stamps the PLAY read before it holds the click.
- **First use.** With no file yet, the next session on or after `queue_at` reads once.
- **Implementation.** `wingman/squadron_ranking.py` holds the file and the rule (`SquadronRanking`,
  `load_last_read`, `save_last_read`). `wingman/main.py` passes the file and logs the owed reading.
  No config key changes.

## Validation

- **V1, a late session reads.** Met 2026-11-01 11:04, below.
- **V2, a restart does not.** Met 2026-11-01: the sessions of 11:23:07 and 11:23:31 logged `next
  reading queues at 2026-11-02 09:38:30` and had no `opening` line.
- **V3, the next session tries again.** Met 2026-11-01: after two `not read` lines, each next
  session logged `today's reading is still owed`.
- **V4, a retry in the same lobby.** Not yet seen. After a `not read` line, the log shows `due again
  now`, no PLAY click, and a new `opening the squadron leaderboard`.

## Trial record

The three sessions of 2026-11-01 on D1 to D4, all acct3 and before D5 existed:

| Session | Result |
|---|---|
| 10:33:32 | `the lobby is not on screen`, 60 ms after opening, with nothing clicked. The saved frame shows the HANGAR page. Later lobbies were READY lobbies, so no retry could have started |
| 11:02:39 | `the leaderboards did not open`. The menu opened and the click on LEADERBOARDS at 11:02:56 did nothing. The menu button shut the menu at 11:03:05, and PLAY went at 11:03:07 |
| 11:04:01 | Read: rank 633, trophies 370,081, 9.8 s after opening. The day was saved |

The click at 11:02:56 was logged as delivered to (1588, 336), and its cause is not established.
- It is not the account (the same account read at 11:04) or the party panel (the menu draws over it).
- It came 0.76 s after the menu click, the shortest of the 18 trips on record, but the next shortest
  worked at 0.78 s.
- The operator's mouse was over the game window, but two good trips clicked LEADERBOARDS that way
  too (2026-10-31 04:44:50 and 04:56:55).
- The saved frame shows the lobby, because it is taken after the navigation has gone back.

D5 does not need the cause: the menu shut cleanly and the next trip worked. The 11:03:05 click also
meets ADR 166 V4: a second click on the menu button shuts the menu.

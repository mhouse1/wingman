# ADR 161 — The Lobby Walk-Back Waits Six Reads

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-18 | 1.9.2           |

Supersedes the count in [ADR 102](102-lobby-recheck-from-game-starting.md) D5 (three reads). The
rest of ADR 102 stands: consecutive reads only, a read-only recheck, the scoped
`starting_play_visible` edge, the click-suppression reset (D6), the health floor (D7) and the
READY crop (D8).

## Decision

**D1. `GAME_STARTING` walks back to `GAME_LOBBY` after six consecutive PLAY or READY reads, not
three** (`STARTING_PLAY_CONFIRM_READS = 6` in `wingman/analyzer.py`). At the quick-scan's
1.1 s cadence that is about 6.6 s of the button continuously on screen.

## Consequences

- A match that is starting and flashes the lobby button for up to five reads is no longer walked
  back. Three-read flashes lost three matches on 2026-10-18 (08:09:13, 09:42:39, 12:55:09),
  each a round of about six minutes with nobody flying.
- A start that really failed is walked back later than before: about 3.3 s when its button stays
  up until it is clicked, and longer when the button comes and goes. The first live walk-back
  (2026-10-18 13:41:19) followed bursts of 2, 2 and 3 reads, so it came 14 s after the three-read
  rule would have fired. Either way far inside the 150 s timeout ADR 102 replaced.
- A failed start whose PLAY reads come and go never makes six in a row, as before with three; the
  150 s timeout to `GAME_STARTING_STALLED` remains its backstop (ADR 102 rev 2, E4).

## Why

Measured over the 1,056 `GAME_STARTING` phases of 2026-10-15 to 2026-10-18
([Anomaly 012](../anomaly/012-lobby-walk-back-abandons-starting-matches.md)): 21 phases that
went on to a battle showed the button for 1 read (13) or 2 reads (8). Of the 301 walk-backs at
three reads, the lobby was real in 269 (a click, an UNREADY read or `GAME_WAITING` came next);
in 32 the lobby crops were gone first, and in 3 of those the button vanished on the very next
read and the match started without wingman. The longest flash seen in those logs is three reads,
but the three-read rule cut every longer one short: live on 2026-10-18 at 14:23 a party start
showed READY for five reads and then started. So six leaves one read of margin in the party flow,
and a start walked back at six that then plays without wingman calls for the first alternative
below.

## Alternatives considered

- **Undo the walk-back when the lobby goes dark right after it** (a scoped `GAME_LOBBY` to
  `GAME_STARTING` edge). It would recover a flash of any length, but it adds an edge and a window,
  and a popup dimming the lobby just after a correct walk-back would send a real lobby to
  `GAME_STARTING` and its 150 s timeout. Kept in reserve for a lost match after this change.
- **A time window instead of a streak.** ADR 102 rev 2 E4 replayed it on September's phases and
  it aborted real starts as often as it helped.

## Validation

- **V1.** `tests/test_starting_play_recheck.py`:
  `test_a_found_match_flash_cannot_walk_the_start_back` fails on three reads;
  `test_the_debounce_is_far_cheaper_than_the_timeout_it_replaces` keeps the streak under a tenth
  of the timeout.
- **V2, live.** No walk-back followed by a lobby blackout or by `GAME_LOBBY → GAME_BATTLE`. Every
  `PLAY visible in GAME_STARTING (3/6 reads)` to `(5/6 reads)` line in a phase that reaches
  `GAME_BATTLE` is a match the three-read rule would have abandoned. Walk-backs still followed by a
  click and `GAME_WAITING`.

# ADR 094 — Finish-Round-Then-Exit Hotkey

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-25 | 1.8.6           |

## Context

Stopping wingman today means Backspace (or Ctrl-C), which sets `exit_requested`
and breaks the main loop on the next tick. That is correct for "stop now" and
wrong for the far more common intent: **"stop when this round is over, and shut
the game down too."**

Pressing Backspace mid-battle abandons an aircraft in flight:

- The round is scored as an abandonment rather than a click-to finish. A manual
  mid-battle stop writes a false **"Lobby exit"** into the session summary, so
  the mission-outcome percentages stop meaning what they say.
- A soak being measured for ADR 092's leak gate ends on a partial round.
- MetalStorm is left running. It leaks ~165 MB/h on its own (Anomaly 002) and is
  now the only growing process on the machine, so an abandoned client keeps
  consuming memory until someone notices it.

The operator's real choice today is therefore: interrupt at a bad moment and
tidy up the game by hand, or sit and wait for the lobby to press Backspace at
the right one. Neither lets a session be left to end itself.

**The deferral mechanism already exists twice.** ADR 090's memory guard and
ADR 093's liveness guard both wait for the same safe point:

```python
_safe = (analyzer.game_state == GameState.GAME_LOBBY
         and not ctrl.is_mission_running())
```

Both then `break`, sharing the single shutdown path that writes the session
summary, performance artifacts and mission stats. This ADR adds a third caller
of that pattern — plus one new step at the end.

## Decision

Add `FINISH_ROUND_THEN_EXIT`, bound to **`z`**: finish the round in progress,
stop at the next lobby, then close MetalStorm.

The name is ordering, not decoration. Three things happen, in this order:

1. **Finish the round** — the press defers; nothing stops immediately.
2. **Then exit wingman** — `break` at the first tick where no round is in
   progress, through the existing shutdown path.
3. **Then close MetalStorm** — after wingman's own artifacts are written.

### Deferral semantics

- Pressing `z` sets a flag. It does not stop anything immediately *if a round is
  in progress*.
- The session ends at the first tick where **no round is in progress** — that is,
  the state is not one of `GAME_BATTLE`, `GAME_BATTLE_MANUAL` or
  `GAME_BATTLE_EJECT` (`analyzer.BATTLE_STATES`).

  **Amended 2026-08-29.** This originally reused the guards' `_safe`
  (`GAME_LOBBY` and no mission lock). That was wrong, and wrong in the state the
  hotkey is most often pressed from: wingman starts in `GAME_UNKNOWN` and stays
  there until the first classification, so `z` pressed at startup did nothing
  at all. It was equally dead in `GAME_WAITING`, `GAME_STARTING` and
  `GAME_END_B` — none of which have an aircraft in flight to protect.

  The guards and the hotkey were conflated because they share a `break`, but
  they mean different things. A guard exit means "restart wingman at a clean
  moment" and should still wait for a real lobby. `z` means "stop unless that
  would abandon an aircraft in flight". The guards keep `_safe` unchanged.
- **No automatic path may start a new round while the exit is pending.** The
  lobby quick-scan clicks PLAY within a second or two of reaching the lobby, so
  a press made *in* the lobby lost the race and cost the operator a further full
  round. The gate sits at the analyzer's click site rather than in the
  `LOBBY_PLAY_CLICK` subscriber, because that site also fires
  `_trigger("play_clicked")` — a subscriber that declined to click would still
  leave the FSM in `GAME_WAITING`. Invite-accept and stall-recovery PLAY clicks
  are gated on the same rule.
- Pressing `z` again **cancels** the pending exit. A deferred action that cannot
  be recalled is a trap: the operator gets no feedback for minutes and no way
  back except killing the process, which is the thing being avoided.
- Backspace and Ctrl-C keep their current meaning: stop now, mid-battle if
  necessary, and **leave the game running**. The two intents are different and
  both are needed.

### Closing the game

**Signal the process; do not drive the in-game menu.** Send `SIGTERM` to the
MetalStorm processes, wait a short grace, and escalate to `SIGKILL` only for
anything still alive.

Three reasons this is the right mechanism here:

- It is the **established path**. `make launch-game` already does exactly this
  (`pkill -f Metalstorm.exe`, 5s settle) before every relaunch, so it is
  exercised constantly and known not to corrupt the prefix.
- There is **nothing unsaved**. The round has completed and the client is
  sitting at the lobby; progression lives on the backend.
- The alternative — pressing ESC and clicking "Exit to Desktop" — means clicking
  an **uncalibrated** button. Only the *Cancel* button of that modal is
  calibrated (`STALL_EXIT_TO_DESKTOP`); Exit sits roughly 130px beside it. An
  uncalibrated click at a guessed offset is precisely how the PROFILE overlay in
  Anomaly 001 got opened and stranded a session for 110 minutes.

**Find the processes via `/proc`, not `pkill -f`.** `resource_monitor`'s
`_find_game_pids` already locates them by `comm` and can be reused. This is not
fastidiousness: the Makefile carries a comment explaining that its recipe splits
the pattern through a shell variable *specifically so `pkill -f` cannot match and
kill the recipe's own shell*. A `/proc`-based lookup cannot match wingman itself
and needs no such trick.

### Ordering and failure handling

Wingman writes its session summary, performance artifacts and mission stats
**before** touching the game. If closing the game hangs or fails, the session
data is already safe on disk.

Closing the game must never prevent wingman exiting. Any failure is logged and
swallowed; the exit proceeds. A stop command that can itself fail to stop is
worse than one that occasionally leaves a window open.

### Configuration

```yaml
finish_round_then_exit:
  close_game: true          # false stops wingman only
  game_term_grace_s: 5.0    # SIGTERM to SIGKILL, matching launch-game's settle
```

### Feedback

A deferred action with no acknowledgement is indistinguishable from a missed
keypress:

```
🏁 FINISH ROUND: requested — wingman will stop at the next lobby, then close
   MetalStorm (ADR 094)
🏁 FINISH ROUND: cancelled — the session continues
🏁 FINISH ROUND: stopping at lobby — closing MetalStorm (N process(es))
```

### Why `z`

Free — `test_keybindings.py` asserts no game key doubles as a wingman hotkey, and
`z` was unused. Physically distant from the flight keys `i/j/k/l` and from
Backspace, and not adjacent to `x` (weapon loop) or `v` (screenshot), so a slip
does not trigger something surprising. It is **grabbed, not injected**, so no
in-game rebinding is needed, and it is absent from `_WATCHED_MANEUVER_KEYS` so
it cannot read as manual takeover (ADR 070 d4).

### Interaction with the guards

If a guard also wants to stop, the outcome is one `break` at the same safe point,
so no precedence rule is needed. A guard-triggered stop does **not** close the
game: the guards exist to bound wingman's own resource use, and ADR 090
explicitly frames a guard exit as "restart to reset perception latency" — an
operator restarting wingman wants the game still up.

`FINISH_ROUND_THEN_EXIT` gets no hard timeout. The guards have one because a leak
or livelock can prevent the safe point arriving; an operator who wants "stop
regardless" already has Backspace, and a timeout here would mean choosing a
number with no evidence behind it.

## Consequences

- A session can be told to end cleanly and left alone — the normal case for an
  unattended soak — and the machine is left with no orphaned game client.
- Round accounting stays honest: no more false "Lobby exit" entries.
- **A pressed `z` can sit pending for up to a full round** (~4m 40s average,
  longer with respawns). That is intended, and it is why the cancel and the
  acknowledgement line are part of the decision rather than polish.
- Wingman gains the ability to terminate another process. That is a real
  escalation in what it does, confined to one code path, gated by config, and
  targeted by `/proc` lookup rather than a name pattern.
- Restarting after this exit costs a full game launch (~1-2 min) rather than
  reusing a warm client. `make r`/`r1` already relaunch the game anyway, so the
  normal workflow is unaffected.

## Alternatives considered

**Reuse `exit_requested` with a "defer" flag.** Rejected: it is also set by
SIGTERM and by the replay/capture paths, where deferring would be wrong.

**Make Backspace itself deferred.** Rejected — it removes the ability to stop
now, and silently changes a key operators rely on.

**Close the game from the Makefile after wingman exits.** Rejected: wingman is
the only thing that knows the round finished, and the hotkey has to work for a
session started any way. It would also close the game after a *guard* exit,
which is wrong (see guard interaction).

**Drive the in-game Exit-to-Desktop dialog.** Rejected — an uncalibrated click
beside a calibrated Cancel button. See "Closing the game".

**A timeout escalating to an immediate stop.** Rejected: no evidence for the
number, and Backspace already covers it.

## Validation

- **V1 — deferred, not immediate.** Pressing `z` during `GAME_BATTLE` does not
  end the session; the mission completes.
- **V2 — ends as soon as no round is in progress.** *(Amended 2026-08-29;
  originally "at the safe point", meaning `GAME_LOBBY` and no mission lock.)*
  The session ends on the first tick where the state is not in `BATTLE_STATES`,
  including `GAME_UNKNOWN` at startup.
- **V10 — a press in the lobby does not start another round.** The quick-scan
  declines to click PLAY while the exit is pending, so the state stays
  `GAME_LOBBY` and the exit fires on the next tick.
- **V11 — the guards are unchanged.** `_safe` still gates the ADR 090 and
  ADR 093 exits.
- **V3 — cancel works.** A second press clears the pending exit; the session
  continues past the next lobby.
- **V4 — clean shutdown.** The exit writes the session summary, performance
  artifacts and mission stats. Guaranteed by construction — this adds a `break`
  in the existing loop, not a new exit path (see ADR 090's Validation note on the
  seven `break` statements and the absence of any bypass).
- **V5 — the hotkey does not reach the game.** `z` is grabbed, not injected, and
  is absent from `_WATCHED_MANEUVER_KEYS`.
- **V6 — the game is closed, after the artifacts.** No `Metalstorm.exe` process
  survives the exit, and the run JSON and stats file are on disk before the
  signal is sent.
- **V7 — a failure to close does not block the exit.** With the game process
  unkillable or already gone, wingman still exits cleanly and logs the reason.
- **V8 — `close_game: false` stops wingman only**, leaving the client running.
- **V9 — a guard exit leaves the game running.** ADR 090 and ADR 093 stops are
  unaffected by this ADR.

## References

- ADR 090 — memory guard; origin of the `_safe` condition and the single
  shutdown path
- ADR 093 — liveness guard; the second caller of that pattern
- Anomaly 001 — the uncalibrated-click failure that rules out driving the
  in-game exit dialog
- Anomaly 002 — the game-side leak that makes an orphaned client cost something
- ADR 070 d4 — why a wingman hotkey must not collide with a maneuver key
- `wingman/keybindings.py` — the binding

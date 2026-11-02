# ADR 169 — `z` in the Lobby Exits, Whatever the FSM Believes

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-11-01 | 1.9.4           |

Amends the press behaviour of [ADR 094](094-finish-round-then-exit-hotkey.md) and
the standby hotkey set of [ADR 099](099-nested-display-lane-for-unattended-operation.md).

## Decision

`z` means "I am done", so a press with the lobby on screen ends wingman and
MetalStorm, even when the FSM disagrees.

1. **A press in a battle state reads the screen.** PLAY or READY on the newest
   frame means there is no round to finish: the FSM walks back to `GAME_LOBBY`
   and the main loop's existing safe point stops the session on its next tick.
   The read runs on the OCR pool, not on the key listener's thread, which also
   hears the takeover keys (SAF-001).
2. **A repeat press does not cancel a stop that has nothing left to wait for.**
   Outside the battle states and `GAME_END_B` it logs `already stopping`. In
   those two states a second press still cancels, as ADR 094 requires.
3. **`z` works in standby.** It requests the close, as a second Backspace does.
   Standby keeps Backspace and `z` and releases the rest.

## Consequences

- A press in a real battle is unchanged apart from two OCR reads on the pool.
- A false PLAY or READY read in a real battle would end that round. It needs a
  press of `z` as well, and the operator has asked to stop by then.
- The read happens once per press. A press on another lobby page, with the FSM
  wrong and neither button showing, still defers. Pressing again cancels it and a
  third press reads the screen again.
- `z` in standby closes the game without reading the screen, because standby has
  no OCR pool left. A press during a hand-flown round closes that round too. On
  the operator's own display it still needs ctrl+alt, so typing cannot trigger it.

**Not changed:** how a battle is entered. The false battle below can still
happen, and until someone presses `z` or double `n` the mission flies into the
lobby. That fix belongs in the GAME_STARTING probe and needs its own live trial.

## Context

Session of 2026-11-01. `P` is the lobby quick-scan, `H` the health probe.

```text
09:14:57,474 H #2 raw=77 UNCONFIRMED               P: PLAY visible (2/6)
09:16:09,561 H #50 raw=73, health 73 confirmed     (ADR 063: 73 is within 15 of 77)
09:16:09,636 Controller: mission 'acs' started → GAME_BATTLE
09:16:09,725 P: PLAY visible in GAME_STARTING (1/6 reads)
09:16:14,844 FINISH ROUND: requested — wingman will stop at the next lobby
09:16:16,366 FINISH ROUND: cancelled — the session continues
09:16:17,251 Controller: Backspace — ending wingman
09:16:18,668 STANDBY: wingman has stopped
09:16:19,119 FINISH ROUND: requested — … (four more presses, toggling a flag nothing reads)
09:16:28,031 STANDBY: MetalStorm exited on its own
```

The last HUD frame, written 09:16:17, shows `[GAME_BATTLE] HP:73` over the main
lobby with PLAY lit. Three separate holes:

- **The FSM was wrong, and `z` trusted it.** Both digit reads coincided with PLAY.
  The 72 s between them does not matter to the recurrence filter, which counts
  reads, not time.
- **The second press cancelled the first,** 1.5 s later.
- **Standby ignored `z`.** It reached the handler only because `unhook_all()`
  leaves the listener's key table full, so the handler ran and nothing read
  its flag.

`z` was never broken when the FSM agreed: at 07:58:20 the same morning a lobby
press stopped the session in 0.7 s.

How often: of 918 GAME_STARTING health confirmations in the logs of 2026-10-23 to
11-01, 15 read under 100 HP and the rest read 100 or more. Those logs hold 6
transitions from a battle state to the lobby, and all 6 are the operator's double
`n`, so nothing recovers from this automatically. Twice more they ended the session
with Backspace within 20 s of such a read.

## Validation

- Offline, real OCR: the PLAY and READY crops over all 246 archived frames gave
  23 hits, all lobby screens: 12 checked by eye, and the other 11 are the two
  reference lobby frames and repeats of the same dwells. None of the 37 `blind_*`
  in-flight frames or the three battle HUD frames gave one.
- Offline, real OCR, pool, FSM and hotkey closure on the nine PATH1 frames, forced
  to `GAME_BATTLE`: both lobby frames walked back and the other seven stayed.
- `tests/test_finish_round_then_exit.py`: reverting each of the five parts of this
  change fails the test written for it.
- **Open:** a live press on a false battle. Expected log: `FINISH ROUND: PLAY is
  on screen in GAME_BATTLE`, then `stopping in GAME_LOBBY`, within about 2 s.
- **Open:** a live press in standby. Expected log: `'z' in standby — closing`.

## Known defect, not fixed here

`_LinuxXTestKeyboard.unhook_all()` never clears `_grabbed`, so on Linux every
hotkey stays registered through standby, whatever the `every hotkey released` line
says. Only `z` was observed firing there (09:16:19, after that line). The narrowing
in `Controller.cleanup()` is therefore not in force. `z` is now registered
explicitly, so fixing the leak will not take it away.

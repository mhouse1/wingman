# ADR 105 — Close the Nested Display When the Game Exits

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-02 | 1.8.8           |

## Context

MetalStorm exits and leaves a black `Xwayland on :3` window behind. Wingman
keeps running, capturing an empty framebuffer.

The teardown in `main()` closes the game and then the nested display, but only
for an **operator-initiated** stop — the ADR 094 deferred exit (`z`) or
Backspace. A game that exits on its own is neither, so nothing reacted to it.

The cost is not cosmetic. On 2026-09-01 the game servers went into maintenance
and MetalStorm exited at about 22:22. Wingman ran for a further **4h51m**:

```
Analyzer: Health OCR no digits for 17261.6s → game_battle_alive=False
FocusGuard: focus unresolved (8000 so far) - injecting anyway (ADR 098 D4)
```

Every crop empty, `incoming_template score=0.000` — the signature of a uniform
frame — and thirty 'u' presses into a display with no game on it. The session
recorded twelve missions, eleven real and one phantom that never ended, and
inflated the average mission time from about six minutes to twenty-eight.

Nothing detected it because every check wingman has was satisfied. Frame age was
healthy: the grabs kept succeeding. The ADR 093 liveness guard counts OCR
*executions*, not results, so a full-rate OCR finding nothing reads as maximum
progress. The ADR 074 anomaly capture is scoped to `GAME_LOBBY` blackouts.

## Decision

**D1. Watch for the game process disappearing, and end the session when it
does.** `GamePresenceWatch` polls `find_game_pids()` — the /proc scan the
shutdown path already uses — and reports when the game was running and is now
reliably absent. The main loop treats that like the other guard exits: log,
`break`, and run the ordinary shutdown so every artifact is written.

This is a positive test on the process table, not an inference from an absence
of OCR results. A blank screen has other causes — a loading screen, a stalled
pipeline — and the 2026-09-01 session shows how long an inference-based check
can take to be sure. The process either exists or it does not.

**D2. Arm only after the game has been seen.** A session can start before the
client finishes launching. Without this, wingman would stop itself immediately
on every cold start.

**D3. Two agreeing absent reads, at a 5 s poll.** The gap between a crash and a
relaunch, and a /proc scan racing process teardown, both look like a single
absent read. Ten seconds is nothing against the 4h51m it replaces and long
enough not to fire on a blink.

**D4. Rate-limit the scan inside the watch.** The caller invokes it every 1.5 s
tick; the answer changes on the scale of a session. The limiting lives in the
watch so no caller has to remember it.

**D5. No safe point.** The ADR 090 and ADR 093 guards wait for a lobby with no
mission running, so a restart never abandons an aircraft in flight. There is no
aircraft when there is no game, so this exit fires wherever it is.

**D6. Close the nested display, and do not gate it on `close_game`.** The
display exists only to host the game; an empty server *is* the black window in
the report. `close_game: false` means "do not kill a running game", and there is
no running game to protect — honouring it here would preserve the exact artifact
this ADR removes. The game close is skipped, because there is nothing to close.

**D7. The launch path reaps its own display too.** D1 covers a game that dies
while wingman is watching. `make g` launches the game with no wingman to watch
it, so the same leftover appears when the launch itself fails — observed
2026-09-02:

```
wine: Call from 00006FFFFFF59AE0 to unimplemented function
      UIAutomationCore.DLL.UiaDisconnectAllProviders, aborting
```

`launch-game` kills any running MetalStorm before a fresh launch, the relaunch
aborted in Wine, and `wait-game` timed out and exited 1 — leaving `:3` up with
no client on it, which is the black window. `wait-game`'s failure path now stops
the nested display and names the launch log before exiting.

The rule is the same one D6 states: the display exists only to host the game, so
it does not outlive one. What differs is who notices — wingman when it is
running, the launch target when it is not.

## Consequences

A game that crashes, is closed by hand, or is taken out by a server outage now
ends the session within about ten seconds instead of running until a guard
notices, and takes its display with it.

Sessions that end this way stop mid-round, so a mission in progress is recorded
as incomplete. That is accurate: the round ended when the game did.

Wingman will now also exit if the operator closes MetalStorm deliberately while
a session runs. That is the intended reading — the game is what wingman is
automating — but it is a behaviour change from "wingman keeps running".

This does not address the deeper defect the same session exposed: the liveness
guard counting OCR executions rather than results, so a blank screen still reads
as progress. That remains open. D1 covers the case where the process is gone,
which is the common one, and not the case where the game is running but showing
nothing.

## Validation

- **V1.** A game that never starts does not stop the session, however long it
  takes to appear.
- **V2.** A game that exits ends the session after two absent reads.
- **V3.** One missed scan between two present ones does not end the session.
- **V4.** The scan is rate-limited: ten ticks in ten seconds produce two scans.
- **V5.** A failing /proc scan never takes the main loop down.
- **V6.** The nested display is closed on this path regardless of `close_game`.
- **V7.** The exit is not gated on a safe point.
- **V8 — live.** A session whose game exits leaves no `Xwayland :3` process and
  no black window. Not yet observed; this ADR is Draft until it is.
- **V9.** A failed `make g` — game never appears — leaves no `Xwayland :3`
  process. The 2026-09-02 leftover was cleared by hand with
  `nested-display.py stop`, confirming the teardown works; the automatic path
  has not yet been exercised by a failing launch.

## References

- ADR 094 — the operator-stop teardown this sits beside, and why it closes the
  game before the display
- ADR 099 — the nested display lane; the display this closes
- ADR 093 — the liveness guard that eventually ended the 2026-09-01 session,
  and whose progress definition is still wrong for a blank screen
- ADR 098 — the focus guard, whose 8,000 "focus unresolved" warnings were the
  other unheeded signal that session

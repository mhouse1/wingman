# Anomaly 009 — "Click to Continue" Clicks Stop Registering After the Ubuntu 26.04 Upgrade

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

## Summary

**Status: fixed 2026-10-02 by ADR 156 (per-click detach); the operator confirmed their mouse works in the game window. Open only for the soak count: 2 of the 10 match ends ADR 156 asks for.**
Xwayland 24.1.10 on the nested display delivers XTest pointer events to a window
only while the operator's real mouse is over the `:3` window. See *Cause* below. On the end-of-match podium screen
wingman sends bursts of seven clicks at the "Click to Continue…" text and the
screen does not advance. Wingman forces `GAME_LOBBY` after each burst, the
still-visible text sends it back, and the loop repeats about every 5 s. The
round restart is delayed by one to five minutes, and in 3 of 11 episodes the
screen never cleared before the session ended.

It began with the first session after the Ubuntu 26.04 upgrade on VEDA
(2026-09-29 22:23 to 22:53: GNOME Shell 46 to 50.1, Xwayland to 24.1.10).

## Evidence

Measured over every archived session in `logs/` (an episode is the run of
`Clicking click_to_continue` bursts between two rounds):

| Period | Episodes | Cleared by the first burst | Typical time to clear | Never cleared |
|---|---|---|---|---|
| 2026-09-13 to 09-28 | about 2,570 | all but about 10 | 9 to 15 s | 84 (session ended) |
| 2026-09-29 to 10-02 | 11 | 1 | 83 to 321 s | 3 |

- Last good session ended 2026-09-28 17:21. First bad one ended 2026-09-29 23:03.
- The four commits in that gap (`a965163` to `b150eb0`) do not touch
  `_click_through_game_end`, `click_crop`, `_linux_click` or the `click_to` crop.
- 2026-10-02 04:41:26 to 04:42:23: 11 bursts (77 clicks) at (939, 1094), no
  advance. `tests/test-output/live_hud.png` at 04:41:26 shows the podium with the
  text under the click point, so the aim is right.
- No click errors and no focus-guard suppression in the log. Pursuit had stopped
  (`PURSUIT SUMMARY: end=external:match_ended`).
- Single clicks still work in the same sessions: PLAY at 04:35:22 took effect
  within 3 s, and a `FINAL_CONTINUE` click on 2026-10-01 04:48:13 worked first time.

## Cause (measured 2026-10-02, 05:03 to 05:10, on the live podium screen)

1. **The pointer is on target.** With the read-back below, 81 bursts logged
   `pointer at each click: (939,1094)` seven times each. Explanation 1 is ruled out.
2. **The X server says the pointer is over no window.** `QueryPointer` on the
   `:3` root returned `child = None` at (939, 1094), while `TranslateCoordinates`
   (pure geometry) returned the `Wine Desktop` window. XTest absolute and relative
   moves and `XWarpPointer` all moved the pointer and all left `child = None`.
   A button event delivered while the pointer window is the root reaches no client.
3. **Nothing else explains it.** No key or button was held (mask: NumLock only),
   focus was on `Metalstorm`, no EI socket in Xwayland's environment.
4. **Not fixable by sending events differently.** `XSendEvent` button events to
   each of the three game windows did nothing (Wine ignores them), and Return and
   Space do not dismiss the podium.
5. **The mechanism is in Xwayland 24.1.10.** `hw/xwayland/xwayland-input.c`:
   `xwl_xy_to_window` returns the root window when `sprite_check_lost_focus` is
   true, and for an event whose last slave is not the Wayland pointer (every
   XTest event) that function returns `!pointer_crossing`, where
   `pointer_crossing = (xwl_seat->pointer_enter_count > 0)`. So the pointer
   window is the root unless the host pointer is inside the Xwayland surface.
   The 24.1.6 source does not have that term. The function returns FALSE early
   when no `xwayland-pointer` device shares the sprite being checked.
6. **Disabling the host pointer devices restores delivery and crashes the
   server.** At 05:10:24 `xwayland-pointer`, `xwayland-relative-pointer` and
   `xwayland-pointer-gestures` were disabled through XInput ("Device Enabled" =
   0). The next XTest move reported `child = 0x400004` (Wine Desktop), the burst
   at 05:10:28 cleared the podium, and PLAY was detected at 05:10:30. At
   05:21:48 the nested Xwayland segfaulted at address 0x28 inside a Wayland
   event handler, mid-battle, and the session died with it (`pursue_and_engage
   loop cycle failed`, frame `None`, then `make` exit 2). `DisableDevice` frees
   the device's sprite and `pointer_handle_enter` uses it when the host pointer
   enters the window.
7. **Detaching them restores delivery without that.** Controlled A/B/A on a
   fresh `Xwayland :9` with one test window, 05:26: attached, `child = None`
   and the click delivered nothing; detached (floating slaves, still enabled),
   `child` = the window and ButtonPress and ButtonRelease arrived; reattached,
   `child = None` again. A floating slave has its own sprite, so the enter
   handler has something valid to work on. This is the fix in ADR 156.

The first PLAY click of that session (04:58:09) had also failed, so the fault
is not specific to the podium: it affects every click while the operator's mouse
is anywhere but over the nested window. It looked intermittent for that reason.

## Side effect seen while applying the fix by hand (2026-10-02 05:10 to 05:19)

The devices were disabled in the middle of the stuck loop, with wingman already
forced into `GAME_LOBBY`. The first click of the 05:10:28 burst cleared the
podium, the lobby scanner clicked PLAY at 05:10:30 while the burst was still
running, and burst clicks 5 to 7 then landed on the lobby. (939, 1094) is a
party "+" slot there, so INVITE PLAYERS opened. The follow-up click at the PLAY
position (1751, 1096) landed on the INVITE button of the bottom row (an offline
squadron member). Wingman does not recognise that screen: `GAME_UNKNOWN`, then
`LIVENESS GUARD` at 05:18:23. A click on the back arrow at 05:19 returned the
lobby (party still 1/5) and wingman resumed on its own.

A session that starts isolated clears the podium in `GAME_END_B`, where the
lobby scanner does not click, as it did in about 2,570 episodes before the
upgrade. The burst still relies on the screens after the podium absorbing its
extra clicks. That is unchanged and worth its own look if it recurs.

## What was not known before the read-back

The log recorded that clicks were sent, not where the pointer was. Two
explanations fit and the log could not separate them:

1. **The pointer is not at the click point** (inferred). After a battle
   something leaves or puts the pointer elsewhere on `:3`, so the burst clicks
   land off the text.
2. **The pointer is right and the game ignores the click** (inferred).

## Instrumentation (2026-10-02)

`wingman/input_linux.py::_linux_click` now reads the pointer back before every
click. Off target, it logs a WARNING with the real position, re-aims, and logs
where the pointer ended up (`STILL OFF TARGET` if the re-aim failed). On target,
the DEBUG line `XTest click: … pointer at each click: …` records it. A burst that
still fails with every click on target rules out explanation 1.

## Runs

| Run (start) | Code state | Game UI | Episodes | Pointer read-back | Result |
|---|---|---|---|---|---|
| 2026-10-02 04:35 | `b150eb0`+ (no read-back) | podium as in `live_hud.png` | 1 | not instrumented | 11 bursts, never cleared |
| 2026-10-02 04:57 | `85e66ab`+ read-back | same | 1 | 81 bursts, every click at (939,1094), `child = None` | cleared by the first burst after the host pointer devices were disabled by hand at 05:10:24; nested Xwayland segfaulted at 05:21:48 (disabled devices), session lost |
| 2026-10-02 05:30 (operator's `make rd`) | `85e66ab`+ ADR 156 detach | same | 5 | `ADR 156: host pointer isolated from :3` at start; 7 of 7 clicks at (939,1094) in each burst, no warning | all 5 cleared by the first burst (05:31:32, 05:37:59, 05:43:06, 05:50:39, 05:58:56); 28m 32s, 4 missions, stopped with `z` at the lobby, `ADR 156: host pointer restored on :3` at exit, nested server survived. This run had the whole-session detach: the operator's manual clicks did not register, the operator rejected that, and ADR 156 was changed to detach per click. The burst was delayed 37 s by a separate startup path: `GAME_END_B` entered from `GAME_UNKNOWN` does not click until its 31 s timeout |
| 2026-10-02 06:09 (`make r1`) | `85e66ab`+ ADR 156 per-click detach | same | 2 | `ADR 156: clicks on :3 detach the host pointer only while they are sent` at start; `(host pointer detached for this click)` on each click; devices attached (use 3) between clicks | FINAL_CONTINUE click 06:09:58 and the first PLAY click 06:10:03 both took effect; battle at 06:10:35. First match end 06:15:00: one burst, then FINAL_CONTINUE and two REVEAL_ALL popup clicks all landed, PLAY at 06:15:25. Second match end 06:21:55: one burst, lobby 06:22:02. No `pointer is over no window` warning. Operator pressed `z` at 06:21:40; clean exit at the lobby after 12m 34s, 2 missions. First launch attempt at 06:05 failed before wingman started (game window not up within the 20 s wait) |
| 2026-10-02 06:25 (operator's `make r`, console only) | same per-click detach | lobby | 0 | `XTest click 1/1 on :3: pointer at (210, 382), not the (1751, 1096) it was sent to; re-aimed, now (360, 486) — STILL OFF TARGET` | the PLAY click missed with the operator's mouse moving in the window (no detach: the pointer was over a window). Wingman went to `GAME_WAITING` with PLAY still visible and refused `m` seven times; operator stopped it after 1m 30s. Fix: detach when the aim is off target too |
| 2026-10-02 06:43 (`make r1`) | `85e66ab`+ ADR 156, detach on no window or off-target aim | lobby | 3 | every burst on target; READY click at 07:10:30 detached for `operator's mouse had it at (1320,39)` and landed | all 3 match ends cleared by their first burst (06:48:22, 07:02:15, 07:08:54). One PLAY click missed behind an UNLOCK popup and recovered in 27 s. Ended 07:12:55 by SIGTERM from the harness's 30-minute task limit |
| 2026-10-02 07:15 (`make r1 GAME_LAUNCH_DEPS=`, attached) | same, plus ADR 157 guard | attached mid-round | 2 | burst at 07:15:51 detached for `operator's mouse had it at (1913,690)`, 7 of 7 on target | both cleared by their first burst (07:15:51, 07:21:58); operator stopped it with `z` at 07:22:36 |

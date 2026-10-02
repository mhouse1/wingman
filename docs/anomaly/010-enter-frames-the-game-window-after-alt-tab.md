# Anomaly 010 — Enter Frames the Game Window After an Alt+Tab Away

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

## Summary

**Status: cause measured 2026-10-02; fix in ADR 157, live validation pending.**
Pressing Enter in the game window (the manual-takeover key) made the Metalstorm
window inside the Wine desktop grow a title bar with minimise, maximise and
close buttons. The game had switched from borderless to a framed window:
`Metalstorm` 1928x1234 with its content at +4,+30. Everything on screen moved
4 px right and 30 px down, so wingman's crops and click points no longer matched.
In the 06:43 session wingman sat in `GAME_BATTLE` while the game showed the
end-of-match screen, and the HUD overlay read `HP:?`.

## Cause

The game toggles borderless and framed on Alt+Enter, and Wine believed Alt was
held although the X server did not.

1. **Alt+Enter is the toggle** (measured). XTest Alt+Enter at 06:57:26 took the
   window from 1928x1234 to 1920x1200, and wingman detected "Click to Continue"
   2 s later. A bare XTest Enter at 06:57:12 and 06:57:15 changed nothing.
2. **The operator's presses carried no modifier in X** (measured). All logged
   `mods=none`, and wingman binds and held no modifier.
3. **Alt is left held on `:3` when the operator leaves** (measured). A watcher
   on the nested display reported Alt_L down for 3.0 s, 6.3 s and 24.1 s.
4. **A bare Enter then framed the window** (measured): Alt held 07:00:51 to
   07:00:58, Enter with `mods=none` at 07:01:28, window FRAMED at 07:01:28.
5. **Why** (from the Xwayland 24.1.10 source). `keyboard_handle_leave` releases
   held keys only for a rootless server. A rootful one keeps them down until the
   next `keyboard_handle_enter`, which clears them with `LeaveNotify` key events
   that no client sees as a `KeyRelease`. Wine never learns Alt came up.

It surfaced on 2026-10-02 because the operator's mouse works in the game window
again (ADR 156), so they can return to it by clicking. Returning with Alt+Tab
delivers a real Alt release and hides the problem.

Not the cause: the game's own settings code. `Player.log` shows one
`Applying resolution … screen_mode_fullscreen_window` at launch and nothing at
the time of the toggle.

## Evidence

| Time (2026-10-02) | Source | Observation |
|---|---|---|
| 06:52:53 | saved frame | no title bar |
| 06:52:56, 06:53:02, 06:53:05 | wingman.log | `'enter' pressed … mods=none` |
| 06:54 | screenshot of `:3` | title bar present, podium shifted down 30 px |
| 06:57:12, 06:57:15 | XTest probe | bare Enter: still 1928x1234 |
| 06:57:26 | XTest probe | Alt+Enter: 1920x1200, borderless |
| 06:57:29 | wingman.log | `Clicking click_to_continue` (screen readable again) |
| 07:00:53 to 07:00:58 | watcher | `Alt_L held on :3` for 6.3 s |
| 07:01:28 | watcher and wingman.log | Enter `mods=none`, window FRAMED |
| 07:01:39 to 07:02:02 | watcher | `Alt_L held on :3` for 24.1 s, until an XTest release |

## Runs

| Run (start) | Code state | Frame toggles | Guard releases | Result |
|---|---|---|---|---|
| 2026-10-02 06:43 (`make r1`) | `85e66ab`+ ADR 156, no guard | framed at about 06:53, 07:01:28, 07:10:13 and 07:11:06, each on an Enter after Alt had been left held (6.3 s, 3.3 s, 14.3 s) | none (no guard) | put back by hand with XTest Alt+Enter, or by the operator's next Enter. Session ended 07:12:55 by SIGTERM when the harness stopped its 30-minute background task |
| 2026-10-02 07:15 (`make r1 GAME_LAUNCH_DEPS=`, attached) | `85e66ab`+ ADR 156 and ADR 157 guard | none | 0: no modifier was left held during the run | guard loaded (`ADR 157: releasing modifier keys held on :3 for more than 1.5s`), not exercised. 7m 21s, 1 mission; operator stopped it with `z` at the lobby at 07:22:36 |

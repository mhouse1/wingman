# Anomaly 015 — A Resized Nested Window Blinds Capture

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-25 | 1.9.3           |

## Summary

**Status: cause measured 2026-10-25; fix in [ADR 162](../adr/162-the-nested-display-keeps-its-size.md), live flight outstanding.**
On 2026-10-25 at 16:21:27, in a `make r3` session (acct3) waiting in `GAME_STARTING`, every frame
grab began to fail and kept failing for about ten minutes. The nested display `:3`
([ADR 099](../adr/099-nested-display-lane-for-unattended-operation.md)) had shrunk from 1920x1200
to 1920x1197. Wingman captures a fixed 1920x1200 region, so the bottom three rows were out of
bounds and `mss` returned nothing. Setting the RandR mode back by hand restored capture at once.

[ADR 104](../adr/104-nested-display-fullscreen-on-launch.md) assumed `-geometry` fixes the
server's screen for its whole life. On Xwayland 24.1.10 started with `-decorate`, it only sets
the starting size: the server is an ordinary resizable window on the operator's desktop and
resizes its own screen to match that window. Numbers are measured from the logs and the live
server named, unless labelled otherwise.

## The sequence

From `wingman.log` (session started 15:36:15; acct3; HEAD `6107007` plus the working tree of
2026-10-25):

```
16:21:26,306 [INFO] GAME_STARTING health probe #51 (+75.6s since armed): no digits
16:21:26,986 [WARNING] Analyzer: Good Luck scan failed: AttributeError("'NoneType' object has no attribute 'shape'")
16:21:27,552 [WARNING] Frame capture failed (monitor disconnected or region out of bounds) — skipping cycle
...
16:31:19,652 [WARNING] Frame capture failed (monitor disconnected or region out of bounds) — skipping cycle
16:31:21,162 [INFO] Game state: GAME_STARTING → GAME_STARTING_STALLED
16:31:21,162 [INFO] Game state: GAME_STARTING_STALLED → GAME_LOBBY
16:31:21,163 [INFO] Game state: GAME_LOBBY → GAME_BATTLE
```

| Measured | Value |
|---|---|
| `Frame capture failed` lines | 395, 16:21:27.552 to 16:31:19.652 |
| `fire_active_weapon - pressing 'f'` lines in that window | 325, all unseen |
| `:3` screen at 16:30 (`get_geometry` on the root; `xrandr`) | 1920x1197, `current 1920 x 1197` |
| RandR modes on `:3` at 16:30 | `1920x1200` (id 47) and a `1920x1197` (id 1050) beside it |
| Game window on `:3` at 16:30 | `Wine Desktop`, 1920x1200 at (0,0), unchanged |
| Nested server | `Xwayland :3 -geometry 1920x1200 -decorate`, pid 2580999, version 24.1.10 |
| `/tmp/wingman-nested-display.log` after 15:36 | nothing; the server logs no resize |
| Restore | `DISPLAY=:3 xrandr --output XWAYLAND0 --mode 1920x1200` at about 16:31:20 |

The 1920x1197 mode is not in Xwayland's standard list: the server made it to fit its window. It
was gone again once the screen was set back to 1920x1200.

What shrank the window is not recorded. The operator was at the desk, typing in another window
(hotkey lines show keys on `:0` throughout). Losing three pixels of height suits a drag, a snap
or a title-bar double-click against the top bar, not a deliberate resize. Not confirmed.

When capture came back, wingman read the end of the stalled start as a battle and stayed in
`GAME_BATTLE` (tactic `Climb`, then `Disengage`) while the screen showed the lobby's
**Reroll mission?** dialog with **REROLL** highlighted. The game exited at 16:33:19 and wingman
followed at 16:33:22. Whether the blind `f` presses opened that dialog is not known.

## Cause

ADR 104 (Context, Decision — revised): "`-geometry` fixes the server's own internal
virtual-screen size … A wrong placement decision costs window placement, never captured
pixels." That holds for `-fullscreen`, and it held for `-decorate` until something resized the
window. With `-decorate`, Xwayland 24.1.10 is a resizable window (it loads GTK for its
decorations: `gtk_disable_setlocale() must be called before gtk_init()` in the server log), and
a resize of that window resizes the X screen. Wine's virtual desktop keeps its own 1920x1200, so
the game draws on, partly below the screen's edge, while every grab of the configured region
fails.

## Fix

[ADR 162](../adr/162-the-nested-display-keeps-its-size.md): `wingman/nested_size_guard.py` reads
the `:3` screen's size every second and, when it is smaller than `nested.size`, sets the mode back
with `xrandr` (`nested.restore_size`, on by default).

Checked on the live server, with no game on it, at 16:36:11:

```
2026-10-25 16:36:11,788 ADR 162: :3 shrank to 1920x1080 (its window was resized); set back to 1920x1200 (1 this session)
after shrink: 1920 1080
check_once=True in 16 ms; now: 1920 1200 restores: 1
```

Still to see: a resize during a flight, with the game's window keeping 1920x1200 through the
restore and the session logging at most one `Frame capture failed`.

## Immediate workaround

```bash
DISPLAY=:3 xrandr --output XWAYLAND0 --mode 1920x1200
```

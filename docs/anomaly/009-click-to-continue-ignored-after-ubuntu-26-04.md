# Anomaly 009 — "Click to Continue" Clicks Stop Registering After the Ubuntu 26.04 Upgrade

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-02 | 1.9.0           |

## Summary

**Status: open, cause not yet measured.** On the end-of-match podium screen
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

## What is not known

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

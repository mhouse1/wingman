# ADR 200 — No HUD, no start key and no pursuit

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-20 | 1.9.5           |

## Decision

1. **D1: the start keys need the HUD.** `u` (the configured mission) and `o` (ACS)
   start or resume a mission only if the HUD's altitude was read in the last
   `pursuit_mode.start_key_hud_window_s` (30 s). Otherwise the press is logged and
   ignored. If there is no telemetry to ask (no analyzer, or its lock timed out),
   the press goes through.
2. **D2: the pursuit stops with no HUD.** `pursue_and_engage` ends after
   `pursuit_mode.no_hud_stop_s` (15 s) with no HUD altitude read, counted from the
   later of the pursuit's start and the last read. It doesn't fall through to
   `eject_and_dive` and doesn't hand back to the mission, so nothing more is
   pressed. Its summary line says `end=no_hud`.

Setting either key to 0 turns that check off.

## Consequences

- A `u` or `o` typed into the party chat in the lobby or matchmaking no longer
  starts a mission. This includes chat typed during a manual takeover in the lobby.
- A `u` pressed on the respawn screen still works: the HUD is gone for 10 to 20 s
  there, which is inside the 30 s window. A `u` pressed while the HUD altitude
  can't be read for more than 30 s is refused. Press it again once the HUD shows.
- If a pursuit starts where no battle is going on, it now taps `f` for at most
  15 s, where before it never stopped.
- Not covered: the hotkeys still fire while the chat box has focus. Wingman can't
  see the chat focus. This ADR stops the effect, not the keystroke.

## Evidence

`wingman.log`, session started 08:50:47 on 2026-11-20. The screenshot
`tests/test-output/screenshot_20261120_092656.png` shows the lobby with CANCEL and
3/3 READY. The party chat shows "You: funf" and an input box reading
`Fufliffffffffffffffffffff`.

```
09:25:49,644 GAME_STARTING health probe #4 (+1.7s since armed): no digits spd=2 alt=None
09:25:49,974 Controller: 'u' key pressed - starting the configured mission (acs, state=GAME_STARTING)
09:25:49,987 Controller: mission_acs - target in view (icon) at the start, no climb: going straight to the pursuit (+0.0s)
09:25:50,200 Controller: 'n' pressed during GAME_BATTLE_EJECT — ignored (press again within 2s to force GAME_LOBBY)
09:25:50,425 Controller: maneuver key 'enter' pressed - entering GAME_BATTLE_MANUAL (manual takeover) [source=':3' (nested, game focused) mods=none]
09:26:11,035 Controller: 'u' key pressed — resuming auto mode from GAME_BATTLE_MANUAL
09:26:51,060 ICONPTS: rung=icon icon=(1137,554,-15deg,h15) n=1 ... act=bankright+up
09:26:51,060 Controller: fire_active_weapon - pressing 'f' key for 0.1 seconds
```

The icon stayed at (1137, 554) on every tick, a false hit on the hangar jet. By
09:27 the log had 211 `f` presses, and no health, ammo, speed or altitude read
off any of those frames.

**Why 15 s is safe in a real battle (measured):** across the ten logs from
2026-11-19 19:52 to 2026-11-20 08:50, in GAME_BATTLE or GAME_BATTLE_EJECT with
health still read in the last 3 s, the longest stretch with `alt=None` on the
BT line was 10 s (20:00:23 on 11-19). Every other session's longest was 6 s or
less. The longer stretches were all deaths, where `respawn_detected` already
stops the pursuit.

## Validation

- [x] `tests/test_no_hud_no_pursuit.py`: the two 09:25/09:26 presses are refused,
      `o` likewise, a HUD-up resume and a respawn-screen press still work, fail-open
      without telemetry, and the pursuit stops with no fall-through or hand-back.
- [ ] Live: a `u` typed in the party chat during matchmaking logs "no HUD altitude
      … ignored" and presses nothing.
- [ ] Live: no `end=no_hud` pursuit summary in a real battle over at least 40 rounds.

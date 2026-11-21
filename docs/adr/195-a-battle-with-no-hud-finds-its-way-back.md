# ADR 195 — A battle with no HUD finds its way back

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-19 | 1.9.5           |

Builds on [ADR 102](102-lobby-recheck-from-game-starting.md) (the PLAY recheck
in GAME_STARTING) and on FR-012's weapon-panel watch. Covers the open item in
[ADR 189](189-the-match-start-is-read-off-the-hud-not-the-good-luck-emote.md)
D6.

## Decision

Operator, 2026-11-19, asked whether three silent weapon-panel crops should mean
GAME_LOBBY, and approved the recommendation: a direct PLAY check, plus the
panel as a backstop to GAME_UNKNOWN.

- **D1. PLAY in a battle state walks back to GAME_LOBBY.** The lobby quick-scan
  now also reads the PLAY and READY crops in GAME_BATTLE, GAME_BATTLE_MANUAL
  and GAME_BATTLE_EJECT. It never clicks there. Two conditions must hold: PLAY
  or READY on `STARTING_PLAY_CONFIRM_READS` (6) reads in a row, and no
  plausible speed or altitude for `BATTLE_LOBBY_NO_TELEMETRY_S` (10 s). Then it
  fires `manual_reset`, the existing any-to-lobby trigger that ADR 169 already
  uses for the same evidence.
- **D2. The telemetry condition keeps ADR 102's rule.** A PLAY misread in a
  real battle cannot reset the FSM, because a real battle reads its speed and
  altitude every tick. "Plausible" means the ADR 189 ranges (100 to 4,000 kph,
  -500 to 15,000 m). Health does not count: the lobby read as health 3, 7
  and 97.
- **D3. Backstop: the whole HUD gone sends the FSM to GAME_UNKNOWN.** A new
  trigger, `battle_hud_lost`, runs from the battle states to GAME_UNKNOWN. It
  fires when FR-012 has warned for all three crops (WEAPON_NAME, AMMO_FLARES,
  AMMO_MISSILE) within 30 s, and no plausible speed or altitude has been read
  for 60 s. Silent crops alongside live telemetry are a moved panel (the
  FR-012 case of 2026-11-18). That logs and changes nothing. GAME_UNKNOWN then
  classifies the screen (lobby, end screen, or battle on HUD evidence), and the
  main loop cancels the mission on the way in.

## Consequences

- A false battle like 07:04 ends about 10 s in rather than never.
- The quick-scan runs one or two more crop reads a second during battle.
- The backstop needs about two minutes (FR-012's 120 s, then the 60 s of no
  telemetry). It is for no-HUD screens D1 cannot recognise: a popup, a
  disconnect, a black screen.

## Evidence

The operator's `v` capture at 07:05:30 shows the lobby with PLAY. Wingman
was in GAME_BATTLE_EJECT, and had been since the false start of ADR 189 D6:
```
07:04:12,016 Game state: GAME_STARTING → GAME_BATTLE
07:04:22,512 Game state: GAME_BATTLE → GAME_BATTLE_EJECT
07:06:12,481 Weapon panel: WEAPON_NAME has had no usable read for 120 s of battle (last read '')
07:06:12,647 Weapon panel: AMMO_FLARES has had no usable read for 120 s of battle (last read None)
07:06:23,163 Weapon panel: AMMO_MISSILE has had no usable read for 121 s of battle (last read None)
07:08:02     still GAME_BATTLE_EJECT
```
The HUD reads between 07:04:12 and 07:08: `PADLOCK: UNKNOWN | Altitude: None |
Speed: 4` twice, and `Health: 3`, `Health: 7` and `Health: 97`. All three
FR-012 warnings had coincided once in all November logs, this one. FR-012 is
new (2026-11-18), so there is no longer history.

## Validation

- [ ] Live: a `there is no battle, returning to GAME_LOBBY (ADR 195)` or
      `the battle HUD is gone` line, each followed by a correct state.
- [ ] No `ADR 195` walk-back in a battle that then reads its HUD: a false
      reset would show as GAME_LOBBY followed by a live HUD within seconds.

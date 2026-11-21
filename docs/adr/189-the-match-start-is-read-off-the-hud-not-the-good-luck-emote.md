# ADR 189 — The match start is read off the HUD, not the Good Luck emote

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-18 | 1.9.5           |

Changes how [ADR 032](032-game-battle-alive-fallback-trigger.md) and
[ADR 065](065-starting-health-probe-reachability.md) enter the battle. Both stay
`Accepted`. Their Good-Luck loop survives as `starting_signal: good_luck`.

## Decision

Operator, 2026-11-18: wingman keeps pressing `u` through matchmaking (the
`screenshot_20261118_070251` 'v' capture shows the CANCEL panel at 2m 6s). The
press sends the Good Luck emote, and wingman reads the chat reply as early
notice that the match is starting. Health was used before and was not reliable
or quick enough. The operator asked to retry with health, speed and altitude.

- **D1. `mission.starting_signal: hud` (shipped default).** The GAME_STARTING
  loop presses nothing and does not OCR the `good_luck` crop. It arms the probe
  at once, grabs a frame every `mission.starting_health_probe_interval_s`
  (0.4 s), and hands each frame to the probe. The main loop's 1.5 s tick still
  feeds the probe as well. It launches the mission when the probe confirms the
  HUD, through the existing `good_luck_detected` FSM trigger.
- **D2. The probe reads HEALTH and ALTITUDE_SPEED from the same frame.** It
  confirms the HUD on the first of:
  1. *health+telemetry*: health at or above `STARTING_MIN_CONFIRMED_HEALTH`
     (20, ADR 102 rev 2), plus a speed of 100 to 4,000 kph or an altitude of
     -500 to 15,000 m in that frame. One frame is enough.
  2. *health x2*: the ADR 063 two-read rule with the ADR 102 floor, unchanged.
  3. *telemetry x2*: speed and altitude both in range on two probes in a row,
     with no usable health (sky behind the green health digits, ADR 080).
- **D3. The confirmation flag is fresh per arm** (`starting_battle_confirmed`).
  `game_battle_alive` can carry the last round's value, which is why ADR 032
  gated its poll at 10 s. The HUD loop does not read it, so it needs no gate.
- **D4. `starting_signal: good_luck` restores the old loop exactly.** Its
  13 s wait and battle-alive bypass now also benefit from D2. Both PATH1 gates
  pin it through `config_overrides`, because their screenshots and validators
  model the Good Luck entry. `main.py` now applies a path file's overrides in
  the live-capture lane as well as in replay.

- **D5. GAME_UNKNOWN calls battle on the same HUD evidence** (added
  2026-11-18, after the first live session). `_classify_unknown_state` used to
  return GAME_BATTLE on any digit in the HEALTH crop. It now needs, in one
  frame, health at the floor with a plausible speed or altitude, or speed and
  altitude together (`battle_hud_evidence`). Its two-frame debounce is
  unchanged.

  Operator, 2026-11-18: "it stalled and started the mission too early". The two
  'v' captures (`screenshot_20261118_100542`, `_100546`) show the spawn intro,
  TEAM ALPHA and no HUD. Matchmaking ran past the 150 s timeout. Then the
  classifier read the intro's `7` twice and launched the mission 28 s or more
  before the HUD:
  ```
  10:05:05,312 GAME_STARTING health probe #201 (+149.1s since armed): raw=7 spd=None alt=None
  10:05:05,706 Controller: game_starting timed out after 150s without the HUD
  10:05:10,766 GAME_STARTING_STALLED persisted for 3s — reclassifying via GAME_UNKNOWN
  10:05:13,581 FSM: GAME_UNKNOWN classified as GAME_BATTLE via unknown_to_battle_detected
  10:05:13,581 💚 HEALTH ALIVE — restarting mission immediately
  10:05:46     'v' capture: still the intro
  ```
  This is the common case, not a one-off: of 45 UNKNOWN→BATTLE classifications
  from 11-10 to 11-18, 34 came more than 20 s before a logged health of 20 or
  more. Only 9 came within 5 s.

- **D6. A health read alone never confirms the HUD in GAME_STARTING** (added
  2026-11-19). D2 rule 2 ("health x2", the ADR 063 recurrence rule) is
  removed. Health now counts only beside a plausible speed or altitude in the
  same frame (rule 1), and telemetry alone counts on two probes in a row
  (rule 3). That is the same evidence GAME_UNKNOWN has needed since D5.

  Operator's `v` capture, 07:05:30: the lobby with PLAY, while wingman was in
  GAME_BATTLE_EJECT. The match had bounced back to the lobby, and two probes
  read `95` off the lobby screen:
  ```
  07:04:06,448 Lobby quick-scan: PLAY visible in GAME_STARTING (1/6 reads)
  07:04:08,722 Lobby quick-scan: PLAY visible in GAME_STARTING (3/6 reads)
  07:04:09,893 Lobby quick-scan: stale lobby crop no longer visible — streak reset
  07:04:11,364 GAME_STARTING health probe #69 (+39.5s since armed): raw=95 spd=None alt=None
  07:04:11,781 HUD confirmed in GAME_STARTING by health x2 (health=95 spd=None alt=None)
  07:04:12,016 Game state: GAME_STARTING → GAME_BATTLE
  07:08:02     still GAME_BATTLE_EJECT in the lobby: no battle state checks for PLAY
  ```
  Rule 2 confirmed 6 starts on 2026-11-18/19, and in 4 of them no altitude
  ever followed. Rule 1 confirmed about 130 starts. The 2 real rule-2 starts
  (04:53, health 76) read an altitude moments later, so rule 1 catches them
  one probe later.

  Once in a false battle, nothing noticed the lobby: the PLAY check ran in
  GAME_STARTING only (ADR 102), so the session waited in GAME_BATTLE_EJECT
  indefinitely. [ADR 195](195-a-battle-with-no-hud-finds-its-way-back.md)
  covers it.

## Consequences

- No more Good Luck emote to the whole match every 5 s through matchmaking,
  which is about 15 presses per entry at the November median.
- Launch moves to the first frame showing the HUD. Before, it was the second
  1.5 s tick that read health, or the 13 s timer. The timer ran out before
  health in 41% of entries. In those, the mission now starts a median 0.7 s
  later, at the HUD instead of during the spawn intro, when the jet could not
  yet be flown anyway.
- Probe log lines now carry `spd=` and `alt=`. The confirmation line names its
  evidence: `HUD confirmed in GAME_STARTING by health+telemetry`.
- If the game ever starts a match without the HUD's green digits readable for
  150 s, the existing `starting_timeout` path takes over as before.

## Evidence

1,484 match entries in `logs/wingman_202611*.log`, from GAME_STARTING to the
loop's exit:

| Exit | Entries | Timing |
|------|---------|--------|
| Good Luck, then health cut the 13 s wait short | 801 | median 10.8 s after Good Luck (3.8 to 12.9) |
| Good Luck, 13 s wait ran out | 611 | health then showed a median 0.7 s later (p90 1.4 s, max 3.9 s) |
| Health with no Good Luck | 16 | 9.5 to 85.8 s after entry |
| Timeout at 150 s | 56 | about 32 went on to battle via GAME_UNKNOWN |

So the HUD appears 11 to 14 s after Good Luck almost every time, and health
already set the launch time in practice. The Good Luck reply shows on the spawn
intro, with no HUD (`test_screenshots/integration_test/P1_020_GOOD_LUCK_VISIBLE.png`).

Speed and altitude are drawn with health. Here is the first battle tick of
2026-11-17 21:52:
```
21:52:48,630 Analyzer: health 175 confirmed in GAME_STARTING (+79.2s since armed)
21:52:50,417 Health: 175 | alive=True
21:52:50,492 PADLOCK: UNKNOWN | Altitude: 940 | Speed: 738 | Nose: n/a
```

The lobby fragment ADR 102 rev 2 guards against still shows on the first probe
of most entries (`GAME_STARTING health probe #1 (+0.3s since armed): raw=7`). It
is below the floor, and rule 1 cannot pass it.

### First live session, 2026-11-18 07:53 to 10:10 (acct, code: ADR 189 D1-D4, uncommitted on acs-improvements2 at 75482c3)

| Entries | HUD confirmed | by health+telemetry | by health x2 | Timeout at 150 s | Other exit |
|---------|---------------|---------------------|--------------|------------------|------------|
| 23 | 20 | 19 | 1 | 1 (10:05, then the D5 early launch) | 2 |

The time from loop start to HUD ran from 42.6 s to 143.1 s, and twice it was
143.1 s. The HUD comes about 12 s after the Good Luck reply did, so the 150 s
timeout now sits in the tail of entry times. With D5, a timeout is no longer
harmful: STALLED goes to GAME_UNKNOWN, which waits for the HUD.

## Validation

- [ ] Live session on `hud`: every entry logs `HUD confirmed ... by`, and no
      `pressed 'u'` appears in GAME_STARTING.
- [ ] Entry-to-battle times against the November table above: no launch
      before the HUD (no mission in the lobby), and no rise in timeouts.
- [ ] Count of each evidence kind (health+telemetry, health x2, telemetry x2).
- [ ] D5: every `classified as GAME_BATTLE via unknown_to_battle_detected` is
      followed within 5 s by `Health: N | alive=True` with N of 20 or more.
      First live case, 2026-11-18 12:17: the 150 s timeout at 12:17:03, then
      13 rejections in GAME_UNKNOWN (`health=7 spd=None alt=None is no battle
      HUD`), then battle at 12:17:54 on `health+telemetry (health=228 spd=1232
      alt=1466)`, with the aircraft level at 1,466 m. That is one case; the
      check stays open until there are more.
- [ ] `make rr-path1-gate` and `make rr-live-path1-gate` green with the
      `good_luck` pin. They could not run on 2026-11-18 while a live session
      held the instance lock.

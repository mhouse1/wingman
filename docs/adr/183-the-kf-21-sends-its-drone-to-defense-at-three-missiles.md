# ADR 183 — The KF-21 sends its drone to defense at three missiles

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Draft    | 2026-11-16 | 1.9.5           |

## Decision

Operator, 2026-11-16: "rename the wingsweep 'w' key to auxilary_feature then update the wingman
so that when flying as KF-21 it activates auxilary_feature after missile count reaches 3 or
lower; this enables the jet's drone to enter defense mode."

- **D1. `w` is the auxiliary feature.** `WINGSWEEP_KEY` is now `AUXILIARY_FEATURE_KEY`, and
  `Controller.wingsweep()` (never called) is now `activate_auxiliary_feature()`. That is a
  0.1 s tap that also goes out during a pursuit, where the mission is cancelled, the same as the
  special weapon (ADR 173). What the key does depends on the jet.
- **D2. A jet profile names it.** `jet_profile.profiles.<jet>.auxiliary_feature:
  {trigger: low_missiles, missiles_at: N}`. A jet without the section never presses `w`. The KF-21
  has `missiles_at: 3`. It spawns with 4 (the 2026-11-16 10:31 session read nothing higher), so
  the press comes after its first missile.
- **D3. Two readings, once a life.** The key goes out when two missile counts in a row (by OCR
  read sequence) read `missiles_at` or fewer, in `GAME_BATTLE` or `GAME_BATTLE_EJECT`, after
  the post-respawn grace. It goes out once a life: a respawn or a new round allows it again, but a
  resupply does not.
- **D4. Or on the first incoming missile** (operator, same day: "as soon as any incoming is
  detected if 'w' hasnt been activated, activate"). With `on_incoming: true` (set on the KF-21),
  a new incoming alert presses it, after the flare burst has started, if neither trigger has
  pressed it this life. An alert inside the post-respawn grace is ignored, as it is for the
  flares.

## Consequences

- If the feature is a toggle, a second press would call the drone back, so a resupply does not
  re-arm the press. If the drone instead leaves defense mode at a resupply, D3 leaves it out of
  defense for the rest of that life. This is not yet known (see Validation).
- The game shows no icon for the feature, so unlike `q` (ADR 173) there is no ready check. A
  press while the feature is cooling down is lost.
- Log line: `Auxiliary feature: missiles=N at or under 3, pressing it once this life`, or
  `Auxiliary feature: incoming missile, pressing it once this life`.

## Implementation

- `wingman/keybindings.py`, `wingman/controller.py`: the rename,
  `activate_auxiliary_feature`, `auxiliary_feature_profile`.
- `wingman/tick_handlers.py`: `AmmoEventsHandler.tick_auxiliary_feature`, run from `tick_events`
  and from `deploy_flares_on_new_incoming`.
- `wingman/config_schema.py`, `wingman/config.yaml`: the `auxiliary_feature` section and the
  `kf21` profile.
- `tests/test_auxiliary_feature.py`.

## Validation

- [ ] Live, KF-21: one `Auxiliary feature:` line per life that fires a missile or sees an incoming, and the drone
      enters defense mode (a `v` screenshot after the press).
- [ ] After a resupply: is the drone still in defense mode? If not, D3's once-a-life rule
      needs a superseding ADR.

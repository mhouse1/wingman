# ADR 027 — J20 Target Painting Mode

| Status   | Date       | Wingman Version |
|----------|------------|-----------------|
| Accepted | 2026-05-02 | 1.6.5           |

## Context

The J20 aircraft has a passive team-buff ability (unlockable at levle12+): **while a pilot holds active missile lock on an enemy, allied pilots gain +5 % missile lock rage and a −15 % missile lock time reduction against that target**. This buff only applies while the lock is continuously held — firing the last missile breaks lock and ends the buff until the next lock cycle.

Wingman's `search_and_destroy_loop` runs two independent daemon threads:

- `_padlock_loop` — presses the padlock-camera key every ~6 s to cycle lock onto the nearest enemy.
- `_weapon_loop` — fires `fire_active_weapon` every ~1 s continuously.

For J20 in a team support role the weapon loop is counter-productive: once `ammo_missiles` drops to 1, firing that final missile ends the padlock lock and removes the ally buff for the remainder of the engagement. There is no in-game way to suppress the weapon loop selectively for the last round.

Separately, when `GAME_BATTLE_MANUAL` is active the player has taken manual control; suppressing fire in that state is incorrect because the player may intentionally want to shoot.

## Decision

Add an opt-in `target_painting_mode` flag under a new `j20_mission:` section in `config.yaml`:

```yaml
j20_mission:
  target_painting_mode: false
```

When `target_painting_mode: true`, the weapon loop reads `_ammo_missiles` before each fire call. If `ammo_missiles == 1` **and** the current FSM state is not `GAME_BATTLE_MANUAL`, `fire_active_weapon` is skipped for that cycle. The padlock loop continues unaffected — the aircraft maintains lock and paints the target for allies indefinitely until the mission ends or manual takeover occurs.

The check is a guard inside `_weapon_loop` (or a thin wrapper on `fire_active_weapon` called from there); it does **not** stop the weapon-loop thread, which remains interruptible by `stop` and `_mission_cancel` events as normal.

### Config wire-up

`main.py` reads `cfg.get("j20_mission", {}).get("target_painting_mode", False)` and passes it as a constructor argument to `Controller`. `Controller.__init__` stores it as `self._target_painting_mode: bool`.

`_ammo_missiles` is already tracked under `self._ammo_lock` by the main OCR loop; the weapon loop acquires the lock with `acquire(timeout=0.5)` to read the value and skips the cycle on timeout (consistent with the lock-acquire-timeout pattern in `CLAUDE.md`).

## Consequences

**Positive**

- The J20 continuously paints enemy targets throughout the battle, maximising the ally buff uptime.
- No new OCR region is required — `AMMO_MISSILE` is already calibrated and read every main-loop cycle.
- The padlock loop is untouched; lock cycling behaviour is identical to standard `search_and_destroy` mode.
- Feature is fully opt-in; all other aircraft and mission configs are unaffected.

**Negative / Trade-offs**

- The final missile is never fired while target-painting mode is active. If the J20 is the last surviving aircraft and no allies can benefit from the buff, the unused missile represents wasted damage output.
- `ammo_missiles` is read via OCR and may occasionally return `None` (e.g. first frame, OCR miss). When the value is `None` the weapon loop fires normally — this is the safe fallback: it is better to fire than to silently suppress fire due to a missing read.
- The `GAME_BATTLE_MANUAL` carve-out means the player can always reclaim full weapon control by triggering manual takeover; the suppression is lifted immediately on state transition.

## Alternatives Considered

**Stop the weapon-loop thread entirely at 1 missile** — rejected because it requires signalling the thread and restarting it if ammo is replenished mid-battle; the per-cycle guard is simpler and handles replenishment for free.

**Separate padlock-only mode (no weapon thread at all)** — would also achieve target painting but loses the ability to fire earlier missiles (2+) when ammo is plentiful. The per-missile guard preserves normal fire behaviour until the last round.

**Always skip fire in J20 missions** — too aggressive; early in a mission firing missiles is still desirable to reduce enemy HP and prevent attrition to allies.

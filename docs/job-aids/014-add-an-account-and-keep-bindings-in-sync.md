# Job Aid 014 — Add an Account and Keep Bindings in Sync

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-10-11 | 1.9.0           |

## Purpose

Wingman can fly more than one MetalStorm account on the same machine, one at a
time. Each account is a separate Wine prefix. This job aid covers two tasks:

- adding another account, so that `make g2` and `make r2` (or `g3`, `r3`, …) work
- keeping every account on the same key bindings and game settings

It replaces the bootstrap steps in
[Research 005](../research/005-multi-account-run-targets.md), which tell you to
copy the main prefix. Do not copy it; see [Why not copy the main prefix](#why-not-copy-the-main-prefix).

## How it fits together

| Targets                 | Prefix under `~/Games/Heroic/Prefixes/` | Run tag |
|-------------------------|------------------------------------------|---------|
| `make g`, `r`, `rd`     | `Metalstorm` (the main prefix)           | none    |
| `make g1`, `make r1`    | `Metalstorm-acct1`                       | `acct1` |
| `make g2`, `make r2`    | `Metalstorm-acct2`                       | `acct2` |
| `make g3`, `make r3`    | `Metalstorm-acct3`                       | `acct3` |

- `gN` launches the game only. `rN` launches the game and Wingman, logging to
  `wingman.log`.
- The login lives in the prefix, so a prefix is an account. Launching another
  target stops the running game first, so switching accounts is just running
  the other target.
- **The main prefix is the source of bindings and settings.** Every launch of
  an account prefix copies them from the main prefix before the game starts.

## Add an account

The Makefile already has targets for acct1, acct2 and acct3. For a fourth
account, first do [Add targets for another account](#add-targets-for-another-account).

The steps below use acct2. Replace the number for another account.

### Step 1 — Let Proton build the prefix

```bash
make g WINE_PREFIX=$HOME/Games/Heroic/Prefixes/Metalstorm-acct2
```

`make g2` cannot do this step. It stops at `ERROR: no user.reg under …` because
the prefix does not exist yet.

Expect these two things, both normal on this first launch:

- the line `settings sync skipped: … user.reg not found`
- a game window named `Metalstorm` instead of `Wine Desktop`

When the game is up, quit it from inside the game: ESC, then **Exit to
Desktop**. Do not log in yet.

### Step 2 — Launch from the account target and log in

```bash
make g2
```

This launch sets up the 1920x1200 virtual desktop, copies the bindings and
settings from the main prefix, and starts the game. Expect these lines among
its output:

```
OK: virtual desktop 1920x1200 enabled for Metalstorm-acct2 (backup: …)
settings sync: copied 29 settings from Metalstorm into Metalstorm-acct2 (backup: …)
Found window … — Wine Desktop
```

The number of settings varies. If the second line reads `settings sync
skipped: target prefix has no Starform/Metalstorm key`, the copy happens on the
next launch instead.

Log in as the new account, then quit from inside the game again, so the game
saves the login.

### Step 3 — Check it

```bash
make g2
```

- The lobby shows the new account's pilot name, not the main account's.
- The launch prints `settings sync: Metalstorm-acct2 already matches Metalstorm`.
- In the game, the controls match
  [Job Aid 011](011-wingman-keybindings.md).

To check the settings without launching, close the game and run:

```bash
make sync-settings-2 DRY=--dry-run
```

It should report `(0 differ)`.

### Step 4 — Fly it

```bash
make r2
```

Performance and mission-stats files from this run carry the account tag:
`docs/performance/current/run_<timestamp>_acct2.json`.

## Keep bindings and settings in sync

**Change bindings and settings in the main account only.**

1. Run `make g`, change the binding or setting in the game, and quit from
   inside the game so it is saved.
2. Launch any account as usual (`make g1`, `make r2`, …). The launch copies the
   change in and prints one line:

   ```
   settings sync: copied 1 settings from Metalstorm into Metalstorm-acct1 (backup: user.reg.bak-…)
   ```

A change made inside an account does not last. The next launch of that account
replaces it with the main prefix's value.

To copy without launching, close that account's game and run
`make sync-settings-1` (or `-2`). `DRY=--dry-run` lists what would be copied and
writes nothing.

### What is copied

| Copied from the main prefix                                  | Never copied                                   |
|---------------------------------------------------------------|------------------------------------------------|
| Key, gamepad and joystick bindings, control scheme, turn mode | Login token, selected account, device id       |
| Volumes, HUD colours, hangar sort order                       | Chat history, session and install ids          |
| Resolution, window position, fullscreen mode, graphics quality | Last-match statistics and the replay match id |

The copy works from a list of allowed setting names in
`scripts/sync-metalstorm-settings.py`. A setting added by a future game version
is copied only if its name starts with one of the allowed prefixes.

### Backups

Every copy that changes something first saves the account's registry as
`user.reg.bak-<timestamp>` in that prefix. To undo a copy, close the game and
copy the backup over `user.reg`.

## What a launch does, in order

1. Checks the virtual desktop keys in the account prefix (`gN` and `rN` only).
2. Sets up the nested display. If a game is still attached to it, this closes
   the game and the display.
3. Stops any MetalStorm that is still running.
4. Waits up to 20 seconds for that prefix's wineserver to exit, then copies the
   settings from the main prefix.
5. Launches the game in the account prefix.
6. `rN` only: starts Wingman with `WINGMAN_ACCOUNT=acctN`.

Step 4 comes after step 3 for a reason. wineserver keeps the registry in memory
and writes the whole file back when it saves, so a copy made while the game is
up is lost. That is how acct2 first came up without bindings on 2026-10-11.

## Troubleshooting

| What you see | Cause | What to do |
|---|---|---|
| `ERROR: no user.reg under …` from `make gN` | The prefix has not been built | Do Step 1 |
| `settings sync skipped: … is in use` | wineserver was still running 20 seconds after the old game was stopped. The game launched without the copy | Quit the game from inside, then launch again |
| `settings sync skipped: target prefix has no Starform/Metalstorm key` | The game has never started in this prefix | Nothing. The next launch copies |
| `ERROR: … is in use — quit the game first` from `make sync-settings-N` | That account's game is running | Quit the game, then run it again |
| The same `copied N settings` line on every launch of one account | The game is rewriting those settings in that account | Run `make sync-settings-N DRY=--dry-run` after a session to see which, and report it |
| Game window is `Metalstorm`, not `Wine Desktop`, after Step 1 | It was launched with `make g WINE_PREFIX=…` instead of `make gN` | Launch with `make gN` |
| A new prefix comes up logged in as the main account | The prefix was copied from the main prefix | Delete the copy and build it with Step 1 |

## Add targets for another account

Add these to the Makefile beside the acct1, acct2 and acct3 lines. Recipe lines start
with a tab.

```make
ACCT4_PREFIX ?= $(HOME)/Games/Heroic/Prefixes/Metalstorm-acct4

sync-settings-4:
	@$(PYTHON_RUN) scripts/sync-metalstorm-settings.py \
	  "$(SETTINGS_SRC_PREFIX)" "$(ACCT4_PREFIX)" $(DRY)

g4: WINE_PREFIX := $(ACCT4_PREFIX)
g4: ensure-virtual-desktop g

r4: WINE_PREFIX := $(ACCT4_PREFIX)
r4: WINGMAN_ENV += WINGMAN_ACCOUNT=acct4
r4: ensure-virtual-desktop rd
```

Also add `g4 r4 sync-settings-4` to the `.PHONY` line that lists `g1 g2 g3 r1 r2 r3`.
Check the result with `make -n r4`: the `WINEPREFIX=` in the launch line must
name the acct4 prefix, and the last line must carry `WINGMAN_ACCOUNT=acct4`.

## Why not copy the main prefix

Research 005 says a `cp -a` copy of the main prefix comes up logged out with a
new device id. The prefixes on disk show otherwise (checked 2026-10-11):

- `Metalstorm-acct1` was built fresh by Proton on 2026-08-21. The `cp -a` ran
  76 seconds later and landed inside it as the unused directory
  `Metalstorm-acct1/Metalstorm`.
- That stray copy holds the same account id, device id and Windows machine id
  as the main prefix. A real copy carries the main account's identity.

## Limits and open points

- **One account at a time.** Input and capture are shared by the whole
  display, so two games cannot run together.
- **`wingman/config.local.yaml` is per machine, not per account.** A setting
  such as `accept_invite` applies to every account flown here.
- **Tuning was done on other accounts.** A new account may fly a different jet,
  and the values in `wingman/config.yaml` may not suit it.
- **The performance regression check does not filter by account.** Runs are
  tagged, but the comparison mixes them
  ([Research 005](../research/005-multi-account-run-targets.md), question 3).
- **acct1 and acct2 hold the same device id.** Two freshly built prefixes
  produced the same `generatedDeviceIdentifier`; the main prefix holds a
  different one. The game's backend therefore sees those two accounts on one
  device id. Whether that matters is the operator's call
  ([Research 005](../research/005-multi-account-run-targets.md), question 4).

## What has been checked

| Item | State on 2026-10-11 |
|---|---|
| Step 1 builds a prefix that logs in as a separate account | Done live for acct2 |
| `make r2` reaches the lobby and starts matchmaking | Done live |
| `make sync-settings-2` leaves acct2 matching the main prefix | Done, 50 of 50 settings identical |
| The copy inside a launch | Unit tests pass. Not yet watched on a live launch |
| How long wineserver takes to exit after the game is stopped | Not measured. The copy waits 20 seconds, then skips |

## References

- [Research 005](../research/005-multi-account-run-targets.md) — per-account
  run targets and account tagging
- [ADR 052](../adr/052-metalstorm-keybinding-persistence.md) — why settings are
  filtered, not copied whole
- [Job Aid 010](010-run-metalstorm-on-linux.md) — installing the game and the
  main prefix
- [Job Aid 011](011-wingman-keybindings.md) — the bindings Wingman needs
- `Makefile`, "Per-account run targets" — the targets and `sync-account-settings`
- `scripts/sync-metalstorm-settings.py`, `scripts/ensure-virtual-desktop.py`,
  `wingman/wine_prefix.py`

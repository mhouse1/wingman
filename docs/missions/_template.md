# Mission — <JET> (mission_<name>)

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | YYYY-MM-DD | x.y.z           |

<!--
Copy this file to docs/missions/<name>.md and fill it in. Delete these comments
if you like. Read docs/missions/README.md first: it says what every mission
already inherits, which building blocks exist, and the rules the code follows.
docs/missions/j20.md is this form filled in.
Set the version from WINGMAN_VERSION in wingman/main.py, never a guess.
-->

Implement per `docs/missions/README.md`.

## Parameters

<!--
Every number the mission uses, once. Config key: the key under which it will
live in wingman/config.yaml, or "hard-coded" if it deliberately has none.
-->

| Parameter | Value | Unit | Config key |
|-----------|-------|------|------------|
|           |       |      |            |

## Decisions

<!-- One value per row. Allowed values are in the third column. -->

| Decision | Value | Allowed |
|----------|-------|---------|
| Jet |  | name; `Compatible Jets:` line in the docstring |
| Hotkey |  | a key the game does not bind (check in-game) |
| Doctrine |  | adaptive (tree owns tactics) or scripted (thread runs the steps) |
| Engagement mode |  | `search_and_destroy`, `boresight_engage` or none |
| Loadout |  | primary, or switch to the secondary at step N (once per life) |
| Hotkey while a mission runs |  | skip, or preempt (cancel the running mission and take over) |
| Selectable as `default_mission` |  | yes or no |
| Ends by |  | cancelled, or hand-off at step N |

## Steps

<!--
In order, as the mission thread runs them. Action is a building block from
README section 4, or "new: <what it does>". On timeout or failure says what the
mission does when the step cannot finish, not just "fails". Log evidence is the
text you will look for in wingman.log to know the step ran.
-->

| # | Trigger | Action | Parameters | On timeout or failure | Log evidence |
|---|---------|--------|------------|-----------------------|--------------|
| 1 |         |        |            |                       |              |

## Inherited behavior

<!--
Default, and usually all you need: every shared behavior in README section 3
applies unchanged. Keep this line unless something below is different.
-->

All shared behavior in `docs/missions/README.md` section 3 applies unchanged.

## Shared changes

<!--
Anything that changes tree, watchdog or config behavior for EVERY mission,
including J20. There is no per-mission switch, so a request to disable or
retune a shared behavior for this mission only goes here. It needs its own ADR.
-->

None.

## Acceptance

<!--
What you will read in wingman.log to accept the mission on a live trial, in
order. Each line must be emitted by the code and covered by a test.
-->

* 

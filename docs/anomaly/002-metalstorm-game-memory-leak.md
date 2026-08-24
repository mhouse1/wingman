# Anomaly 002 — MetalStorm Game-Side Memory Leak

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-08-24 | 1.8.5           |

## Summary

The MetalStorm process grows at a steady **~165 MB/h**, linearly, for as long as
it runs. It is **not wingman's memory** and nothing in this repository can fix
it. It resets completely on relaunch.

It was invisible for weeks because wingman's own leak (ADR 091, ~950 MB/h) was
six times larger and masked it. With that fixed, the game is now the **only**
growing process on the machine and therefore the binding constraint on how long
an unattended session can run.

This record exists so the leak can be re-measured cheaply after a game update,
rather than rediscovered.

## Fingerprint of the measured build

Record these before any future comparison — the whole point is knowing whether
the thing being measured changed.

| | value |
|---|---|
| Product | Starform / Metalstorm |
| `cold_start_last_seen_app_version` | `1.0` (not granular — do not rely on it alone) |
| Unity runtime | **6000.3.14f1** |
| `Metalstorm.exe` build date | **2026-08-13** |
| `Metalstorm.exe` size | 667,648 bytes |
| Proton | GE-Proton10-34 |
| Launcher | umu-launcher 1.4.0, runtime `sniper` |
| Host | VEDA, Ubuntu, X11 via XWayland |

Re-read the app version with:

```bash
grep -A2 cold_start_last_seen_app_version \
  ~/Games/Heroic/Prefixes/Metalstorm-acct1/user.reg
strings ~/Games/Heroic/Metalstorm/Metalstorm_Data/globalgamemanagers | head -3
ls -la --time-style=+%Y-%m-%d ~/Games/Heroic/Metalstorm/Metalstorm.exe
```

## Evidence

21 sessions carry `game_rss_mb` in their `RESOURCE` lines. Restricting to
sessions of **2h or more** — short windows over-read the early ramp badly, up to
+513 MB/h over 20 minutes — and excluding the run that was half idle because of
Anomaly 001:

| session (log end time) | duration | game start | game end | rate | R² |
|------------------------|----------|-----------|----------|------|-----|
| `wingman_20260821_165401` | 5.52 h | 1152 | 2409 | +156 | 0.977 |
| `wingman_20260821_234448` | 2.09 h | 1133 | 1933 | +176 | 0.980 |
| `wingman_20260822_053710` | 3.09 h | 1114 | 2035 | +159 | 0.946 |
| `wingman_20260822_133450` | 3.59 h | 1121 | 2143 | +166 | 0.960 |
| `wingman_20260823_002829` | 6.77 h | 1130 | 2571 | +153 | 0.977 |
| `wingman_20260823_065033` | 2.26 h | 1138 | 2003 | +196 | 0.983 |
| `wingman_20260823_230230` | 3.01 h | 1106 | 2120 | +193 | 0.985 |
| `wingman_20260824_091431` | 4.34 h | 1120 | 2203 | +165 | 0.986 |
| `wingman_20260824_180336` * | 7.68 h | 1124 | 2668 | +143 | 0.991 |

\* At the time of writing this session was still `wingman.log`; it rotates to
the name above on the next `make rd` (rotated logs are named by end time).

**Baseline: median +165 MB/h, mean 167, range 143–196, stdev 17 (n=9).**

Three properties worth keeping:

- **Highly linear.** R² between 0.946 and 0.991. This is steady accumulation,
  not a step or a spike.
- **Fully resets on relaunch.** Start RSS across all 21 sessions spans only
  1106–1197 MB. Nothing carries over between runs, so restarting the game is a
  complete mitigation.
- **Mildly sub-linear, with no plateau by 8 hours.** Segment rates within the
  7.68h session:

  | segment | rate |
  |---------|------|
  | 0.2–2 h | +207 MB/h |
  | 2–4 h | +138 MB/h |
  | 4–6 h | +145 MB/h |
  | 6–8 h | +127 MB/h |

  The rate decays but stays firmly positive. Whether it asymptotes beyond 8
  hours is **unmeasured** — no session has run longer.

## It is not wingman's memory

Established by the ADR 091 work rather than assumed. In the 7.68h session:

```
wingman  rss 683 -> 2736 MB   live allocation +3 MB/h   (flat)
game     rss 1124 -> 2668 MB  +143 MB/h                 (climbing)
```

`resource_monitor` reaches the same conclusion unaided and prints it:

> `VERDICT: GAME-SIDE growth (+143 MB/h) while wingman stayed flat — wingman is
> a victim, not the cause`

The two are independently sampled from `/proc`, so this is not one measurement
being split two ways.

## Impact

At +165 MB/h from a ~1,120 MB baseline:

| session length | projected game RSS |
|----------------|--------------------|
| 4 h | ~1.8 GB |
| 8 h | ~2.4 GB |
| 12 h | ~3.1 GB |
| 24 h | ~5.1 GB |

Observed: 2,668 MB at 7.9 h, with system swap first appearing around minute 226
and reaching 78 MB by the end — the first non-zero swap in any post-ADR-091
session. Trivial in absolute terms, but it marks where the machine starts to
feel it.

For context, the pre-ADR-091 sessions show swap of 2.5–10.4 GB, but that was
wingman's leak, not this one. **The game alone has not yet caused meaningful
memory pressure at 8 hours.**

## How to re-test after a game update

Cheap, roughly 3 hours of unattended time:

1. **Record the new fingerprint** (commands above) and confirm it actually
   changed. A rebuild with the same date proves nothing.
2. Confirm `heap_census.enabled: false` — tracemalloc inflates timings and is
   irrelevant here.
3. Run `make r1` for **at least 3 hours**. Shorter runs over-read the rate
   badly; anything under 2 hours is not comparable to the table above.
4. Extract the rate:

   ```bash
   grep "RESOURCE elapsed" wingman.log \
     | grep -oP 'elapsed=\K\d+|game_rss_mb=\K\d+' | paste - -
   ```

   Fit a slope over samples with `elapsed >= 600` (excluding warm-up), or read
   the `game` line from `RESOURCE SUMMARY` directly.
5. Compare against the baseline:

   | measured | reading |
   |----------|---------|
   | under 80 MB/h | **materially improved** — update this record |
   | 80–140 MB/h | improved, but confirm with a second session |
   | 140–200 MB/h | **unchanged** — still leaking at baseline |
   | over 200 MB/h | worse, or the session was too short |

6. Check the session is not half idle (Anomaly 001) before trusting the number:
   consecutive `n_ocr=0` samples invalidate the measurement.

## Further investigation, if it becomes worth it

Ordered by cost. None of these have been attempted.

1. **Is it the game or is it Proton?** The measurement is the RSS of
   `Metalstorm.exe` running under GE-Proton10-34, so "the game leaks" is really
   "the game-under-Proton process leaks." A leak in Wine, DXVK or the Vulkan
   layer would look identical from outside. **Running the same session under a
   different GE-Proton build would discriminate cheaply**, and is the single
   highest-value next step — it could move this from unfixable to configurable.
2. **Time-driven or match-driven?** Correlate the rate against missions/hour and
   respawns/hour across the sessions above. If it tracks match count rather than
   wall-clock, a per-match resource is leaking and the behaviour is far more
   diagnosable. The data to test this already exists in the run stats JSONs.
3. **Lobby versus battle.** A session parked in the lobby would separate
   rendering and match state from idle UI. Cheap, but consumes a session.
4. **Unity managed heap versus native.** Would need the game's own profiler or
   a Unity development build; not available for a shipped client. Probably out
   of reach.

## Mitigations available now

- **Restart the game between long sessions.** The reset is complete, so this
  fully bounds it. `make launch-game` already kills any running instance first.
- **Cap unattended runs at roughly 8 hours**, where the game reaches ~2.4 GB.
- ADR 090's memory guard watches wingman's RSS, **not the game's**. It would not
  fire on this leak. If game-side growth ever needs bounding automatically, that
  guard would have to be extended — it currently cannot see this.

## References

- ADR 091 — wingman's own leak, whose removal made this one visible
- Anomaly 001 — the livelock that invalidates a session's rate measurement
- Performance 008 — the investigation that produced the `game_rss_mb`
  instrumentation
- ADR 090 — the memory guard, which does not cover this

#!/usr/bin/env python3
"""One-page report on a wingman session log (`make session-report`, alias `make sr`).

Why it exists: judging a session used to mean grepping the log by hand or asking for
ad-hoc analysis scripts, and every comparison rebuilt the same counts slightly
differently. This reads the log only (no game, no display, nothing imported from
wingman), and prints the numbers the target-tracking work is judged on.

What it needs from the log:
- `PURSUIT SUMMARY:` and `DIVE SUMMARY:` lines (INFO, since Cycle 7);
- `TRACKPICK:` lines (DEBUG, `make rd` / `make r1` write them) for the acquisition
  and steering-point sections; without them those sections say so.

Usage: python3 scripts/session-report.py [wingman.log] [more logs...]
"""

from __future__ import annotations

import collections
import datetime as dt
import re
import sys
from pathlib import Path

# The acquisition box before 2026-09-24 (action item 001, Cycle 12), in pixels of the
# 1920 x 1200 frame. "Outside the old box" is what the widened region can find that the
# old one could not.
OLD_BOX = (384, 216, 1536, 816)
# Fixed-position HUD zones masked out of the red mask (tracking.red_mass_exclude_zones_pct):
# scoreboard and rosters, minimap, weapons panel, squad logo. A lock inside one is a bug.
HUD_ZONES = ((0, 0, 1920, 110), (1590, 0, 1920, 330), (1440, 1060, 1920, 1200), (0, 1080, 330, 1200))
# tracking.red_mass_aim_offset_px: since 2026-09-24 the steering point (TRACKPICK's sel) is this far
# BELOW the nameplate, and the HUD zones are masked on the nameplate, so a zone is tested against
# sel minus this. Without it a label just above the weapons panel put its steering point in the
# zone and read as a HUD lock (2 false alarms in the 2026-09-25 00:03 session).
AIM_OFFSET_PX = 100
# Frame centre and the roll and pitch deadbands in pixels (tracking.deadband and pitch_deadband,
# both 0.05 of the half-frame). A lock tick whose steering point is inside them is a target the
# controller already treats as centred. TRACKPICK's sel is in absolute 1920 x 1200 coordinates.
FRAME_CENTRE = (960, 600)
DEADBAND_PX = (48, 30)
# Pooled locked-scan share before the widened region (four sessions, 08:42 to 12:46 on 2026-09-24).
BASELINE_SHARE = {"PURSUIT": 5.8, "DIVE": 11.5}

_TS = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d{3} ")
_SUMMARY = re.compile(
    r"(PURSUIT|DIVE) SUMMARY: end=(\S+) dur=([\d.]+)s scans=(\d+) locked=(\d+) "
    r"\((\d+)%\) first_lock=(\S+) ammo=(\S+) switched=(\w+)")
_PICK = re.compile(
    r"TRACKPICK: path=(\w+) sel=(?:\((\d+),(\d+)\)|-) .*?gate=(\S+) glyphs=(\S+) rm_px=(\d+)"
    r"(?: edges=\S+)?(?: blob=\S+)?(?: clu=(\S+))?")
_DIED = re.compile(r"DIED ARMED — (\d+) missile\(s\), cause=(\w+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# ADR 147: the altitude a chase settles at, and whether mission_su30's -10 degree step ever
# holds. The PADLOCK telemetry line lands about every 1.5 s at INFO and carries the altitude.
_ALT = re.compile(r"Altitude: (\d+) \| Speed: \d+ \| Nose:")
_FLOOR = re.compile(r"ALTITUDE FLOOR — \d+m below (\d+)m")


def _when(stamp: str) -> dt.datetime:
    return dt.datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")


def _inside(box, x, y) -> bool:
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def _fired(ammo: str) -> bool:
    """True when the raw ammo reading fell during the engagement (a rack switch
    inside the engagement can also change it; the summary line says `switched=`)."""
    first, _, last = ammo.partition("->")
    return first.isdigit() and last.isdigit() and int(first) > int(last)


def analyse(lines) -> dict:
    """Parse log lines into the numbers the report prints."""
    r: dict = {"first_ts": None, "last_ts": None, "lines": 0, "errors": collections.Counter(),
               "classification_timeouts": 0, "warnings": 0, "engagements": [], "ticks": 0,
               "locks": 0, "lock_outside_old": 0, "lock_in_hud_zone": 0,
               "clu": collections.Counter(), "acquisitions": 0, "acq_outside_old": 0,
               "acq_sides": collections.Counter(), "gate_reject_some_glyphs": 0,
               "weapon_presses": 0, "empty_switches": 0, "pursuit_caps": 0, "respawns": 0,
               "died_armed": collections.Counter(), "close_recovery_clicks": 0,
               "roi_follows": 0, "session_summary": [], "floor_events": collections.Counter(),
               "su30_handoffs": 0, "su30_nose_unconfirmed": 0, "pursuit_alts": [],
               "lock_runs": [], "lock_dx": [], "lock_dy": []}
    run = 0               # consecutive lock ticks so far
    alts: list = []       # (time, altitude) for every telemetry line
    windows: list = []    # (start, end) of every pursuit, from its summary line
    recent: collections.deque = collections.deque(maxlen=3)   # last three tick kinds, for acquisitions
    in_summary = 0
    for raw in lines:
        line = _ANSI.sub("", raw.rstrip("\n"))
        r["lines"] += 1
        m = _TS.match(line)
        stamp = m.group(1) if m else None
        if m:
            r["first_ts"] = r["first_ts"] or stamp
            r["last_ts"] = stamp
        if "[ERROR]" in line:
            if "startup classification timeout" in line:
                r["classification_timeouts"] += 1
            else:
                r["errors"][line.split("[ERROR]", 1)[1].strip()[:90]] += 1
        elif "[WARNING]" in line:
            r["warnings"] += 1
        m = _SUMMARY.search(line)
        if m:
            kind, end, dur, scans, locked, _pct, first_lock, ammo, switched = m.groups()
            r["engagements"].append(dict(kind=kind, end=end, dur=float(dur), scans=int(scans),
                                         locked=int(locked), first_lock=first_lock, ammo=ammo,
                                         switched=switched == "yes"))
            if kind == "PURSUIT" and stamp:
                end_at = _when(stamp)
                windows.append((end_at - dt.timedelta(seconds=float(dur)), end_at))
        m = _PICK.search(line)
        if m:
            path, sx, sy, gate, glyphs, _px, clu = m.groups()
            r["ticks"] += 1
            # path=keep (2026-09-24) is a lock held on the looser keep test: a
            # lock tick, but never an acquisition, since it needs a lock to keep.
            if path in ("redmass", "keep") and sx is not None:
                x, y = int(sx), int(sy)
                r["locks"] += 1
                run += 1
                r["lock_dx"].append(abs(x - FRAME_CENTRE[0]))
                r["lock_dy"].append(abs(y - FRAME_CENTRE[1]))
                if not _inside(OLD_BOX, x, y):
                    r["lock_outside_old"] += 1
                if any(_inside(z, x, y - AIM_OFFSET_PX) for z in HUD_ZONES):
                    r["lock_in_hud_zone"] += 1
                if clu is not None:
                    r["clu"][clu] += 1
                if (path == "redmass" and len(recent) == 3
                        and all(k not in ("redmass", "keep") for k in recent)):
                    r["acquisitions"] += 1
                    if not _inside(OLD_BOX, x, y):
                        r["acq_outside_old"] += 1
                        r["acq_sides"][("below" if y > OLD_BOX[3] else "above" if y < OLD_BOX[1] else "")
                                       + ("left" if x < OLD_BOX[0] else "right" if x > OLD_BOX[2] else "")] += 1
            elif gate == "reject" and glyphs.isdigit() and int(glyphs) > 0:
                r["gate_reject_some_glyphs"] += 1
            if not (path in ("redmass", "keep") and sx is not None) and run:
                r["lock_runs"].append(run)
                run = 0
            recent.append(path)
        m = _ALT.search(line)
        if m and stamp:
            alts.append((_when(stamp), int(m.group(1))))
        m = _FLOOR.search(line)
        if m:
            r["floor_events"][int(m.group(1))] += 1
        if "mission_su30 - step 4/4: activating pursuit mode" in line:
            r["su30_handoffs"] += 1
        if "mission_su30 - nose angle" in line and "not confirmed within" in line:
            r["su30_nose_unconfirmed"] += 1
        if "switch_weapon - pressing" in line:
            r["weapon_presses"] += 1
        if "selected weapon empty" in line:
            r["empty_switches"] += 1
        if "PURSUIT CAP" in line:
            r["pursuit_caps"] += 1
        if "RESPAWN DETECTED" in line:
            r["respawns"] += 1
        m = _DIED.search(line)
        if m:
            r["died_armed"][m.group(2)] += 1
        if "GenericCloseRecovery: GAME_UNKNOWN" in line and "clicking" in line:
            r["close_recovery_clicks"] += 1
        if "[DEBUG] ROIFOLLOW:" in line:
            r["roi_follows"] += 1
        if "Wingman Session Summary" in line:
            in_summary = 12
        elif in_summary:
            in_summary -= 1
            if line.strip() and not set(line.strip()) <= {"━"}:
                r["session_summary"].append(line.strip())
    if run:
        r["lock_runs"].append(run)
    r["lock_runs"].sort()
    r["lock_dx"].sort()
    r["lock_dy"].sort()
    r["pursuit_alts"] = sorted(a for t, a in alts if any(lo <= t <= hi for lo, hi in windows))
    return r


def _pct(n, d) -> str:
    return f"{100.0 * n / d:.0f}%" if d else "n/a"


def render(r: dict, name: str = "wingman.log") -> str:
    out = [f"SESSION REPORT  {name}"]
    out.append(f"  {r['first_ts'] or '?'}  to  {r['last_ts'] or '?'}   ({r['lines']:,} lines)")
    errs = sum(r["errors"].values())
    out.append(f"  errors: {errs}" + (f"   (+{r['classification_timeouts']} GAME_UNKNOWN classification timeouts)"
                                      if r["classification_timeouts"] else "")
               + f"   warnings: {r['warnings']}")
    for msg, n in r["errors"].most_common(5):
        out.append(f"      {n:4d} x {msg}")
    out.append("")
    out.append("ENGAGEMENTS (PURSUIT SUMMARY / DIVE SUMMARY lines)")
    if not r["engagements"]:
        out.append("  none in this log")
    for kind in ("PURSUIT", "DIVE"):
        e = [x for x in r["engagements"] if x["kind"] == kind]
        if not e:
            continue
        scans = sum(x["scans"] for x in e)
        locked = sum(x["locked"] for x in e)
        ends = collections.Counter(x["end"].split(":")[0] for x in e)
        durs = sorted(x["dur"] for x in e)
        out.append(f"  {kind.lower():8s} {len(e):3d} engagements   any lock {sum(1 for x in e if x['locked'])}"
                   f" ({_pct(sum(1 for x in e if x['locked']), len(e))})   locked scans {locked}/{scans}"
                   f" ({_pct(locked, scans)}; pooled before the widened region {BASELINE_SHARE[kind]}%)"
                   f"   fired {sum(1 for x in e if _fired(x['ammo']))}"
                   f"   length median {durs[len(durs) // 2]:.0f}s, longest {durs[-1]:.0f}s   ends {dict(ends)}")
    out.append("")
    out.append("TRACKING (TRACKPICK lines)")
    if not r["ticks"]:
        out.append("  no TRACKPICK lines: this needs a DEBUG log (make rd / make r1)")
    else:
        out.append(f"  ticks {r['ticks']:,}   lock ticks {r['locks']} ({_pct(r['locks'], r['ticks'])})")
        out.append(f"  acquisitions {r['acquisitions']}   outside the old box {r['acq_outside_old']}"
                   f" ({_pct(r['acq_outside_old'], r['acquisitions'])})   sides {dict(r['acq_sides'])}")
        runs = r["lock_runs"]
        if runs:
            dx, dy = r["lock_dx"], r["lock_dy"]
            out.append(f"  lock runs {len(runs)} (median {runs[len(runs) // 2]} ticks, longest {runs[-1]},"
                       f" {sum(1 for n in runs if n >= 4)} of 4 or more)   steering point off centre:"
                       f" |dx| median {dx[len(dx) // 2]} px ({_pct(sum(1 for v in dx if v <= DEADBAND_PX[0]), len(dx))}"
                       f" inside the roll deadband), |dy| median {dy[len(dy) // 2]} px"
                       f" ({_pct(sum(1 for v in dy if v <= DEADBAND_PX[1]), len(dy))} inside the pitch deadband)")
        out.append(f"  lock ticks outside the old box {r['lock_outside_old']} ({_pct(r['lock_outside_old'], r['locks'])})"
                   f"   in an excluded HUD zone {r['lock_in_hud_zone']} (must be 0)")
        out.append(f"  nameplate clusters on lock ticks (clu=): {dict(sorted(r['clu'].items())) or 'field absent'}")
        out.append(f"  gate-rejected ticks with some glyphs {r['gate_reject_some_glyphs']}   ROI follows {r['roi_follows']}")
    out.append("")
    out.append("ALTITUDE (ADR 147: mission_su30 levels off and chases at its own floor, 3000 m)")
    alts = r["pursuit_alts"]
    if alts:
        out.append(f"  pursuit altitude median {alts[len(alts) // 2]} m   10th to 90th percentile "
                   f"{alts[int(0.1 * (len(alts) - 1))]} to {alts[int(0.9 * (len(alts) - 1))]} m   "
                   f"({len(alts)} readings inside pursuit windows)")
    else:
        out.append("  no altitude readings inside a pursuit window")
    out.append(f"  ALTITUDE FLOOR events by floor {dict(sorted(r['floor_events'].items())) or 'none'}"
               f"   su30 hand-offs {r['su30_handoffs']}, nose angle not confirmed {r['su30_nose_unconfirmed']}")
    out.append("")
    out.append("WEAPON AND LIFE")
    out.append(f"  pursuit caps {r['pursuit_caps']}   switch_weapon presses {r['weapon_presses']}"
               f"   selected-weapon-empty switches {r['empty_switches']}")
    out.append(f"  respawns {r['respawns']}   DIED ARMED {sum(r['died_armed'].values())} {dict(r['died_armed']) or ''}"
               f"   generic close clicks {r['close_recovery_clicks']}")
    if r["session_summary"]:
        out.append("")
        out.append("SESSION SUMMARY (from the log)")
        out.extend("  " + s for s in r["session_summary"][:12])
    return "\n".join(out)


def main(argv: list[str]) -> int:
    paths = [Path(a) for a in argv[1:]] or [Path("wingman.log")]
    status = 0
    for p in paths:
        if not p.exists():
            print(f"session-report: {p} not found", file=sys.stderr)
            status = 1
            continue
        with p.open(encoding="utf-8", errors="replace") as fh:
            print(render(analyse(fh), str(p)))
        print()
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))

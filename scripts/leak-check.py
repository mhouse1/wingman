#!/usr/bin/env python3
"""ADR 092 Design 2 — leak gate over archived session logs.

Performance 008's leak ran for two months at up to 1,666 MB/h because nothing
watched for it. ADR 091 fixed that leak; this exists so the next one — from any
cause, not just the one we now understand — fails a gate instead of hiding.

Reads the `RESOURCE` lines wingman already emits, measures post-warm-up growth
per session, and reports a verdict against the thresholds in `config.yaml`.

**Three outcomes, never two.**

    PASS         exit 0   a qualifying session grew under the threshold
    FAIL         exit 1   a qualifying session grew over it
    INSUFFICIENT exit 2   no qualifying session — NOT a pass

The third matters more than it looks. Short sessions underread this defect
roughly tenfold: 0.75h runs measured +120 and +288 MB/h while the same code
leaked over 1,300 MB/h in longer ones. A green light derived from a twenty
minute session retires the question while the defect is live, which is worse
than having no gate. So a run that cannot support a conclusion says so.

**Signal preference.** `mi_use` (live allocation, from mallinfo2) when the log
carries it; `rss` otherwise, with wider thresholds and the verdict marked
lower-confidence. RSS includes arena retention that is not a leak — a post-fix
session showed RSS at +109 MB/h while live allocation was flat, and gating on
RSS alone would have failed a clean build.

**The driven game is reported, never gated.** MetalStorm leaks ~165 MB/h on its
own (Anomaly 002). That is not wingman's memory and must not fail this.

Usage:
    leak-check.py [--log-dir DIR] [--config PATH] [--all] [--json]
"""

import argparse
import glob
import json
import os
import re
import statistics
import sys

_RESOURCE = re.compile(r"RESOURCE (elapsed=.*)$")
_FIELD = re.compile(r"(\w+)=([\d.]+|n/a)")

PASS, FAIL, INSUFFICIENT = 0, 1, 2
_NAMES = {PASS: "PASS", FAIL: "FAIL", INSUFFICIENT: "INSUFFICIENT DATA"}

_DEFAULTS = {
    "log_dir": "logs",
    "warmup_s": 600.0,
    "min_window_h": 1.0,
    "min_samples": 5,
    # Live allocation — the trustworthy signal.
    "mi_use_pass_mb_h": 100.0,
    "mi_use_fail_mb_h": 400.0,
    # RSS fallback for logs predating the mallinfo2 instrumentation. Wider,
    # because arena retention inflates it without being a leak.
    "rss_pass_mb_h": 200.0,
    "rss_fail_mb_h": 600.0,
    "history_sessions": 10,
}


def load_config(path):
    cfg = dict(_DEFAULTS)
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as fh:
            loaded = (yaml.safe_load(fh) or {}).get("leak_gate") or {}
        cfg.update({k: v for k, v in loaded.items() if k in _DEFAULTS})
    except Exception as e:                      # a missing config must not crash the gate
        print(f"note: using built-in thresholds ({type(e).__name__}: {e})")
    return cfg


def parse_session(path):
    """Return the RESOURCE samples in one log, oldest first. Never raises."""
    samples = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _RESOURCE.search(line)
                if not m:
                    continue
                d = dict(_FIELD.findall(m.group(1)))
                if "elapsed" not in d:
                    continue
                try:
                    d["elapsed"] = int(float(d["elapsed"]))
                except ValueError:
                    continue
                samples.append(d)
    except OSError:
        return []
    return samples


def _num(d, key):
    v = d.get(key)
    if v in (None, "n/a"):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def rate_of(samples, key):
    """Post-anchor MB/h for `key`, or None if the field is absent."""
    a, b = samples[0], samples[-1]
    va, vb = _num(a, key), _num(b, key)
    if va is None or vb is None:
        return None
    hours = (b["elapsed"] - a["elapsed"]) / 3600.0
    if hours <= 0:
        return None
    return (vb - va) / hours


def measure(path, cfg):
    """Measure one session. Returns a dict; `qualifies` says whether to trust it."""
    samples = parse_session(path)
    post = [s for s in samples if s["elapsed"] >= cfg["warmup_s"]]
    out = {
        "log": os.path.basename(path),
        "samples": len(samples),
        "post_samples": len(post),
        "window_h": 0.0,
        "qualifies": False,
        "reason": "",
    }
    if len(post) < cfg["min_samples"]:
        out["reason"] = f"only {len(post)} post-warm-up samples (need {cfg['min_samples']})"
        return out
    out["window_h"] = (post[-1]["elapsed"] - post[0]["elapsed"]) / 3600.0
    if out["window_h"] < cfg["min_window_h"]:
        out["reason"] = (f"post-warm-up window {out['window_h']:.2f}h "
                         f"(need {cfg['min_window_h']}h) — short sessions underread")
        return out

    out["mi_use"] = rate_of(post, "mi_use_mb")
    out["rss"] = rate_of(post, "rss_mb")
    out["game"] = rate_of(post, "game_rss_mb")
    # A session that went inert cannot be trusted: no work means no leak, and
    # the rate is diluted by the idle stretch (Anomaly 001).
    idle = sum(1 for s in post if _num(s, "n_ocr") == 0)
    out["idle_samples"] = idle
    if idle > len(post) / 2:
        out["reason"] = (f"{idle}/{len(post)} samples had no OCR activity — "
                         "session was largely inert (Anomaly 001)")
        return out

    if out["mi_use"] is not None:
        out["signal"], out["rate"], out["confidence"] = "mi_use", out["mi_use"], "high"
        out["pass_at"], out["fail_at"] = cfg["mi_use_pass_mb_h"], cfg["mi_use_fail_mb_h"]
    elif out["rss"] is not None:
        out["signal"], out["rate"], out["confidence"] = "rss", out["rss"], "low"
        out["pass_at"], out["fail_at"] = cfg["rss_pass_mb_h"], cfg["rss_fail_mb_h"]
    else:
        out["reason"] = "no usable memory field in the RESOURCE lines"
        return out

    out["qualifies"] = True
    # Two rate outcomes, not three: the third outcome (INSUFFICIENT) is about
    # whether the DATA supports a conclusion, never about where the rate sits.
    # `fail_at` only labels severity, so a borderline result reads differently
    # from a runaway one without inventing a rate verdict that means neither.
    out["verdict"] = PASS if out["rate"] < out["pass_at"] else FAIL
    out["severity"] = ("clear" if out["rate"] >= out["fail_at"]
                       else "borderline" if out["verdict"] == FAIL else "")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="ADR 092 leak gate")
    ap.add_argument("--log-dir", default=None)
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--all", action="store_true", help="report every session, not just the latest")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    cfg = load_config(args.config)
    log_dir = args.log_dir or cfg["log_dir"]

    paths = sorted(glob.glob(os.path.join(log_dir, "wingman_*.log")))
    # The current session lives at the repo root rather than in logs/ until it
    # rotates. Only fold it in for the default directory, so an explicit
    # --log-dir selects exactly what it names (which is what tests rely on).
    if args.log_dir is None and os.path.exists("wingman.log"):
        paths.append("wingman.log")
    if not paths:
        print(f"{_NAMES[INSUFFICIENT]}: no logs found in {log_dir}/")
        return INSUFFICIENT

    measured = [measure(p, cfg) for p in paths]
    qualifying = [m for m in measured if m["qualifies"]]

    if not qualifying:
        print(f"{_NAMES[INSUFFICIENT]}: {len(measured)} logs, none qualifying")
        print(f"  need a post-warm-up window of {cfg['min_window_h']}h and "
              f"{cfg['min_samples']} samples")
        for m in measured[-5:]:
            print(f"    {m['log']:<34} {m['reason']}")
        print("\n  This is NOT a pass. Short sessions underread an accumulating")
        print("  defect roughly tenfold; run a longer session before concluding.")
        return INSUFFICIENT

    latest = qualifying[-1]
    history = [m["rate"] for m in qualifying[:-1]]

    if args.json:
        print(json.dumps({"latest": latest, "history": history,
                          "verdict": _NAMES[latest["verdict"]]}, indent=2, default=str))
        return latest["verdict"]

    for m in (measured if args.all else [latest]):
        if not m["qualifies"]:
            print(f"  {m['log']:<34} skipped — {m['reason']}")
            continue
        print(f"  {m['log']:<34} {m['window_h']:>5.2f}h  {m['signal']:>6} "
              f"{m['rate']:+7.0f} MB/h  game {('%+.0f' % m['game']) if m['game'] is not None else 'n/a':>7}")

    print()
    print(f"latest qualifying : {latest['log']}  ({latest['window_h']:.2f}h window)")
    print(f"  signal          : {latest['signal']} ({latest['confidence']} confidence)")
    print(f"  wingman growth  : {latest['rate']:+.0f} MB/h   (pass under {latest['pass_at']:.0f})")
    if latest["game"] is not None:
        print(f"  game growth     : {latest['game']:+.0f} MB/h   "
              "(reported, never gated — Anomaly 002)")
    if history:
        print(f"  history         : median {statistics.median(history):+.0f} MB/h, "
              f"range {min(history):+.0f} to {max(history):+.0f} over {len(history)} sessions")
    if latest["confidence"] == "low":
        print("  NOTE: rss fallback — arena retention inflates this; treat a "
              "borderline result as unproven")

    sev = latest.get("severity")
    print(f"\n{_NAMES[latest['verdict']]}"
          + (f" ({sev} — at or over {latest['fail_at']:.0f} MB/h)" if sev == "clear"
             else f" ({sev} — between {latest['pass_at']:.0f} and "
                  f"{latest['fail_at']:.0f} MB/h)" if sev == "borderline" else ""))
    return latest["verdict"]


if __name__ == "__main__":
    sys.exit(main())

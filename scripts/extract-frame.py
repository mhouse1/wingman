#!/usr/bin/env python3
"""Design 012 — pull one still frame from a recorded session video.

This is the literal mechanism behind "point Claude at a specific timestamp":
grep the BT JSONL trace (logs/bt_trace_<run_id>.jsonl) for the transition of
interest, take its "t" (elapsed seconds since session start — the same unit
the video's own timeline uses, since both start at the same moment), then
run this against logs/session_<run_id>.mp4 to see what the screen showed.

Usage:
    extract-frame.py --video logs/session_20260913_120000.mp4 --at 1842.3 --out /tmp/frame.png
"""

from __future__ import annotations

import argparse
import sys

import cv2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--video", required=True, help="Path to a session_*.mp4 file")
    ap.add_argument("--at", type=float, required=True, help="Elapsed seconds into the video")
    ap.add_argument("--out", required=True, help="Where to write the extracted PNG")
    args = ap.parse_args(argv)

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"error: could not open {args.video}", file=sys.stderr)
        return 1
    try:
        cap.set(cv2.CAP_PROP_POS_MSEC, args.at * 1000.0)
        ok, frame = cap.read()
        if not ok:
            duration = cap.get(cv2.CAP_PROP_FRAME_COUNT) / (cap.get(cv2.CAP_PROP_FPS) or 1.0)
            print(f"error: no frame at {args.at}s (video is ~{duration:.1f}s long)", file=sys.stderr)
            return 1
        cv2.imwrite(args.out, frame)
    finally:
        cap.release()
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

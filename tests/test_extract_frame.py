"""Design 012 — scripts/extract-frame.py, the mechanism behind "point Claude
at a specific timestamp": pull the still frame a session's video showed at a
given elapsed-seconds mark."""

import importlib.util
from pathlib import Path

import cv2
import numpy as np
import pytest

_SPEC = importlib.util.spec_from_file_location("extract_frame", Path("scripts/extract-frame.py"))
extract_frame = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(extract_frame)


def _write_synthetic_video(path, fps=10.0, n_frames=20, size=(8, 8)):
    """Frame i is solid greyscale value i*10 — a known, checkable fingerprint."""
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, size)
    for i in range(n_frames):
        frame = np.full((size[1], size[0], 3), i * 10, dtype=np.uint8)
        writer.write(frame)
    writer.release()
    return fps, n_frames


def test_extracts_the_frame_at_the_requested_timestamp(tmp_path):
    video = tmp_path / "session.mp4"
    fps, _ = _write_synthetic_video(video)
    out = tmp_path / "frame.png"

    # Frame 5 covers [0.5s, 0.6s) at 10fps — 0.55s must land on it.
    rc = extract_frame.main(["--video", str(video), "--at", "0.55", "--out", str(out)])
    assert rc == 0
    assert out.exists()
    got = cv2.imread(str(out))
    assert got is not None
    # frame 5's fingerprint is 50; mp4v is lossy (inter-frame prediction blurs
    # a solid color a few units), so this checks "landed on the right frame,"
    # not exact pixel equality. int() avoids a numpy uint8 underflow in the
    # comparison itself (50 - np.uint8(x) wraps instead of going negative).
    assert int(got[0, 0, 0]) == pytest.approx(50, abs=15)


def test_a_timestamp_past_the_end_is_an_error_not_a_crash(tmp_path):
    video = tmp_path / "session.mp4"
    _write_synthetic_video(video, n_frames=5)
    rc = extract_frame.main(["--video", str(video), "--at", "999.0",
                             "--out", str(tmp_path / "frame.png")])
    assert rc == 1


def test_a_missing_video_is_an_error_not_a_crash(tmp_path):
    rc = extract_frame.main(["--video", str(tmp_path / "nope.mp4"), "--at", "0.0",
                             "--out", str(tmp_path / "frame.png")])
    assert rc == 1

"""Design 012 — paired session video + behavior tree trace.

No real mss/X11 or ffmpeg dependency here: VideoRecorder takes an injectable
capture_factory precisely so this can run in CI without a display.
"""

import json
import time

import numpy as np
import pytest

from wingman.session_recording import BtTraceWriter, VideoRecorder


class FakeClock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _FakeCapture:
    def __init__(self, frame=None):
        self._frame = frame if frame is not None else np.zeros((4, 4, 3), dtype=np.uint8)
        self.frames_served = 0
        self.cleaned_up = False

    def get_frame(self):
        self.frames_served += 1
        return self._frame

    def cleanup(self):
        self.cleaned_up = True


# --- BtTraceWriter -----------------------------------------------------------

def test_records_one_json_line_per_transition(tmp_path):
    clock = FakeClock(1000.0)
    path = tmp_path / "trace.jsonl"
    w = BtTraceWriter(path, clock=clock)
    clock.advance(1.5)
    w.record("Idle", "Engage", {"Idle": "FAILURE", "Engage": "RUNNING"})
    clock.advance(3.0)
    w.record("Engage", "Disengage", {"Engage": "FAILURE", "Disengage": "RUNNING"})
    w.close()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first == {"t": 1.5, "from": "Idle", "to": "Engage",
                     "statuses": {"Idle": "FAILURE", "Engage": "RUNNING"}}
    second = json.loads(lines[1])
    assert second["t"] == pytest.approx(4.5)
    assert second["to"] == "Disengage"


def test_t_is_elapsed_since_construction_not_wall_clock(tmp_path):
    # A fresh writer at a later wall-clock start must read t=0 at its own
    # first record, not accumulate the earlier writer's elapsed time.
    clock = FakeClock(5000.0)
    w = BtTraceWriter(tmp_path / "a.jsonl", clock=clock)
    clock.advance(10.0)
    w.record("Idle", "Engage", {})
    w.close()
    line = json.loads((tmp_path / "a.jsonl").read_text().splitlines()[0])
    assert line["t"] == pytest.approx(10.0)


def test_a_fresh_file_never_appends_to_a_previous_session(tmp_path):
    path = tmp_path / "trace.jsonl"
    path.write_text('{"stale": true}\n', encoding="utf-8")
    w = BtTraceWriter(path, clock=FakeClock(0.0))
    w.record("Idle", "Engage", {})
    w.close()
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert "stale" not in lines[0]


# --- VideoRecorder -------------------------------------------------------

def _recorder(tmp_path, fake_cap, fps=50.0):
    return VideoRecorder(
        region=(0, 0, 4, 4), monitor_index=1, game_window_offset=None,
        display=None, out_path=str(tmp_path / "session.mp4"),
        fps=fps, scale=1.0, capture_factory=lambda: fake_cap,
    )


def test_start_and_stop_leaves_no_thread_running(tmp_path):
    fake_cap = _FakeCapture()
    rec = _recorder(tmp_path, fake_cap)
    rec.start()
    time.sleep(0.1)
    rec.stop(timeout=2.0)
    assert not rec._thread.is_alive()


def test_stop_releases_the_capture(tmp_path):
    fake_cap = _FakeCapture()
    rec = _recorder(tmp_path, fake_cap)
    rec.start()
    time.sleep(0.1)
    rec.stop(timeout=2.0)
    assert fake_cap.cleaned_up is True


def test_frames_are_written_to_the_output_file(tmp_path):
    fake_cap = _FakeCapture()
    out = tmp_path / "session.mp4"
    rec = VideoRecorder(region=(0, 0, 4, 4), monitor_index=1, game_window_offset=None,
                        display=None, out_path=str(out), fps=50.0, scale=1.0,
                        capture_factory=lambda: fake_cap)
    rec.start()
    time.sleep(0.2)
    rec.stop(timeout=2.0)
    assert fake_cap.frames_served > 0
    assert out.exists()
    assert out.stat().st_size > 0


def test_a_none_frame_is_skipped_not_crashed_on(tmp_path):
    fake_cap = _FakeCapture()
    fake_cap.get_frame = lambda: None  # e.g. a transient capture failure
    rec = _recorder(tmp_path, fake_cap)
    rec.start()
    time.sleep(0.1)
    rec.stop(timeout=2.0)
    assert not rec._thread.is_alive()   # must not have died on the None frame


# --- frame timing (Research 017 step 3, 2026-10-19) ---------------------------
#
# The 2026-10-04 videos ran about 5% slow: each frame waited a full period
# after the last one was written, so every grab stretched the period, and a
# death the trace put at 10,050.9 s showed 9,571 s into the video. Frames are
# now on an absolute schedule and each one's time is written beside the video.

def _times(out_path):
    from wingman.session_recording import frames_path
    lines = frames_path(out_path).read_text(encoding="utf-8").splitlines()
    assert lines[0] == "frame,time"
    rows = [line.split(",") for line in lines[1:]]
    return [int(i) for i, _ in rows], [float(t) for _, t in rows]


def test_the_schedule_is_absolute():
    from wingman.session_recording import next_frame_due
    assert next_frame_due(10.0, 10.02, 0.5) == 10.5      # a 20 ms grab costs nothing
    assert next_frame_due(10.0, 10.6, 0.5) == pytest.approx(11.1)   # a period behind: restart


def test_a_slow_grab_does_not_stretch_the_frame_period(tmp_path):
    """The old loop waited 1/fps after each write: a grab of 40% of the period
    made it 1.4 periods. On the absolute schedule the period stays."""
    fake_cap = _FakeCapture()
    slow = fake_cap.get_frame

    def get_frame():
        time.sleep(0.08)
        return slow()

    fake_cap.get_frame = get_frame
    out = tmp_path / "session.mp4"
    rec = VideoRecorder(region=(0, 0, 4, 4), monitor_index=1, game_window_offset=None,
                        display=None, out_path=str(out), fps=5.0, scale=1.0,
                        capture_factory=lambda: fake_cap)
    rec.start()
    time.sleep(1.7)
    rec.stop(timeout=2.0)
    _, stamps = _times(out)
    gaps = sorted(b - a for a, b in zip(stamps[:-1], stamps[1:], strict=True))
    assert len(gaps) >= 5
    assert gaps[len(gaps) // 2] < 0.24, gaps     # the old loop: 0.28 s


def test_each_written_frame_has_its_wall_clock_time(tmp_path):
    served = {"n": 0}
    fake_cap = _FakeCapture()
    frame = np.zeros((4, 4, 3), dtype=np.uint8)

    def get_frame():                              # every other grab fails
        served["n"] += 1
        return frame if served["n"] % 2 else None

    fake_cap.get_frame = get_frame
    out = tmp_path / "session.mp4"
    before = time.time()
    rec = _recorder(tmp_path, fake_cap, fps=50.0)
    rec.start()
    time.sleep(0.3)
    rec.stop(timeout=2.0)
    after = time.time()
    index, stamps = _times(out)
    assert index == list(range(len(index)))
    assert len(index) == (served["n"] + 1) // 2  # only the frames written
    assert all(b > a for a, b in zip(stamps[:-1], stamps[1:], strict=True))
    assert before <= stamps[0] and stamps[-1] <= after

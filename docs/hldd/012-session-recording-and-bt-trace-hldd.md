# Design 012 — Session Video Recording Paired with a Behavior Tree Trace

| Status | Date       | Wingman Version |
|--------|------------|-----------------|
| Draft  | 2026-09-13 | 1.8.9           |

## Purpose

Research 013 added a live per-node status view of the ADR 024 selector
(`tree_status_text`, logged at DEBUG on every tactic-selection change). That
answers "what did the tree decide" after the fact by reading `wingman.log`,
but text alone can't answer "what did the game actually look like at that
moment" — and watching the dump live is not useful, since a 1.5 s decision
loop gives no time to act on what's on screen before it's already resolved.

This design pairs the existing trace with a video recording of the same
session, both keyed to the same timestamp, so a session can be reviewed
after the fact — by the operator, or by pointing Claude at a specific
elapsed-seconds mark — by cross-referencing "what the tree did" against
"what was on screen" at that instant.

## Problem Statement

Two gaps, addressed together because they're only useful together:

1. **The tree-status trace is free text.** `tree_status_text` produces
   ascii art meant for a human skimming a log narrative. Nothing can query
   it ("every transition into Climb, with statuses") without regex-scraping
   box-drawing characters.
2. **Nothing records the screen during a live session.** The only existing
   frame capture is anomaly-triggered (`BoundaryPerceptionHandler`'s blind
   captures, `UnknownAnomalyRecorder`, `HealthDropoutRecorder`, crash
   frames) — single PNGs at specific trigger conditions, not a continuous
   record. There is no way to look at what the screen showed at an
   arbitrary moment that didn't happen to trip one of those triggers.

Both are opt-in, session-scoped artifacts — this is a debugging aid, not a
new always-on subsystem, and specifically not the ADR 044/045 replay-gate
machinery (`LivePathCaptureEngine`), which captures screenshots to *feed
back into* wingman for testing, not to record what a real session showed.

## Goals

1. A structured, line-oriented trace of every tactic-selection change,
   parseable without scraping ascii art.
2. A video of the same session, sharing a single session identifier with
   the trace (and with `PerformanceTracker.run_id`, already the project's
   canonical per-session stamp), so the two can be opened side by side or
   cross-referenced by a script.
3. A way to jump to a specific elapsed-seconds mark in the video and pull a
   still frame — the concrete mechanism for "point Claude at a timestamp."
4. Opt-in via a single `v` argument on the existing run targets (`make r
   v`, `make rd v`, `make r1 v`, `make r2 v`) — recording is not the
   default, given the disk-space and CPU cost of a multi-hour capture.
5. **No new dependency outside wingman's `uv`-managed environment.**
   Everything here must run through the same `uv run --active python`
   wingman itself runs through (`$(PYTHON_RUN)` in the Makefile) — nothing
   shells out to a system tool `uv sync` doesn't manage. This is a hard
   constraint, not a preference (see "Why `cv2.VideoWriter`, not
   `ffmpeg`" below).

## Non-Goals

- Replacing the OCR-cadence `Capture` instance the main loop already uses
  for analysis. The recorder owns a second, independent capture instance
  (see "Why a second `Capture` instance").
- Audio.
- Long-term storage, upload, or retention policy — files land under
  `logs/`, same as archived text logs, and are the operator's to manage.
- A live-viewing UI. This is a record-now, review-later tool.
- Replacing or touching `LivePathCaptureEngine`/`ScreenshotReplayCapture` —
  those serve the replay-gate test lane and are out of scope here.

## Design

### Why `cv2.VideoWriter`, not `ffmpeg`

`ffmpeg` is not installed on this machine (`which ffmpeg` → not found), and
even where it is, invoking it would mean shelling out to a system binary
`uv sync` cannot install, version-pin, or reproduce — exactly the
containerization/reproducibility property this design is required to keep.
`opencv-python` (`cv2`) is already a `pyproject.toml` dependency, already
`uv`-managed, and its `VideoWriter` (confirmed working here without any
system codec install — `mp4v` fourcc, verified by a smoke test producing a
playable `.mp4`) needs nothing outside what `uv sync --all-groups` already
provides. This is the same reasoning Research 013 used to prefer Mermaid
over `render_dot_tree`'s graphviz dependency: don't reach for a system tool
when an already-managed one does the job.

### Why a second `Capture` instance

`wingman/capture.py`'s `Capture` wraps `mss`, which is thread-local — the
existing constraint documented in `CLAUDE.md` ("Must be called from the
thread that constructed it"). The recorder runs on its own daemon thread
(below), so it constructs its **own** `Capture`, with the same `region`,
`monitor_index`, `game_window_offset`, and `display` (the nested display
when ADR 099's nested lane is active) that the main loop's own `cap`
already uses — guaranteeing the recording shows the exact view the
analyzer/tree are reacting to, not the operator's own screen or a
differently-cropped view.

### Component 1 — `tree_status_dict`

`wingman/behavior_tree.py` gains a structured sibling to the existing
`tree_status_text`:

```python
def tree_status_dict(tree: py_trees.trees.BehaviourTree) -> dict[str, str]:
    """{node name: Status.name} for every node, JSON-serializable sibling
    of tree_status_text — same data, structured instead of ascii art."""
    return {n.name: n.status.name for n in tree.root.iterate()}
```

### Component 2 — `BtTraceWriter`

New `wingman/session_recording.py`. Appends one compact JSON object per
tactic-selection change to `logs/bt_trace_<run_id>.jsonl` — append mode,
flushed per write, so a crash loses at most the in-flight line (matching
the existing text log's crash-safety property, not the "one JSON blob
written at shutdown" pattern `performance.py`'s `run_*.json` uses, which
loses the whole session if the process dies before that write happens):

```json
{"t": 1842.34, "from": "Idle", "to": "Engage", "statuses": {"Idle": "FAILURE", "...": "..."}}
```

`t` is elapsed seconds since session start (matching `PerformanceTracker`'s
own `_session_start` epoch), not wall-clock — the same unit the video's own
timeline uses, which is what makes the two directly comparable: "trace says
a transition at t=1842.3" means "look at 1842.3s into the video."

### Component 3 — `VideoRecorder`

Also in `wingman/session_recording.py`. A stoppable daemon thread per this
project's standing convention (`CLAUDE.md` "Stoppable Daemon Threads"):

```python
class VideoRecorder:
    def __init__(self, region, monitor_index, game_window_offset, display,
                 out_path, fps=2.0, scale=0.5):
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        ...

    def _run(self):
        cap = Capture(self._region, self._monitor_index,
                      game_window_offset=self._game_window_offset,
                      display=self._display)          # own instance — thread-local mss
        writer = None
        try:
            while not self._stop.wait(timeout=1.0 / self._fps):
                frame = cap.get_frame()
                if frame is None:
                    continue
                if writer is None:
                    h, w = frame.shape[:2]
                    writer = cv2.VideoWriter(self._out_path,
                        cv2.VideoWriter_fourcc(*"mp4v"), self._fps,
                        (int(w * self._scale), int(h * self._scale)))
                if self._scale != 1.0:
                    frame = cv2.resize(frame, None, fx=self._scale, fy=self._scale)
                writer.write(frame)
        finally:
            if writer is not None:
                writer.release()
            if hasattr(cap, "cleanup"):
                cap.cleanup()

    def start(self): self._thread.start()

    def stop(self, timeout=5.0):
        self._stop.set()
        self._thread.join(timeout=timeout)
```

Default `fps=2.0` and `scale=0.5`: this is a debugging aid reviewed at a
tactic-selection cadence (selections change on the order of seconds, not
frames), not a cinematic capture — a low rate keeps a multi-hour session's
file size and CPU cost bounded. Both are configurable
(`config.yaml`'s new `session_recording:` block) since the right value
depends on the machine and how long sessions typically run.

### Wiring into `wingman/main.py`

- New flag: `parser.add_argument("--record-session", action="store_true", ...)`.
- Only in the real/live branch (not `replay_mode`/`capture_mode` — those
  already have their own dedicated capture engines and a video recording
  of a replay run would just be re-filming pre-recorded screenshots):
  when set, construct `VideoRecorder(region, monitor_index,
  game_window_offset, nested_display, out_path=f"logs/session_{tracker.run_id}.mp4",
  **cfg.get("session_recording", {}))` and `.start()` it, and construct
  `BtTraceWriter(f"logs/bt_trace_{tracker.run_id}.jsonl", clock=...)`,
  passed into `BehaviorTreeHandler(..., trace_writer=bt_trace_writer)`.
- `BehaviorTreeHandler.tick()` calls `self._trace_writer.record(...)` at
  the exact same selection-change edge that already logs
  `BT[...]: tactic X -> Y` and (at DEBUG) `tree_status_text` — one place
  decides "a transition happened," now with three sinks (INFO log line,
  DEBUG ascii dump, JSONL record) instead of two.
- Cleanup: `video_recorder.stop()` alongside the existing `if
  hasattr(cap, "cleanup"): cap.cleanup()` cleanup-block line, wrapped in the
  same defensive `try/except Exception` pattern already used there for
  every other cleanup step — a recording failure must never block a normal
  shutdown. `BtTraceWriter` closes its file the same way.

### Makefile — the `v` argument

`make rd v` passes `v` as a second `make` goal alongside `rd`. GNU Make
treats every word after the target as an additional goal to build, so `v`
must exist as a real (phony, no-op) target, and `rd`'s recipe reads
`MAKECMDGOALS` to notice it was named:

```makefile
.PHONY: v
v:
	@:

RECORD_FLAG = $(if $(filter v,$(MAKECMDGOALS)),--record-session,)

rd: $(GAME_LAUNCH_DEPS)
	$(WINGMAN_ENV) $(WINGMAN_NESTED_ENV) $(WINGMAN_NICE) $(PYTHON_RUN) -m wingman.main --log-file wingman.log $(RECORD_FLAG)
```

`r1`/`r2` chain to `rd` as a prerequisite already, so this one change
covers `make r1 v` / `make r2 v` too; `r`'s recipe gets the same
`$(RECORD_FLAG)` addition independently for `make r v`.

### Component 4 — pulling a still frame at a timestamp

`scripts/extract-frame.py`, following the existing `scripts/*.py` shape
(argparse, `sys.path` bootstrap, no new dependency — `cv2` again):

```
extract-frame.py --video logs/session_<run_id>.mp4 --at 1842.3 --out /tmp/frame.png
```

Seeks via `cv2.VideoCapture`'s `CAP_PROP_POS_MSEC`, reads one frame, writes
it with `cv2.imwrite`. This is the literal mechanism behind "point Claude
at a specific timestamp": grep the JSONL trace for the transition of
interest, take its `t`, run this script, then read the resulting PNG.

## Config

New `config.yaml` block, defaults matching the tuning discussion above:

```yaml
session_recording:
  fps: 2.0
  scale: 0.5
```

## Failure Modes and Handling

- **Disk fills mid-session.** `cv2.VideoWriter.write` doesn't raise on a
  full disk in all backends; the recorder thread checks `writer.isOpened()`
  after construction and logs a WARNING once (not per-frame) if the writer
  never opened, rather than silently producing an empty/corrupt file.
- **Recorder thread dies.** It is a daemon thread wrapped in its own
  `try/finally`; a crash there must not take down the main loop. Exceptions
  inside `_run` are caught at the top of the loop body and logged, not
  propagated — the same "never let an instrumentation failure become a
  flight-safety failure" posture this codebase already applies to capture
  and logging elsewhere.
- **`--record-session` without the nested lane.** Recording targets
  whatever `display`/`region` the real capture uses, nested or not — no
  special case needed, since `Capture` already resolves that.
- **Video file left behind after a crash.** `cv2.VideoWriter` buffers
  internally; a hard kill (not the graceful `z`/SIGTERM path) can leave a
  truncated but still-openable `.mp4` (most container muxers finalize
  incrementally) — noted as a known limitation, not solved here.

## Testing

- `tree_status_dict`: unit test alongside the existing
  `tree_status_text` tests in `tests/test_behavior_tree.py` — same
  harness, asserting the dict's values against `Status.name` strings.
- `BtTraceWriter`: writes to a temp path, asserts each line is valid JSON
  with the expected keys, and that a fresh file is created per instance
  (no accidental append across sessions).
- `VideoRecorder`: constructed against a stub `Capture`-shaped object (no
  real X11/mss dependency in CI, matching how `tests/test_tick_handlers.py`
  already stubs collaborators) returning synthetic frames; asserts
  `start()`/`stop()` leaves no thread alive (`Thread.is_alive()` false
  after `stop()` returns) and that the output file exists and is non-empty
  after a few simulated ticks via a fake clock, not a real `time.sleep`.
- `scripts/extract-frame.py`: a smoke test writing a tiny synthetic video
  with known per-frame content, then asserting the extracted frame at a
  given timestamp matches the frame that should be there.
- Gate: `make lint && make test` per `/iterate` discipline, one component
  at a time, before wiring the next.

## Rollout

Land in the order listed under Design (trace dict → trace writer → video
recorder → main.py wiring → Makefile → extract-frame script), gated by
`make lint && make test` after each. This is new, additive, opt-in
functionality — no existing behavior changes, so no live-trial requirement
in the ADR 070/073 sense. A live session with `make rd v` is still worth
running once implementation lands, specifically to confirm file sizes and
CPU overhead are acceptable over a multi-hour run — recorded here as a
follow-up, not a blocking gate.

## References

- `docs/research/013-behavior-tree-tooling-and-composability.md` — the
  live-status view (`tree_status_text`) this design pairs with a video
  recording.
- `docs/adr/099-nested-display-lane-for-unattended-operation.md` — the
  `display`/region resolution this design's second `Capture` instance
  reuses.
- `wingman/performance.py` — `PerformanceTracker.run_id`, the existing
  per-session timestamp convention this design's file names share.
- `wingman/capture.py` — `Capture`, reused (not modified) for the
  recorder's own capture instance.

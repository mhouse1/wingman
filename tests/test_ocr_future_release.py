"""ADR 103: a scan cycle must not leave queued OCR work holding frames.

Every task submitted to the OCR pool carries the WHOLE frame — 1920x1200x3 is
6.9 MB — because get_crop runs inside the worker, deliberately, so the crop sits
under the caller's result timeout (see _process_crop_region).

A cycle submits every lobby crop, but the handlers break on the first hit, so up
to three futures per cycle are never read. While the workers keep up that is
invisible. When they stall it is not:

    2026-09-01 11:09   OCR stops completing (game servers in maintenance)
    2026-09-01 11:07   rss=2553MB
    2026-09-01 11:17   rss=3688MB          ~160 frames pinned
    2026-09-01 11:41   LIVENESS GUARD: no progress for 901s — session ended

The leak gate then read that step as +252 MB/h and failed the release, on a
session whose median-session growth is +1 MB/h.
"""

import concurrent.futures
import gc
import weakref

import numpy as np

from wingman.analyzer import _crop_for_ocr, _process_crop_region, _process_text_region


def test_passing_the_frame_pins_it_and_cancel_does_not_help():
    """The premise, and the fix that does NOT work.

    cancel() looks like the obvious answer and is not: CPython leaves the
    _WorkItem and its arguments in the queue until a worker pops it, which is
    precisely what a stalled pool never does."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    block = concurrent.futures.Future()
    executor.submit(block.result)                     # occupy the only worker
    try:
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        ref = weakref.ref(frame)
        fut = executor.submit(_process_crop_region, frame, (0.0, 0.0, 0.5, 0.5), [])
        del frame
        gc.collect()
        assert ref() is not None, "a queued task holds the whole frame"
        assert fut.cancel(), "the future reports itself cancelled"
        gc.collect()
        assert ref() is not None, (
            "cancel() does NOT release the frame — the work item stays queued. "
            "This is why the fix crops in the caller instead.")
    finally:
        block.set_result(None)
        executor.shutdown(wait=False)


def test_a_detached_crop_does_not_pin_the_frame():
    """The actual fix. get_crop returns a VIEW whose .base is the frame, so the
    copy is load-bearing: without it a queued crop pins all 6.9 MB just as the
    frame did."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    block = concurrent.futures.Future()
    executor.submit(block.result)                     # occupy the only worker
    try:
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        ref = weakref.ref(frame)
        crop = _crop_for_ocr(frame, (0.0, 0.0, 0.5, 0.5))
        assert crop.base is None, "the crop must be detached, not a view"
        executor.submit(_process_text_region, crop, [])
        del frame
        gc.collect()
        assert ref() is None, "a queued crop must not keep the frame alive"
    finally:
        block.set_result(None)
        executor.shutdown(wait=False)


def test_cancel_is_harmless_once_the_task_has_run():
    """The cleanup cancels indiscriminately, including futures the cycle already
    consumed. That must not disturb a completed result."""
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        fut = executor.submit(_process_crop_region, frame, (0.0, 0.0, 0.5, 0.5), [])
        result = fut.result(timeout=30)
        assert fut.cancel() is False, "a finished future reports it cannot be cancelled"
        assert fut.result(timeout=1) == result, "the result must survive the cancel"
    finally:
        executor.shutdown(wait=False)


def test_the_scan_loop_submits_crops_not_frames():
    """The fix, at the sites that matter. A future regression here is invisible
    until a stall, so assert the shape of the submission directly."""
    import inspect
    from wingman.analyzer import GameStateAnalyzer

    scan = inspect.getsource(GameStateAnalyzer._run_game_lobby_quick_scan)
    assert "_crop_for_ocr(" in scan
    # Every submit in this method must hand over a detached crop.
    for call in scan.split("executor.submit(")[1:]:
        head = call[:200]
        assert "_crop_for_ocr(" in head or "block.result" in head, \
            f"a quick-scan submit does not pass a detached crop: {head[:90]!r}"

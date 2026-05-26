from types import SimpleNamespace

from wingman.main import _update_waiting_fallback


class _AnalyzerStub:
    def __init__(self, diff=0.2, has_cancel=True):
        self._diff = diff
        self.crops = {"CANCEL": object()} if has_cancel else {}

    def compute_waiting_cancel_diff(self, _frame):
        return self._diff


class _AnalyzerNoDiff(_AnalyzerStub):
    def __init__(self):
        super().__init__(diff=0.0, has_cancel=True)

    def compute_waiting_cancel_diff(self, _frame):
        return None


def _logger():
    return SimpleNamespace(debug=lambda *args, **kwargs: None)


def test_update_waiting_fallback_triggers_after_thresholds():
    analyzer = _AnalyzerStub(diff=0.2)
    score = 0
    consecutive = 0

    score, consecutive, triggered, _ = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=9.0,
        play_visible=False,
        score=score,
        consecutive=consecutive,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )
    assert triggered is False

    score, consecutive, triggered, _ = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=12.0,
        play_visible=False,
        score=score,
        consecutive=consecutive,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )
    assert triggered is True


def test_update_waiting_fallback_resets_when_play_visible():
    analyzer = _AnalyzerStub(diff=0.2)

    score, consecutive, triggered, _ = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=10.0,
        play_visible=True,
        score=3,
        consecutive=2,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )

    assert score == 0
    assert consecutive == 0
    assert triggered is False


def test_update_waiting_fallback_does_not_run_without_baseline_diff():
    analyzer = _AnalyzerNoDiff()

    score, consecutive, triggered, diff = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=20.0,
        play_visible=False,
        score=0,
        consecutive=0,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )

    assert score == 0
    assert consecutive == 0
    assert triggered is False
    assert diff is None


def test_update_waiting_fallback_not_before_min_elapsed():
    analyzer = _AnalyzerStub(diff=0.2)

    score, consecutive, triggered, _ = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=2.0,
        play_visible=False,
        score=0,
        consecutive=0,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )

    assert score == 0
    assert consecutive == 0
    assert triggered is False


def test_update_waiting_fallback_requires_cancel_crop():
    analyzer = _AnalyzerStub(diff=0.2, has_cancel=False)

    score, consecutive, triggered, _ = _update_waiting_fallback(
        analyzer,
        frame=None,
        elapsed_waiting=10.0,
        play_visible=False,
        score=0,
        consecutive=0,
        enabled=True,
        diff_threshold=0.08,
        score_threshold=4,
        consecutive_required=2,
        min_elapsed_s=6.0,
        logger=_logger(),
    )

    assert score == 0
    assert consecutive == 0
    assert triggered is False

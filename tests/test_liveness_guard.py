"""Liveness guard and the blackout ESC ceiling (ADR 093).

The 2026-08-24 session ran 3h27m and was inert for 110 minutes of it: a PROFILE
overlay opened over the lobby, no recovery path was eligible, and nothing was
watching for absence of work. These pin the two generic protections — the guard
that notices, and the ceiling that stops ESC suppression being permanent.
"""

from wingman.liveness_guard import LivenessGuard


class _Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def _guard(**cfg):
    clock = _Clock()
    cfg.setdefault("enabled", True)
    cfg.setdefault("stall_limit_s", 300.0)
    cfg.setdefault("hard_limit_s", 900.0)
    return LivenessGuard(cfg, clock=clock), clock


def test_quiet_below_the_soft_limit():
    g, clock = _guard()
    clock.advance(299)
    assert g.check() is False
    assert g.should_stop() is False


def test_soft_limit_fires_but_does_not_stop():
    g, clock = _guard()
    clock.advance(301)
    assert g.check() is True
    assert g.should_stop() is False, "soft limit must escalate, not end the session"


def test_hard_limit_stops():
    g, clock = _guard()
    clock.advance(901)
    assert g.check() is True
    assert g.should_stop() is True
    assert "900" in g.reason


def test_progress_resets_the_clock():
    g, clock = _guard()
    clock.advance(299)
    g.note_progress("OCR")
    clock.advance(299)
    assert g.check() is False, "progress must reset the stall clock"


def test_state_change_alone_counts_as_progress():
    """A quiet lobby produces little OCR but transitions normally."""
    g, clock = _guard()
    for _ in range(10):
        clock.advance(200)
        g.note_progress("state change")
    assert g.check() is False


def test_ocr_alone_counts_as_progress():
    """A long battle produces few state changes but plenty of OCR."""
    g, clock = _guard()
    for _ in range(10):
        clock.advance(200)
        g.note_progress("OCR")
    assert g.check() is False


def test_the_livelock_shape_is_caught():
    """The actual failure: neither signal for 110 minutes."""
    g, clock = _guard()
    clock.advance(110 * 60)
    assert g.check() is True
    assert g.should_stop() is True


def test_disabled_guard_never_fires():
    g, clock = _guard(enabled=False)
    clock.advance(10_000)
    assert g.check() is False
    assert g.should_stop() is False


def test_check_never_raises(monkeypatch):
    g, _ = _guard()
    monkeypatch.setattr(g, "stalled_for",
                        lambda now=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert g.check() is False


def test_stall_clears_when_progress_resumes():
    g, clock = _guard()
    clock.advance(301)
    assert g.check() is True
    g.note_progress("state change")
    assert g.check() is False


# --- blackout ESC ceiling ---------------------------------------------------

class _Analyzer:
    """Only the pieces blackout_esc_suppressed touches."""
    from wingman.analyzer import GameStateAnalyzer as _GSA
    lobby_blackout_active = _GSA.lobby_blackout_active
    lobby_blackout_age_s = _GSA.lobby_blackout_age_s
    blackout_esc_suppressed = _GSA.blackout_esc_suppressed

    def __init__(self, since=0.0, ceiling=120.0):
        self._lobby_blackout_since = since
        self._blackout_esc_ceiling_s = ceiling


def test_no_blackout_means_no_suppression():
    assert _Analyzer(since=0.0).blackout_esc_suppressed() is False


def test_esc_suppressed_below_the_ceiling():
    import time
    assert _Analyzer(since=time.time() - 10).blackout_esc_suppressed() is True


def test_esc_released_past_the_ceiling():
    """The fix: suppression is a delay, not a veto (ADR 093)."""
    import time
    a = _Analyzer(since=time.time() - 300, ceiling=120.0)
    assert a.blackout_esc_suppressed() is False
    assert a.lobby_blackout_active() is True, "still a blackout — just no longer muted"


def test_zero_ceiling_restores_adr087_behaviour():
    import time
    a = _Analyzer(since=time.time() - 100_000, ceiling=0.0)
    assert a.blackout_esc_suppressed() is True

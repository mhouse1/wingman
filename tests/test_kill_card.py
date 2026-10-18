"""Code Review 019 cycle 19: the death's cause read from the game's kill card.

The fixtures are the bottom strip (from 0.78 of the height down) of three black
box frames of 2026-10-17: two respawn screens after enemy kills (00:42:35, a
Meteor from [LAFB] VIPER; 00:17:29, a K-5 Alkali from [GoT] Blister1996) and one
after the terrain crash at 00:44:18, which had no card.
"""

import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from wingman.kill_card import DeathCard, KillCardReader, has_heading, read_card

_FIXTURES = Path(__file__).parent / "fixtures"


def _frame(name, size=(600, 960)):
    """A black frame of `size` with the strip in its bottom 22%, scaled to it."""
    h, w = size
    frame = np.zeros((h, w, 3), np.uint8)
    strip = cv2.imread(str(_FIXTURES / name))
    top = int(0.78 * h)
    frame[top:, :] = cv2.resize(strip, (w, h - top), interpolation=cv2.INTER_LINEAR)
    return frame


@pytest.mark.parametrize("text, expected", [
    ("Killed By", True), ("KILLED BY", True), ("Kiled By", False), ("", False), ("RESPAWN 5", False)])
def test_the_heading_is_killed_by(text, expected):
    assert has_heading(text) is expected


@pytest.fixture(scope="module")
def reader():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


@pytest.mark.slow
@pytest.mark.parametrize("name, size, killer_has, weapon_starts", [
    ("kill_card_enemy_strip.png", (600, 960), "VIPER", "meteor"),
    ("kill_card_enemy_strip.png", (1200, 1920), "VIPER", "meteor"),
    ("kill_card_enemy_k5_strip.png", (600, 960), "BLISTER", "k-5")])
def test_a_kill_card_names_the_killer_and_the_weapon(reader, name, size, killer_has, weapon_starts):
    seen, killer, weapon = read_card(reader, _frame(name, size))
    assert seen
    assert killer_has in killer.upper()
    assert weapon.lower().startswith(weapon_starts)


@pytest.mark.slow
def test_a_crash_has_no_card(reader):
    assert read_card(reader, _frame("kill_card_crash_strip.png")) == (False, "", "")


# -- the reader thread ------------------------------------------------------------

class _Script:
    """read_card stand-in: answers from the frame's first pixel, 1 = a card."""

    def __init__(self):
        self.calls = 0
        self.gate = threading.Event()
        self.gate.set()

    def __call__(self, _reader, frame):
        self.gate.wait(timeout=2.0)
        self.calls += 1
        if frame[0, 0, 0] == 1:
            return True, "[LAFB] VIPER", "Meteor"
        return False, "", ""


def _marked(card: bool):
    frame = np.zeros((6, 6, 3), np.uint8)
    frame[0, 0, 0] = 1 if card else 0
    return frame


def _reader(_monkeypatch, script):
    r = KillCardReader(read_fn=lambda frame: script(None, frame))
    r.start()
    return r


def _settle(script, calls, timeout=2.0):
    deadline = time.time() + timeout
    while script.calls < calls and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)


def test_a_death_with_a_card_is_an_enemy_kill(monkeypatch, caplog):
    script = _Script()
    r = _reader(monkeypatch, script)
    try:
        r.submit(_marked(False))                 # the explosion, before the card
        _settle(script, 1)
        r.submit(_marked(True))
        _settle(script, 2)
        with caplog.at_level("INFO", logger="wingman.kill_card"):
            card = r.end()
    finally:
        r.stop()
    assert card == DeathCard(reads=2, card_reads=1, killer="[LAFB] VIPER", weapon="Meteor")
    assert card.verdict == "enemy"
    assert "DEATH CARD: killed by '[LAFB] VIPER' with 'Meteor' (the card on 1 of 2 reads)" in [
        rec.getMessage() for rec in caplog.records]


def test_a_death_read_without_a_card_is_no_card(monkeypatch):
    script = _Script()
    r = _reader(monkeypatch, script)
    try:
        for _ in range(3):
            r.submit(_marked(False))
            _settle(script, script.calls + 1)
        card = r.end()
    finally:
        r.stop()
    assert card.verdict == "no card" and card.reads == 3


def test_a_death_with_no_frame_read_is_unread_and_the_session_counts_all_three(monkeypatch, caplog):
    script = _Script()
    r = _reader(monkeypatch, script)
    try:
        assert r.end().verdict == "unread"
        r.submit(_marked(True))
        _settle(script, 1)
        r.end()
        r.submit(_marked(False))
        _settle(script, 2)
        r.end()
    finally:
        with caplog.at_level("INFO", logger="wingman.kill_card"):
            r.stop()
    assert "DEATH CARDS: 3 deaths: enemy 1, no card 1, unread 1" in [
        rec.getMessage() for rec in caplog.records]


def test_a_read_that_lands_after_its_death_closed_is_dropped(monkeypatch):
    script = _Script()
    r = _reader(monkeypatch, script)
    try:
        script.gate.clear()                      # the read blocks until released
        r.submit(_marked(True))
        time.sleep(0.1)
        first = r.end()                          # closed while the read is under way
        script.gate.set()
        _settle(script, 1)
        second = r.end()
    finally:
        r.stop()
    assert first.verdict == "unread"
    assert second.verdict == "unread", "the late card was not counted for the next death"


def test_a_frame_the_pool_could_not_read_is_not_counted(monkeypatch):
    r = KillCardReader(read_fn=lambda frame: None)
    r.start()
    try:
        r.submit(_marked(True))
        time.sleep(0.2)
        card = r.end()
    finally:
        r.stop()
    assert card.verdict == "unread"


def test_a_disabled_reader_takes_nothing(monkeypatch):
    script = _Script()
    r = KillCardReader(read_fn=lambda frame: script(None, frame), enabled=False)
    r.start()
    r.submit(_marked(True))
    assert r.end() is None
    r.stop()
    assert script.calls == 0


# -- GameStateAnalyzer.run_ocr: the read on the OCR pool's own reader -------------

def _run_ocr(monkeypatch, executor, reader="R"):
    from types import SimpleNamespace
    import wingman.analyzer as analyzer_module
    monkeypatch.setattr(analyzer_module, "_get_thread_ocr_reader", lambda: reader)
    host = SimpleNamespace(ocr_executor=executor)
    return lambda fn, *a, **k: analyzer_module.GameStateAnalyzer.run_ocr(host, fn, *a, **k)


def test_run_ocr_hands_the_pool_threads_reader_to_the_read(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        run = _run_ocr(monkeypatch, pool)
        assert run(lambda reader, frame: (reader, frame), "F") == ("R", "F")
    finally:
        pool.shutdown(wait=True)


@pytest.mark.parametrize("case", ["no pool", "no reader", "raises"])
def test_run_ocr_answers_none_when_it_cannot_read(monkeypatch, case):
    from concurrent.futures import ThreadPoolExecutor
    pool = None if case == "no pool" else ThreadPoolExecutor(max_workers=1)
    try:
        run = _run_ocr(monkeypatch, pool, reader=None if case == "no reader" else "R")

        def read(_reader, _frame):
            raise RuntimeError("OCR failed")

        assert run(read if case == "raises" else (lambda r, f: "x"), "F") is None
    finally:
        if pool is not None:
            pool.shutdown(wait=True)

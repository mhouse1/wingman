"""ADR 166: the squadron leaderboard, read in the lobby between rounds."""

import logging
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
import yaml

from wingman import squadron_ranking as sr
from wingman.crop_region import CropCoords
from wingman.squadron_ranking import (Navigation, RowRead, SquadronRanking, SquadronReading,
                                      parse_count, read_row)

MAIN_SRC = Path("wingman/main.py").read_text(encoding="utf-8")
ANALYZER_SRC = Path("wingman/analyzer.py").read_text(encoding="utf-8")


# -- parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("659", 659), ("358,032", 358032), ("358,,032", 358032), ("358.032", 358032),
    ("17,366", 17366), ("1,295,121", 1295121), ("0", 0), ("6O9", 609),
    ("358,32", None),       # a group of two: a box lost
    ("35,8,032", None),     # a stray box between the groups
    ("659,", None), (",659", None), ("", None), ("30/30", None)])
def test_a_count_is_a_number_with_whole_groups(text, expected):
    assert parse_count(text) == expected


# -- one frame's read, on a stand-in reader ----------------------------------------

def _crops():
    """Each crop names itself in its text field, so a stand-in can tell them apart."""
    return {name: CropCoords(0.0, 0.0, 1.0, 1.0, [name]) for name in sr.CROPS}


class _Reader:
    """EasyOCR stand-in: `answers` lists the boxes each successive read returns,
    each (text, left edge)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.allowlists = []

    def readtext(self, img, detail=1, paragraph=False, workers=0, allowlist=None):
        self.allowlists.append(allowlist)
        boxes = self.answers.pop(0)
        return [([[x, 0], [x + 50, 0], [x + 50, 20], [x, 20]], text, 0.9) for text, x in boxes]


_FRAME = np.zeros((1200, 1920, 3), np.uint8)


def test_the_squadrons_tab_reads_our_row():
    reader = _Reader([("CURRENT TOP SQUADRONS", 0), ("MEMBERS", 400)], [("Mobius Squadron", 0)],
                     [("659", 0)], [("032", 60), ("358,", 0)])
    row = read_row(reader, _FRAME, _crops(), "Mobius Squadron")
    assert row == RowRead(SquadronReading(659, 358032),
                          "CURRENT TOP SQUADRONS MEMBERS", "Mobius Squadron", "659", "358,,032")
    assert reader.allowlists[2:] == ["0123456789,.", "0123456789,."], "digits only for the counts"


def test_the_first_tab_pins_our_player_in_the_same_place_and_does_not_read():
    """TOTAL TROPHIES: our own player's row (17,366, 20,094) sits where the
    squadron's will, with the squadron's name under the player's."""
    reader = _Reader([("CURRENT TOP PLAYERS", 0)])
    assert read_row(reader, _FRAME, _crops(), "Mobius Squadron") == RowRead(
        None, "CURRENT TOP PLAYERS")


def test_another_squadrons_row_does_not_read():
    reader = _Reader([("CURRENT TOP SQUADRONS", 0)], [("Korea Top Gun", 0)])
    assert read_row(reader, _FRAME, _crops(), "Mobius Squadron").reading is None


def test_a_rank_of_zero_is_no_reading():
    reader = _Reader([("CURRENT TOP SQUADRONS", 0)], [("Mobius Squadron", 0)],
                     [("0", 0)], [("358,032", 0)])
    assert read_row(reader, _FRAME, _crops(), "Mobius Squadron").reading is None


# -- the navigation, against a model of the game's screens --------------------------

class _Game:
    """The game's screens and what each click does to them. `ignore` lists
    clicks the game drops; `slow` holds a screen back for that many frames."""

    # (screen, crop clicked) -> next screen
    MOVES = {("lobby", sr.MENU): "menu",
             ("menu", sr.MENU_LEADERBOARDS): "players",
             ("menu", sr.MENU): "lobby",
             ("players", sr.TAB_SQUADRONS): "squadrons",
             ("players", sr.BACK): "lobby",
             ("squadrons", sr.BACK): "lobby"}
    # What each screen shows, crop by crop.
    SHOWS = {"lobby": {sr.PLAY: "PLAY"},
             "menu": {sr.PLAY: "PLAY", sr.MENU_LEADERBOARDS: "LEADERBOARDS"},
             "players": {sr.BOARD_TITLE: "LEADERBOARDS"},
             "squadrons": {sr.BOARD_TITLE: "LEADERBOARDS"},
             "other": {}}

    def __init__(self, screen="lobby", ignore=(), slow=None, row=SquadronReading(659, 358032),
                 tab_lag=0):
        self.screen = screen
        self.ignore = set(ignore)
        self.slow = dict(slow or {})
        self.row = row
        self.tab_lag = tab_lag      # frames the old tab's rows stay up after the click
        self.clicks = []
        self.pending = None

    def click(self, name):
        self.clicks.append(name)
        if name in self.ignore:
            return
        nxt = self.MOVES.get((self.screen, name))
        if nxt is None:
            return
        if self.slow.get(nxt):
            self.pending = [nxt, self.slow[nxt]]
        else:
            self.screen = nxt

    def grab(self):
        if self.pending is not None:
            self.pending[1] -= 1
            if self.pending[1] < 0:
                self.screen, self.pending = self.pending[0], None
        return self.screen

    def ocr(self, fn, frame, *args):
        if fn is sr.shows:
            crop, word = args
            return word in self.SHOWS[frame].get(crop.text[0], "")
        if fn is sr.read_row:
            if frame == "squadrons" and self.tab_lag <= 0:
                return RowRead(self.row, "CURRENT TOP SQUADRONS")
            self.tab_lag -= 1
            return RowRead(None, "CURRENT TOP PLAYERS")
        raise AssertionError(fn)


def _navigate(game, stop=lambda: False):
    nav = Navigation(_crops(), game.grab, game.ocr, game.click, stop, "Mobius Squadron",
                     step_timeout_s=2.0, sleep_fn=lambda s: None, clock=_Clock())
    return nav.run(), nav


class _Clock:
    """Each call is one poll later: 0.4 s, as POLL_S."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += sr.POLL_S
        return self.t


def test_the_leaderboard_is_read_and_left_by_its_own_back_arrow():
    game = _Game()
    (reading, why), _nav = _navigate(game)
    assert (reading, why) == (SquadronReading(659, 358032), "read")
    assert game.clicks == [sr.MENU, sr.MENU_LEADERBOARDS, sr.TAB_SQUADRONS, sr.BACK]
    assert game.screen == "lobby"


def test_a_slow_page_is_waited_for():
    game = _Game(slow={"players": 3})
    (reading, _why), _nav = _navigate(game)
    assert reading == SquadronReading(659, 358032)


def test_the_first_tabs_rows_left_up_after_the_click_are_not_read():
    game = _Game(tab_lag=2)
    (reading, _why), nav = _navigate(game)
    assert reading == SquadronReading(659, 358032)
    assert [row.reading for _f, row in nav.reads][:2] == [None, None]


@pytest.mark.parametrize("screen", ["menu", "other", "players"])
def test_nothing_is_clicked_away_from_the_lobby(screen):
    """The open menu leaves PLAY in view: the lobby check must not take it."""
    game = _Game(screen=screen)
    (reading, why), _nav = _navigate(game)
    assert (reading, why) == (None, "the lobby is not on screen")
    assert game.clicks == []


def test_a_menu_that_does_not_open_is_reported_from_the_lobby():
    game = _Game(ignore={sr.MENU})
    (reading, why), _nav = _navigate(game)
    assert (reading, why) == (None, "the menu did not open")
    assert game.screen == "lobby"


def test_an_open_menu_is_shut_by_its_own_button():
    game = _Game(ignore={sr.MENU_LEADERBOARDS})
    (reading, why), _nav = _navigate(game)
    assert (reading, why) == (None, "the leaderboards did not open")
    assert game.clicks[-1] == sr.MENU and game.screen == "lobby"


def test_rows_that_never_read_alike_are_reported_and_left():
    game = _Game(row=None)
    (reading, why), nav = _navigate(game)
    assert reading is None
    assert why == f"no two of {sr.MAX_READS} frames read alike"
    assert len(nav.reads) == sr.MAX_READS and game.screen == "lobby"


def test_a_back_arrow_that_does_nothing_is_reported():
    game = _Game(ignore={sr.BACK})
    (reading, why), _nav = _navigate(game)
    assert reading == SquadronReading(659, 358032)
    assert why == "read; NOT back in the lobby"
    assert game.clicks.count(sr.BACK) == 3


def test_a_stop_ends_the_navigation_at_once():
    game = _Game()
    stop = threading.Event()
    original = game.click

    def click(name):
        original(name)
        if name == sr.MENU_LEADERBOARDS:
            stop.set()
    game.click = click
    (reading, why), _nav = _navigate(game, stop.is_set)
    assert (reading, why) == (None, "stopped: wingman is leaving")
    assert game.clicks == [sr.MENU, sr.MENU_LEADERBOARDS]


def test_the_navigation_has_no_way_to_press_escape():
    """ESC on the lobby opens Exit to Desktop: the navigation is given clicks only."""
    src = Path("wingman/squadron_ranking.py").read_text(encoding="utf-8")
    assert "press_escape" not in src and "press(" not in src and "keyboard" not in src


# -- the schedule ------------------------------------------------------------------

class _Steps:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_a_reading_is_due_at_the_first_lobby_and_every_period_after_an_attempt():
    clock = _Steps()
    schedule = SquadronRanking(True, 3600.0, clock)
    assert schedule.due() and schedule.holds_play()
    done = threading.Event()

    class _Nav:
        def run(self):
            assert schedule.active() and not schedule.due() and schedule.holds_play()
            return None, "the lobby is not on screen"
    schedule.start(_Nav(), lambda *a: done.set()).join(5)
    assert done.is_set() and not schedule.active()
    assert not schedule.due(), "a navigation that gave up counts as the attempt"
    schedule.lobby_left()
    clock.t = 3599.0
    assert not schedule.due()
    clock.t = 3600.0
    assert schedule.due()


def test_every_lobby_means_once_a_lobby_not_back_to_back():
    """2026-10-31 02:17: with every_min 0 the leaderboard was read at 02:17:50 and
    opened again at 02:17:55, in the same lobby, and no round would have started."""
    schedule = SquadronRanking(True, 0.0, _Steps())

    class _Nav:
        def run(self):
            return SquadronReading(659, 358046), "read"
    schedule.start(_Nav(), lambda *a: None).join(5)
    assert not schedule.due() and not schedule.holds_play(), "PLAY goes ahead after a reading"
    schedule.lobby_left()
    assert schedule.due()


def test_play_stays_held_from_due_through_the_start_of_the_navigation():
    """2026-10-31 02:17:56: the quick-scan clicked PLAY while a navigation was
    starting. The hold must not drop between due and active."""
    seen = []

    class _Clock:
        """The attempt's time is taken only once the navigation counts as active."""

        def __call__(self):
            seen.append(schedule.active())
            return 0.0
    schedule = SquadronRanking(True, 3600.0, _Clock())
    release = threading.Event()

    class _Nav:
        def run(self):
            release.wait(5)
            return None, "x"
    assert schedule.holds_play()
    thread = schedule.start(_Nav(), lambda *a: None)
    assert schedule.holds_play()
    release.set()
    thread.join(5)
    assert seen and all(seen), "active before the attempt is booked"


def test_off_is_never_due():
    schedule = SquadronRanking(False, 0.0, _Steps())
    assert not schedule.due() and not schedule.holds_play()


def test_a_navigation_that_raises_still_ends_and_reports():
    schedule = SquadronRanking(True, 60.0, _Steps())
    answers = []

    class _Nav:
        def run(self):
            raise RuntimeError("boom")
    schedule.start(_Nav(), lambda reading, why, nav: answers.append((reading, why))).join(5)
    assert answers == [(None, "the navigation failed")] and not schedule.active()


# -- the analyzer's holds ----------------------------------------------------------

class _FakeAnalyzer:
    def __init__(self):
        from wingman.analyzer import GameStateAnalyzer
        self._lobby_errand_active = None
        self._lobby_errand_pending = None
        self._play_seen_ts = 0.0
        for name in ("set_lobby_errand", "lobby_errand_active", "play_seen_within"):
            setattr(self, name, getattr(GameStateAnalyzer, name).__get__(self))
        self._ask = GameStateAnalyzer._ask


def test_the_errand_predicates_fail_open():
    a = _FakeAnalyzer()
    assert a.lobby_errand_active() is False

    def boom():
        raise RuntimeError("predicate exploded")
    a.set_lobby_errand(boom, boom)
    assert a.lobby_errand_active() is False
    a.set_lobby_errand(lambda: True, lambda: False)
    assert a.lobby_errand_active() is True


def test_the_quick_scan_holds_its_whole_lobby_cycle_while_the_errand_runs():
    """No click, dismissal or stall count while wingman is on the leaderboard: the
    hold sits before the lobby crops are even submitted."""
    hold = ANALYZER_SRC.index("if state == GameState.GAME_LOBBY and self.lobby_errand_active():")
    assert hold < ANALYZER_SRC.index("lobby_futures[crop] = executor.submit(", hold - 20000)
    branch = ANALYZER_SRC[hold:ANALYZER_SRC.index("continue", hold)]
    assert "lobby_stall_since = 0.0" in branch


def test_a_due_reading_holds_play_but_not_ready():
    guard = ANALYZER_SRC.index('elif crop == "PLAY" and self._ask(self._lobby_errand_pending,')
    branch = ANALYZER_SRC[guard:ANALYZER_SRC.index("\n                        elif", guard + 10)]
    assert "handled = True" in branch
    assert "LOBBY_PLAY_CLICK" not in branch and "play_clicked" not in branch


def test_play_and_ready_are_not_clicked_once_a_navigation_runs():
    """The hold at the top of the cycle is not enough: a navigation can start
    while the cycle's OCR is out (2026-10-31 02:17:56)."""
    guard = ANALYZER_SRC.index("                        elif self.lobby_errand_active():")
    click = ANALYZER_SRC.index("self.emit(GameEvent.LOBBY_PLAY_CLICK, crop, frame)", guard)
    branch = ANALYZER_SRC[guard:ANALYZER_SRC.index("\n                        elif", guard + 10)]
    assert guard < click and "handled = True" in branch


def test_main_holds_play_while_due_or_running_and_resets_each_lobby():
    assert "pending=lambda: (squadron.holds_play() and" in MAIN_SRC
    assert "squadron.lobby_left()" in MAIN_SRC


def test_the_escape_loop_and_the_deferred_exit_wait_for_the_errand():
    loop = MAIN_SRC.index("def _lobby_escape_loop(")
    assert MAIN_SRC.index("if squadron.active():", loop) < MAIN_SRC.index(
        "ctrl.press_escape(", loop)
    assert "or _settling or squadron.active()):" in MAIN_SRC
    assert "and not squadron.active())" in MAIN_SRC          # the guards' safe point


# -- booking a navigation's answer -------------------------------------------------

class _Nav:
    def __init__(self, reads, last_frame="last"):
        self.reads = reads
        self.last_frame = last_frame


def test_a_reading_is_logged_booked_and_its_frame_kept(caplog):
    from wingman.main import _note_squadron_ranking
    booked, saved = [], []
    nav = _Nav([("f1", RowRead(SquadronReading(659, 358032), "h")),
                ("f2", RowRead(SquadronReading(659, 358032), "h"))])
    with caplog.at_level(logging.INFO):
        _note_squadron_ranking(SquadronReading(659, 358032), "read", nav, logging.getLogger("t"),
                               lambda r, ts: booked.append(r),
                               lambda f, n: saved.append((f, n)), stamp="S")
    assert "SQUADRON RANKING: rank 659, trophies 358,032" in caplog.text
    assert booked == [SquadronReading(659, 358032)]
    assert saved == [("f2", "S_read-659-358032")]


def test_a_navigation_that_gave_up_says_why_and_keeps_its_last_frame(caplog):
    from wingman.main import _note_squadron_ranking
    booked, saved = [], []
    with caplog.at_level(logging.INFO):
        _note_squadron_ranking(None, "the menu did not open; NOT back in the lobby", _Nav([]),
                               logging.getLogger("t"), lambda r, ts: booked.append(r),
                               lambda f, n: saved.append((f, n)), stamp="S")
    assert "SQUADRON RANKING: not read — the menu did not open" in caplog.text
    assert "not back in the lobby after the leaderboard" in caplog.text
    assert booked == [] and saved == [("last", "S_unread")]


def test_the_stats_file_keeps_each_reading_and_the_summary_the_last(tmp_path):
    from wingman.mission_stats import MissionStatsTracker, squadron_ranking_lines
    tracker = MissionStatsTracker(version="t", output_dir=str(tmp_path))
    tracker.record_squadron_ranking(SquadronReading(660, 357000), 100.0)
    tracker.record_squadron_ranking(SquadronReading(659, 358032), 200.0)
    summary = tracker.finalize(run_id="20261031_020000_acct1")
    assert summary["squadron_rankings"] == [{"ts": 100.0, "rank": 660, "trophies": 357000},
                                            {"ts": 200.0, "rank": 659, "trophies": 358032}]
    assert squadron_ranking_lines(summary["squadron_rankings"]) == [
        "Squadron ranking  : rank 659, trophies 358,032  (last of 2 reading(s); ADR 166)"]
    assert squadron_ranking_lines([]) == []


# -- the shipped config --------------------------------------------------------------

def test_every_crop_the_navigation_uses_is_calibrated_and_it_ships_off():
    cfg = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))
    assert set(sr.CROPS) <= set(cfg["crops"])
    assert cfg["squadron_ranking"]["enabled"] is False


# -- real OCR on the operator's screenshots (2026-10-31) -----------------------------

_CORPUS = Path(__file__).parent / "fixtures" / "squadron_ranking"


def _config_crops():
    crops = yaml.safe_load(Path("wingman/config.yaml").read_text(encoding="utf-8"))["crops"]
    return {name: CropCoords(*crops[name]["coords"][0], *crops[name]["coords"][1])
            for name in sr.CROPS}


def _load(name, size=(1200, 1920)):
    path = _CORPUS / name
    if not path.exists():
        pytest.skip(f"{path} not present")
    frame = cv2.imread(str(path))
    h, w = size
    return frame if w == frame.shape[1] else cv2.resize(frame, (w, h), interpolation=cv2.INTER_AREA)


@pytest.fixture(scope="module")
def reader():
    easyocr = pytest.importorskip("easyocr")
    return easyocr.Reader(["en"], gpu=False, verbose=False)


# Enumerated, not globbed: each screen and what the navigation must make of it.
_SCREENS = [
    # (file, in the lobby, the menu open, the leaderboard up, the reading)
    ("lobby.png", True, False, False, None),
    ("menu.png", False, True, False, None),
    ("total_trophies.png", False, False, True, None),
    ("squadrons_659_358032.png", False, False, True, SquadronReading(659, 358032)),
]


@pytest.mark.slow
@pytest.mark.parametrize("name, lobby, menu, board, reading", _SCREENS)
def test_the_screens_read_as_labelled(reader, name, lobby, menu, board, reading):
    crops, frame = _config_crops(), _load(name)
    on = {crop: sr.shows(reader, frame, crops[crop], word) for crop, word in (
        (sr.PLAY, "PLAY"), (sr.MENU_LEADERBOARDS, "LEADERBOARDS"),
        (sr.BOARD_TITLE, "LEADERBOARDS"))}
    assert (on[sr.PLAY] and not on[sr.MENU_LEADERBOARDS]) == lobby
    assert on[sr.MENU_LEADERBOARDS] == menu
    assert on[sr.BOARD_TITLE] == board
    assert read_row(reader, frame, crops, "Mobius Squadron").reading == reading


@pytest.mark.slow
def test_a_half_size_frame_reads_too(reader):
    frame = _load("squadrons_659_358032.png", (600, 960))
    assert read_row(reader, frame, _config_crops(), "Mobius Squadron").reading == \
        SquadronReading(659, 358032)

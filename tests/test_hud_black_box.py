"""Code Review 019 (CR-019-09): the HUD's black box.

The last seconds of annotated frames are held in memory and written at a
death, so a crash can be watched as wingman saw it. Checked on the real
HudRenderer; the wiring into main is checked from its source, as the loom's is.
"""

import inspect
import time

import numpy as np
import pytest

from wingman import main as main_module
from wingman.hud import HudRenderer

_FRAME = np.zeros((300, 400, 3), np.uint8)


def _box(tmp_path, **kw):
    settings = dict(black_box_enabled=True, black_box_dir=str(tmp_path / "box"),
                    black_box_seconds=18.0, black_box_rate_hz=10.0)
    settings.update(kw)
    return HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=60.0, **settings)


def _render(renderer, n, gap=0.11):
    for _ in range(n):
        thread = renderer.maybe_render(_FRAME, None, "PURSUIT_MODE", 100, 2, 4)
        if thread is not None:
            thread.join(timeout=5)
        time.sleep(gap)


def test_frames_are_kept_and_written_at_a_death(tmp_path):
    renderer = _box(tmp_path)
    _render(renderer, 4)
    assert len(renderer._bb_frames) == 4
    thread = renderer.dump_black_box("death")
    thread.join(timeout=5)
    files = sorted((tmp_path / "box").glob("*_death_*.jpg"))
    assert len(files) == 4
    assert [f.name.split("_")[3] for f in files] == ["00", "01", "02", "03"]   # oldest first
    assert renderer._bb_frames == type(renderer._bb_frames)()                  # started again


def test_live_hud_png_is_still_written_once_a_second_only(tmp_path):
    """The black box renders more often; live_hud.png keeps its own cadence."""
    renderer = _box(tmp_path)
    _render(renderer, 3)
    first = (tmp_path / "live_hud.png").stat().st_mtime_ns
    _render(renderer, 3)
    assert (tmp_path / "live_hud.png").stat().st_mtime_ns == first
    assert len(renderer._bb_frames) == 6


def test_frames_older_than_the_span_are_let_go(tmp_path):
    renderer = _box(tmp_path, black_box_seconds=5.0)
    image = np.zeros((150, 200, 3), np.uint8)
    for ts in (100.0, 102.0, 104.0, 106.0, 108.0):
        renderer._keep_for_black_box(image, ts)
    assert [ts for ts, _ in renderer._bb_frames] == [104.0, 106.0, 108.0]


def test_nothing_held_writes_nothing(tmp_path):
    renderer = _box(tmp_path)
    assert renderer.dump_black_box("death") is None
    assert not (tmp_path / "box").exists()


def test_off_unless_configured(tmp_path):
    renderer = HudRenderer(str(tmp_path / "live_hud.png"), interval_sec=0.0)
    _render(renderer, 2, gap=0.0)
    assert not renderer._bb_frames
    assert renderer.dump_black_box("death") is None


@pytest.mark.parametrize("live, enabled", [(True, True), (False, False)])
def test_the_replay_and_capture_lanes_keep_it_off(tmp_path, live, enabled):
    cfg = {"hud": {"enabled": True, "output_path": str(tmp_path / "live_hud.png"),
                   "black_box": {"enabled": True, "dir": str(tmp_path / "box")}}}
    renderer = HudRenderer.from_config(cfg, live=live)
    assert renderer._bb_enabled is enabled


def test_main_writes_the_black_box_at_each_death():
    src = inspect.getsource(main_module.main)
    assert "HudRenderer.from_config(cfg, live=not replay_mode and not capture_mode)" in src
    hook = src.index('if event_name == "respawn_detected" and hud_renderer is not None:')
    assert src.index('hud_renderer.dump_black_box("death")') > hook


# ---------------------------------------------------------------------------
# Cycle 5: a clip round each planner alarm
# ---------------------------------------------------------------------------

def _boxed(tmp_path, **kw):
    renderer = _box(tmp_path, black_box_alarm_max=kw.pop("alarm_max", 5),
                    black_box_alarm_gap_s=kw.pop("alarm_gap_s", 30.0),
                    black_box_alarm_seconds=kw.pop("alarm_seconds", 2.0), **kw)
    image = np.zeros((150, 200, 3), np.uint8)
    for ts in (100.0, 101.0, 102.0, 103.0, 104.0):
        renderer._keep_for_black_box(image, ts)
    return renderer


def test_an_alarm_clip_keeps_the_last_seconds_and_leaves_the_box_whole(tmp_path):
    renderer = _boxed(tmp_path)
    timer = renderer.note_alarm("escape-resupply", delay_s=0.0)
    timer.join(timeout=5)
    deadline = time.time() + 5
    files = []
    while not files and time.time() < deadline:
        files = sorted((tmp_path / "box" / "alarms").glob("*_escape-resupply_*.jpg"))
        time.sleep(0.02)
    time.sleep(0.2)
    files = sorted((tmp_path / "box" / "alarms").glob("*_escape-resupply_*.jpg"))
    assert len(files) == 3                       # 102, 103 and 104: the last 2 s
    assert len(renderer._bb_frames) == 5         # a death a moment later finds it whole
    assert not list((tmp_path / "box").glob("*.jpg"))   # not in the deaths' folder


def test_alarm_clips_are_spaced_and_capped(tmp_path):
    renderer = _boxed(tmp_path, alarm_max=2, alarm_gap_s=0.0)
    first = renderer.note_alarm("detour-resupply", delay_s=60.0)
    second = renderer.note_alarm("detour-resupply", delay_s=60.0)
    third = renderer.note_alarm("detour-resupply", delay_s=60.0)
    assert first is not None and second is not None and third is None
    for timer in (first, second):
        timer.cancel()
    spaced = _boxed(tmp_path, alarm_max=5, alarm_gap_s=30.0)
    first = spaced.note_alarm("escape-target", delay_s=60.0)
    assert first is not None and spaced.note_alarm("escape-target", delay_s=60.0) is None
    first.cancel()


def test_alarm_clips_are_off_at_zero(tmp_path):
    renderer = _boxed(tmp_path, alarm_max=0)
    assert renderer.note_alarm("escape-target", delay_s=0.0) is None


# ---------------------------------------------------------------------------
# Research 017 step 3: the game's own picture beside each annotated frame
# ---------------------------------------------------------------------------
#
# The annotated frames carry the planner's free-air fill across the analysed
# box, so a picture-based terrain warning cannot be studied on them. With
# `raw`, each death also leaves the game's own picture, same size, same moments,
# same names, in <dir>/raw: labelled pictures from before every crash, in every
# session, with no video to line up.

_GREY = np.full((300, 400, 3), 128, np.uint8)


def _render_grey(renderer, n, gap=0.11):
    for _ in range(n):
        thread = renderer.maybe_render(_GREY, None, "PURSUIT_MODE", 100, 2, 4)
        if thread is not None:
            thread.join(timeout=5)
        time.sleep(gap)


def test_the_raw_picture_is_kept_beside_each_annotated_frame(tmp_path):
    import cv2
    renderer = _box(tmp_path, black_box_raw=True)
    _render_grey(renderer, 3)
    renderer.dump_black_box("death").join(timeout=5)
    annotated = sorted(p.name for p in (tmp_path / "box").glob("*_death_*.jpg"))
    raw = sorted(p.name for p in (tmp_path / "box" / "raw").glob("*_death_*.jpg"))
    assert len(annotated) == 3 and raw == annotated          # same names, same moments
    picture = cv2.imread(str(tmp_path / "box" / "raw" / raw[0]))
    marked = cv2.imread(str(tmp_path / "box" / annotated[0]))
    assert picture.shape == marked.shape == (150, 200, 3)    # the annotated frame's size
    assert np.abs(picture.astype(int) - 128).max() <= 6      # the picture, untouched
    assert np.abs(marked.astype(int) - 128).max() > 60       # the HUD's text and panels
    assert not renderer._bb_raw                               # started again


def test_raw_is_off_unless_configured(tmp_path):
    renderer = _box(tmp_path)
    _render_grey(renderer, 2)
    renderer.dump_black_box("death").join(timeout=5)
    assert list((tmp_path / "box").glob("*_death_*.jpg"))
    assert not (tmp_path / "box" / "raw").exists()


def test_an_alarm_clip_keeps_its_raw_pictures_and_leaves_the_box_whole(tmp_path):
    renderer = _box(tmp_path, black_box_raw=True)
    image = np.zeros((150, 200, 3), np.uint8)
    for ts in (100.0, 101.0, 102.0, 103.0, 104.0):
        renderer._keep_for_black_box(image, ts)
        renderer._keep_for_black_box(image, ts, raw=True)
    renderer.dump_black_box("escape-target", last_s=2.0, clear=False,
                            directory=tmp_path / "box" / "alarms").join(timeout=5)
    assert len(list((tmp_path / "box" / "alarms" / "raw").glob("*.jpg"))) == 3
    assert len(renderer._bb_raw) == 5


@pytest.mark.parametrize("box, expected", [({"raw": True}, True), ({}, False)])
def test_raw_comes_from_the_config(tmp_path, box, expected):
    cfg = {"hud": {"enabled": True, "output_path": str(tmp_path / "live_hud.png"),
                   "black_box": {"enabled": True, "dir": str(tmp_path / "box"), **box}}}
    assert HudRenderer.from_config(cfg, live=True)._bb_raw_enabled is expected


def test_the_shipped_config_keeps_raw_pictures_with_a_budget_of_their_own():
    import yaml
    from constants import CONFIG_PATH
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    box = cfg["hud"]["black_box"]
    assert box["raw"] is True
    assert f"{box['dir']}/raw" in cfg["capture_budget"]["dirs"]

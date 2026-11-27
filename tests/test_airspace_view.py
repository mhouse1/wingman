"""Design 017: the live airspace view's server (scripts/airspace-view.py): the log reader, the
view's state, the log tail across a rotation, and the file guard."""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from wingman.airspace import Sample, build_record

_SPEC = importlib.util.spec_from_file_location("airspace_view", Path("scripts/airspace-view.py"))
av = importlib.util.module_from_spec(_SPEC)
sys.modules["airspace_view"] = av
_SPEC.loader.exec_module(av)

ARENAS = {"CRIMSON CANYON": "crimson-canyon", "CRIMSON CANYON SUNRISE": "crimson-canyon"}


def _line(sec, text, level="DEBUG", ms=0):
    return f"2026-11-26 12:{sec // 60:02d}:{sec % 60:02d},{ms:03d} [{level}] {text}\n"


def _padlock(sec, alt, kph=543, ms=0):
    return _line(sec, f"\x1b[93mPADLOCK: OFF | Altitude: {alt} | Speed: {kph} | Nose: +2\N{DEGREE SIGN} (level)\x1b[0m",
                 "INFO", ms)


def _terrain(sec, fix="-2097,5966", map_name="CRIMSON CANYON", extra="", ms=30):
    tail = (" score=0.92/0.37 ground=1010 speed=135(fix 107) ttc_y=clear ttc_m=clear"
            if not fix.startswith("none") else "")
    return _line(sec, f"TERRAIN: map={map_name} fix={fix}{tail} arm=map{extra} act=- ms=27", ms=ms)


def _feed(parser, lines):
    out = []
    for line in lines:
        out += parser.feed(line)
    return out


def test_a_tick_carries_the_latest_altitude_speed_and_heading():
    p = av.LineParser(ARENAS)
    events = _feed(p, [
        _line(0, "BT[active]: selected=Climb missiles=4 alt=2369.3 alt_rate=+133m/s hdg=163"),
        _padlock(1, 2710, 543),
        _terrain(1),
    ])
    match, tick = events
    assert match == {"type": "match", "t": match["t"], "map": "CRIMSON CANYON", "mode": None, "arena": "crimson-canyon"}
    assert tick["fix"] and (tick["x"], tick["z"]) == (-2097.0, 5966.0)
    assert (tick["alt"], tick["kph"], tick["hdg"], tick["ground"]) == (2710.0, 543.0, 163.0, 1010.0)
    assert tick["speed"] == 135.0 and tick["ttc_y"] == "clear" and not tick["warn"]


def test_a_reading_the_filter_rejected_never_reaches_the_jet():
    """2026-11-26 12:50 to 13:00: four rejected readings at 13,877 to 23,824 m drew the trail
    straight up into the sky."""
    p = av.LineParser(ARENAS)
    events = _feed(p, [
        _padlock(1, 2710),
        _line(2, "Analyzer: telemetry plausibility filter rejected reading "
                 "(speed_raw=850 altitude_raw=18104, total_rejected=4)", "WARNING"),
        _padlock(2, 18104, 850),
        _terrain(2),
    ])
    tick = events[-1]
    assert tick["alt"] == 2710.0 and tick["kph"] == 543.0


def test_a_lost_fix_is_a_tick_without_a_position_and_a_warning_is_flagged():
    p = av.LineParser(ARENAS)
    lost = _feed(p, [_terrain(1, fix="none(weak-match)")])[-1]
    assert lost["fix"] is False and lost["reason"] == "weak-match" and "x" not in lost
    warned = _feed(p, [_terrain(2, extra=" WARN")])[-1]
    assert warned["warn"]


def test_match_states_deaths_and_cards():
    p = av.LineParser(ARENAS)
    events = _feed(p, [
        _line(0, "MATCH: mode=PRIORITY TARGET map=CRIMSON CANYON SUNRISE", "INFO"),
        _line(1, "\x1b[96m\N{VIDEO GAME} Game state: GAME_BATTLE_EJECT \N{RIGHTWARDS ARROW} GAME_BATTLE\x1b[0m", "INFO"),
        _line(9, "RespawnLatency: OCR edge \N{EM DASH} since_confirmed=8.28s no_digits_for=4.78s", "INFO"),
        _line(11, "DEATH CARD: no kill card on 1 read(s) of the respawn screen: a crash or self-inflicted", "INFO"),
        _line(12, "DEATH CARDS: 3 deaths: enemy 2, no card 1, unread 0", "INFO"),
    ])
    kinds = [e["type"] for e in events]
    assert kinds == ["match", "state", "death", "card"]
    assert events[0]["arena"] == "crimson-canyon" and events[0]["mode"] == "PRIORITY TARGET"
    assert events[1]["state"] == "GAME_BATTLE"
    assert events[2]["t"] == pytest.approx(av._epoch(_line(9, "")) - 4.78)
    assert events[3]["crash"] is True
    # the TERRAIN line names the same map: no second match
    assert [e["type"] for e in _feed(p, [_terrain(13, map_name="CRIMSON CANYON SUNRISE")])] == ["tick"]


def test_hub_keeps_the_trail_of_this_life_only():
    hub = av.Hub("live", "wingman.log")
    p = av.LineParser(ARENAS)
    for ev in _feed(p, [_padlock(1, 900), _terrain(1, "0,0"), _padlock(3, 950), _terrain(3, "300,0")]):
        hub.publish(ev)
    snap = hub.copy()
    assert snap["arena"] == "crimson-canyon"
    assert [q[:3] for q in snap["trail"]] == [[0.0, 0.0, 900.0], [300.0, 0.0, 950.0]]
    hub.publish({"type": "death", "t": 4.0})
    snap = hub.copy()
    assert snap["trail"] == [] and snap["deaths"] == [[300.0, 0.0, 950.0]]
    hub.publish({"type": "match", "t": 9.0, "map": "X", "mode": None, "arena": None})
    assert hub.copy()["deaths"] == []


def test_subscribers_get_each_event_after_the_snapshot():
    hub = av.Hub("live", "wingman.log")
    q, snap = hub.subscribe()
    assert snap["trail"] == []
    hub.publish({"type": "state", "t": 1.0, "state": "GAME_BATTLE"})
    assert q.get_nowait()["state"] == "GAME_BATTLE"
    hub.unsubscribe(q)
    hub.publish({"type": "state", "t": 2.0, "state": "GAME_LOBBY"})
    assert q.empty()


def test_the_record_floor_rides_on_the_tick(tmp_path):
    rec, _ = build_record([Sample(0.0, 100.0, 100.0, 500.0, life=1), Sample(9.0, 100.0, 100.0, 560.0, life=2)])
    path = tmp_path / "crimson-canyon-airspace.npz"
    rec.save(str(path))
    lookup = av.RecordLookup({"crimson-canyon": str(path)})
    tick = lookup.annotate({"type": "tick", "fix": True, "arena": "crimson-canyon", "x": 100.0, "z": 100.0})
    assert (tick["floor"], tick["floor2"], tick["passes"]) == (500.0, 560.0, 2)
    lost = lookup.annotate({"type": "tick", "fix": False, "arena": "crimson-canyon"})
    assert "floor" not in lost
    none = av.RecordLookup({}).annotate({"type": "tick", "fix": True, "arena": "x", "x": 0.0, "z": 0.0})
    assert "floor" not in none


def test_log_tail_returns_whole_lines_and_follows_a_rotation(tmp_path):
    log = tmp_path / "wingman.log"
    log.write_text("one\ntw")
    tail = av.LogTail(str(log))
    assert tail.poll() == ["one\n"]
    with open(log, "a") as fh:
        fh.write("o\nthree\n")
    assert tail.poll() == ["two\n", "three\n"]
    assert tail.poll() == []
    # wingman renames the old log away and starts a new one at the same path
    os.rename(log, tmp_path / "wingman_old.log")
    log.write_text("fresh\n")
    assert tail.poll() == ["fresh\n"]


def test_log_tail_waits_for_a_log_that_is_not_there_yet(tmp_path):
    tail = av.LogTail(str(tmp_path / "wingman.log"))
    assert tail.poll() == []
    (tmp_path / "wingman.log").write_text("first\n")
    assert tail.poll() == ["first\n"]


def test_safe_join_refuses_paths_outside_the_root(tmp_path):
    (tmp_path / "three").mkdir()
    (tmp_path / "three" / "a.js").write_text("x")
    root = str(tmp_path / "three")
    assert av.safe_join(root, "a.js") == os.path.realpath(tmp_path / "three" / "a.js")
    assert av.safe_join(root, "../secret") is None
    assert av.safe_join(root, "/../../etc/passwd") is None


def test_arena_files_name_a_scene_and_a_record_beside_each_height_map():
    arena_of, glb_of, rec_of = av.arena_files("wingman/config.yaml")
    arena = arena_of["CRIMSON CANYON"]
    assert glb_of[arena].endswith("crimson-canyon-day/crimson-canyon-airspace.glb")
    assert rec_of[arena].endswith("crimson-canyon-day/crimson-canyon-airspace.npz")


def test_a_rebuilt_record_is_picked_up_without_a_restart(tmp_path):
    path = tmp_path / "crimson-canyon-airspace.npz"
    rec, _ = build_record([Sample(0.0, 100.0, 100.0, 500.0, life=1)])
    rec.save(str(path))
    lookup = av.RecordLookup({"crimson-canyon": str(path)})
    tick = {"type": "tick", "fix": True, "arena": "crimson-canyon", "x": 100.0, "z": 100.0}
    assert lookup.annotate(dict(tick))["floor"] == 500.0
    rec, _ = build_record([Sample(0.0, 100.0, 100.0, 300.0, life=1)])
    rec.save(str(path))
    os.utime(path, (1.0, 1.0))                      # a different mtime, as a rebuild gives
    assert lookup.annotate(dict(tick))["floor"] == 300.0


def test_a_rearm_is_placed_at_the_last_fix_3_to_6_s_before_it_and_kept_for_the_round():
    p = av.LineParser(ARENAS)
    hub = av.Hub("live", "wingman.log")
    lines = [_line(0, "RESUPPLY: missiles exhausted; heading for a resupply marker", "INFO")]
    for sec, x in ((1, 100), (3, 200), (5, 300), (7, 400)):
        lines += [_padlock(sec, 400 + sec), _terrain(sec, f"{x},0")]
    lines.append(_line(11, "RESUPPLY: confirmed ammo=4; urgency reset, resuming target pursuit", "INFO"))
    events = _feed(p, lines)
    kinds = [e["type"] for e in events]
    assert kinds[0] == "resupply" and events[0]["on"] is True
    ticks = [e for e in events if e["type"] == "tick"]
    assert all(t["resupply"] for t in ticks)
    rearm = events[-1]
    assert rearm["type"] == "rearm" and (rearm["x"], rearm["z"], rearm["alt"]) == (400.0, 0.0, 407.0)  # the fix at 7.03, 3.97 s before
    for ev in events:
        hub.publish(ev)
    hub.publish({"type": "death", "t": 12.0})
    assert hub.copy()["rearms"] == [[400.0, 0.0, 407.0]]          # a death does not clear the round's re-arms
    hub.publish({"type": "match", "t": 20.0, "map": "X", "mode": None, "arena": None})
    assert hub.copy()["rearms"] == []
    assert p.feed(_terrain(21, map_name="CRIMSON CANYON"))[-1]["resupply"] is False


def test_a_rearm_with_no_fix_in_its_window_carries_no_position():
    p = av.LineParser(ARENAS)
    events = _feed(p, [_padlock(1, 900), _terrain(1, "0,0"),
                       _line(30, "RESUPPLY: confirmed ammo=4; urgency reset", "INFO")])
    rearm = events[-1]
    assert rearm["type"] == "rearm" and "x" not in rearm


# --- picking and accepting golden-path candidates from the page ---------------------------

def _candidate_files(folder, arena="a"):
    rows = [{"id": 0, "log": "wingman_x.log", "t": 100.0, "verdict": "candidate", "reasons": [], "reject": [],
             "waypoints": [[100.0, 0.0, 0.0, 500.0]]},
            {"id": 1, "log": "wingman_x.log", "t": 200.0, "verdict": "rejected", "reasons": ["a crash within 30 s"],
             "reject": ["crash"], "waypoints": []}]
    with open(os.path.join(folder, f"{arena}-golden-candidates.json"), "w") as fh:
        json.dump({"arena": arena, "transit_alt_m": 2000.0, "paths": rows}, fh)


def test_the_page_gets_candidates_only_marked_when_accepted(tmp_path):
    _candidate_files(str(tmp_path))
    data = av.candidates_with_accepted(str(tmp_path), "a")
    assert [r["id"] for r in data["paths"]] == [0] and data["paths"][0]["accepted"] is False
    out = av.accept_candidate(str(tmp_path), "a", 0, "wingman_x.log", 100.0)
    assert out["ok"] and out["said"] == ["a:0 accepted as a golden path"]
    assert av.candidates_with_accepted(str(tmp_path), "a")["paths"][0]["accepted"] is True
    assert av.candidates_with_accepted(str(tmp_path), "nowhere") is None


def test_accept_refuses_a_renumbered_or_rejected_candidate(tmp_path):
    _candidate_files(str(tmp_path))
    moved = av.accept_candidate(str(tmp_path), "a", 0, "wingman_other.log", 100.0)
    assert not moved["ok"] and "reload" in moved["said"][0]
    rejected = av.accept_candidate(str(tmp_path), "a", 1, "wingman_x.log", 200.0)
    assert not rejected["ok"]
    gold = tmp_path / "a-golden.json"
    assert not gold.exists() or json.loads(gold.read_text())["paths"] == []


def test_accept_needs_the_pages_own_header(tmp_path):
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer
    _candidate_files(str(tmp_path))
    reexported = []
    handler = av.make_handler(av.Hub("live", "x"), {}, str(tmp_path), folder_of={"a": str(tmp_path)},
                              rec_of={"a": "rec.npz"}, reexport=reexported.append)
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/accept"
    body = json.dumps({"arena": "a", "id": 0, "log": "wingman_x.log", "t": 100.0}).encode()
    try:
        with pytest.raises(urllib.error.HTTPError) as err:
            urllib.request.urlopen(urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}))
        assert err.value.code == 403
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json", "X-Airspace": "1"})
        with urllib.request.urlopen(req) as resp:
            assert json.loads(resp.read())["ok"] is True
        assert reexported == ["rec.npz"]
    finally:
        server.shutdown()

"""Design 017: watch the jet fly through an arena's free-airspace record, live or from a log.

Reads ``wingman.log`` as it grows, or replays an archived log, and streams the jet's position to
a browser page. The page draws the arena's 3D scene (``export_scene.py``'s ``<arena>-airspace.glb``)
with three.js, and the jet on it: an arrow along its track, its trail since the last spawn, a line
down to the ground, and a readout of its height over the ground and over the record's flown floor.

It reads the log, the records and the scenes, and writes one thing: a golden path the operator
accepts with the page's button (``<arena>-golden.json``, as ``airspace-golden.py --accept`` writes),
after which it re-exports that arena's scene in the background. Wingman is not touched, so it is
safe beside a running session. The page and the server are local (127.0.0.1).

    uv run --active python scripts/airspace-view.py                          # follow wingman.log
    uv run --active python scripts/airspace-view.py --replay LOG --speed 8   # replay an archived log
    then open http://127.0.0.1:8765/

Each battle tick gives one position (``TERRAIN:``, every 1.5 s); the page glides the jet between
them. ``fix=none`` ticks show the last position, greyed. Altitude is the latest ``PADLOCK:``
reading, heading the latest ``hdg=`` on the ``BT[active]`` line.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import queue
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, ".."))

from wingman.airspace import AirspaceRecord  # noqa: E402

_spec = importlib.util.spec_from_file_location("airspace_record", os.path.join(_HERE, "airspace-record.py"))
_record_tool = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_record_tool)
arenas_from_config = _record_tool.arenas_from_config

_gspec = importlib.util.spec_from_file_location("airspace_golden", os.path.join(_HERE, "airspace-golden.py"))
_golden_tool = importlib.util.module_from_spec(_gspec)
sys.modules.setdefault("airspace_golden", _golden_tool)
_gspec.loader.exec_module(_golden_tool)
EXPORT_SCENE = "test_screenshots/terrain/tools/export_scene.py"

PAGE = os.path.join(_HERE, "airspace-view", "index.html")
THREE_DIR = "test_screenshots/terrain/tools/web/three"
TRAIL_MAX = 600                  # ticks kept per life: 15 minutes at 1.5 s
REARM_WINDOW_S = (3.0, 6.0)      # a re-arm is placed at the last fix this long before it

_RE_TERRAIN = re.compile(r"TERRAIN: map=(.+?) fix=(?:(-?\d+),(-?\d+)|none\(([\w-]+)\))")
_RE_FIELD = {k: re.compile(rf"\b{k}=(\S+)") for k in ("ground", "speed", "ttc_y", "ttc_m", "arm", "act")}
_RE_ALT = re.compile(r"PADLOCK: (\w+) \| Altitude: (-?\d+|None) \| Speed: (\d+|None)")
_RE_HDG = re.compile(r"\bhdg=(\d+(?:\.\d+)?|n/a)")
_RE_STATE = re.compile(r"Game state: (\w+) \S+ (\w+)")
_RE_MATCH = re.compile(r"MATCH: mode=(.*?) map=(.*)$")
_RE_EDGE = re.compile(r"no_digits_for=(\d+(?:\.\d+)?)s")
_RE_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _epoch(line: str) -> float:
    return (dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S").timestamp()
            + int(line[20:23]) / 1000.0)


def _is_line(line: str) -> bool:
    return len(line) > 24 and line[4] == "-" and line[:4].isdigit() and line[19] == ","


def _num(text: "str | None") -> "float | None":
    if text is None:
        return None
    m = re.match(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


class LineParser:
    """Turns log lines into view events. Keeps the latest altitude, speed and heading, which
    are logged on their own lines, to put on the next position tick."""

    def __init__(self, arena_of: dict):
        self.arena_of = arena_of
        self.alt: "float | None" = None
        self.kph: "float | None" = None
        self.hdg: "float | None" = None
        self.padlock: "str | None" = None
        self.state: "str | None" = None
        self.map: "str | None" = None
        self._rejected_t: "float | None" = None
        self._fixes: list = []           # the last few fixes (t, x, z, alt), to place a re-arm
        self.resupply = False

    def feed(self, line: str) -> list:
        if not _is_line(line):
            return []
        if "TERRAIN: map=" in line:
            return self._terrain(line)
        if "PADLOCK: " in line:
            m = _RE_ALT.search(_RE_ANSI.sub("", line))
            if m:
                self.padlock = m.group(1)
                # A reading the plausibility filter rejected (logged just before it) is kept out,
                # as the record does: 12:50 to 13:00 on 2026-11-26 held four, at 13,877 to 23,824 m.
                t = _epoch(line)
                rejected = self._rejected_t is not None and t - self._rejected_t <= 0.05
                self._rejected_t = None
                if not rejected:
                    if m.group(2) != "None":
                        self.alt = float(m.group(2))
                    if m.group(3) != "None":
                        self.kph = float(m.group(3))
            return []
        if "plausibility filter rejected" in line:
            self._rejected_t = _epoch(line)
            return []
        if "BT[active]: selected=" in line:
            m = _RE_HDG.search(line)
            if m:
                self.hdg = None if m.group(1) == "n/a" else float(m.group(1))
            return []
        if "Game state: " in line:
            m = _RE_STATE.search(_RE_ANSI.sub("", line))
            if m:
                self.state = m.group(2)
                return [{"type": "state", "t": _epoch(line), "state": self.state}]
            return []
        if "MATCH: " in line:
            m = _RE_MATCH.search(line)
            if m:
                return [self._match(_epoch(line), m.group(2).strip(), m.group(1).strip())]
            return []
        if "RespawnLatency: OCR edge" in line:
            self.resupply = False
            t = _epoch(line)
            m = _RE_EDGE.search(line)
            return [{"type": "death", "t": t - (float(m.group(1)) if m else 0.0)}]
        if "DEATH CARD:" in line and "DEATH CARDS" not in line:
            return [{"type": "card", "t": _epoch(line), "crash": "no kill card" in line}]
        if "RESUPPLY: missiles exhausted" in line or "RESUPPLY: urgency overtook" in line:
            if self.resupply:
                return []
            self.resupply = True
            return [{"type": "resupply", "t": _epoch(line), "on": True}]
        if "RESUPPLY: confirmed ammo" in line:
            # Placed as the record places a re-arm: the last fix 3 to 6 s before the confirmation,
            # which lands after the ring (Design 020's ring sites; airspace-record.py).
            t = _epoch(line)
            self.resupply = False
            placed = [f for f in self._fixes if t - REARM_WINDOW_S[1] <= f[0] <= t - REARM_WINDOW_S[0]]
            ev = {"type": "rearm", "t": t, "map": self.map, "arena": self.arena_of.get(self.map)}
            if placed:
                ev.update(x=placed[-1][1], z=placed[-1][2], alt=placed[-1][3])
            return [ev]
        if "Configuration loaded" in line:
            self.alt = self.kph = self.hdg = None
            return [{"type": "session", "t": _epoch(line)}]
        return []

    def _match(self, t: float, name: str, mode: "str | None" = None, reset: bool = True) -> dict:
        self.map = name
        if reset:
            self.resupply = False
            self._fixes = []
        return {"type": "match", "t": t, "map": name, "mode": mode, "arena": self.arena_of.get(name)}

    def _terrain(self, line: str) -> list:
        m = _RE_TERRAIN.search(line)
        if not m:
            return []
        t = _epoch(line)
        out = []
        name = m.group(1)
        if name != self.map:
            # A log read from mid-round names its map first here: learn it, keep the round's state.
            out.append(self._match(t, name, reset=self.map is not None))
        tick = {"type": "tick", "t": t, "map": name, "arena": self.arena_of.get(name),
                "alt": self.alt, "kph": self.kph, "hdg": self.hdg, "state": self.state,
                "padlock": self.padlock, "warn": " WARN" in line, "resupply": self.resupply}
        if m.group(2) is not None:
            fields = {k: r.search(line) for k, r in _RE_FIELD.items()}
            tick.update(fix=True, x=float(m.group(2)), z=float(m.group(3)),
                        ground=_num(fields["ground"] and fields["ground"].group(1)),
                        speed=_num(fields["speed"] and fields["speed"].group(1)),
                        ttc_y=fields["ttc_y"] and fields["ttc_y"].group(1),
                        act=fields["act"] and fields["act"].group(1))
            self._fixes.append((t, tick["x"], tick["z"], self.alt))
            del self._fixes[:-12]
        else:
            tick.update(fix=False, reason=m.group(4))
        out.append(tick)
        return out


class RecordLookup:
    """The free-airspace record's floors at a point, per arena, loaded on first use."""

    def __init__(self, record_path_of: dict):
        self.record_path_of = record_path_of
        self._loaded: dict = {}

    def annotate(self, tick: dict) -> dict:
        arena = tick.get("arena")
        if not tick.get("fix") or arena is None:
            return tick
        # Reloaded when its file changes: `make airspace` after a session rewrites it.
        path = self.record_path_of.get(arena)
        try:
            mtime = os.path.getmtime(path) if path else None
        except OSError:
            mtime = None
        held = self._loaded.get(arena)
        if held is None or held[0] != mtime:
            try:
                rec = AirspaceRecord.load(path) if mtime is not None else None
            except Exception:
                rec = None
            self._loaded[arena] = (mtime, rec)
        rec = self._loaded[arena][1]
        if rec is None:
            return tick
        ij = rec.index(tick["x"], tick["z"])
        if ij is not None:
            tick["floor"] = rec.floor_at(tick["x"], tick["z"], trusted=False)
            tick["floor2"] = rec.floor_at(tick["x"], tick["z"])
            tick["passes"] = int(rec.passes[ij])
        return tick


class Hub:
    """The view's state (the arena, the trail since the last spawn) and every connected page."""

    def __init__(self, mode: str, source: str):
        self.lock = threading.Lock()
        self.clients: list = []
        self.snapshot = {"mode": mode, "source": source, "arena": None, "map": None, "mode_name": None,
                         "state": None, "trail": [], "deaths": [], "rearms": [], "last": None,
                         "ended": False}

    def publish(self, event: dict) -> None:
        with self.lock:
            s = self.snapshot
            kind = event["type"]
            if kind == "match":
                s.update(arena=event["arena"], map=event["map"], trail=[], deaths=[], rearms=[])
                if event.get("mode"):
                    s["mode_name"] = event["mode"]
            elif kind == "tick":
                s["last"] = event
                if event.get("arena") and event["arena"] != s["arena"]:
                    s.update(arena=event["arena"], map=event["map"], trail=[], deaths=[], rearms=[])
                if event.get("fix"):
                    s["trail"].append([event["x"], event["z"], event["alt"], event["t"]])
                    del s["trail"][:-TRAIL_MAX]
            elif kind == "death":
                last = s["last"]
                if last and last.get("fix"):
                    s["deaths"].append([last["x"], last["z"], last["alt"]])
                s["trail"] = []
            elif kind == "rearm":
                if event.get("x") is not None:
                    s["rearms"].append([event["x"], event["z"], event["alt"]])
            elif kind == "state":
                s["state"] = event["state"]
            elif kind == "end":
                s["ended"] = True
            for q in list(self.clients):
                with contextlib.suppress(queue.Full):     # a stalled page drops events, not the feed
                    q.put_nowait(event)

    def copy(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.snapshot))

    def subscribe(self) -> "tuple[queue.Queue, dict]":
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self.lock:
            self.clients.append(q)
            snap = json.loads(json.dumps(self.snapshot))
        return q, snap

    def unsubscribe(self, q) -> None:
        with self.lock:
            if q in self.clients:
                self.clients.remove(q)


class LogTail:
    """New complete lines of a log that is appended to and sometimes replaced (wingman rotates
    the previous log away and opens a new one at the same path)."""

    def __init__(self, path: str, from_start: bool = True):
        self.path = path
        self.fh = None
        self.ino = None
        self.partial = ""
        self._open(from_start)

    def _open(self, from_start: bool) -> None:
        if self.fh is not None:
            self.fh.close()
            self.fh = None
        try:
            self.fh = open(self.path, errors="replace")   # noqa: SIM115 (held open between polls)
        except FileNotFoundError:
            return
        self.ino = os.fstat(self.fh.fileno()).st_ino
        self.partial = ""
        if not from_start:
            self.fh.seek(0, os.SEEK_END)

    def poll(self) -> list:
        if self.fh is None:
            self._open(True)
            if self.fh is None:
                return []
        lines = []
        while True:
            chunk = self.fh.readline()
            if not chunk:
                break
            if not chunk.endswith("\n"):
                self.partial += chunk
                break
            lines.append(self.partial + chunk)
            self.partial = ""
        if not lines:
            try:
                st = os.stat(self.path)
            except FileNotFoundError:
                return []
            if st.st_ino != self.ino or st.st_size < self.fh.tell():
                self._open(True)                     # rotated: the new file from its start
                return self.poll()
        return lines


def follow(path: str, parser: LineParser, lookup: RecordLookup, hub: Hub, stop: threading.Event,
           interval: float = 0.25) -> None:
    tail = LogTail(path, from_start=True)          # catch up on the session so far, then follow
    while not stop.is_set():
        for line in tail.poll():
            for ev in parser.feed(line):
                hub.publish(lookup.annotate(ev) if ev["type"] == "tick" else ev)
        stop.wait(interval)


def replay(path: str, parser: LineParser, lookup: RecordLookup, hub: Hub, stop: threading.Event,
           speed: float, start: "str | None" = None, max_gap_s: float = 3.0) -> None:
    """Re-run an archived log at `speed` times real time; gaps over `max_gap_s` (lobby, loading)
    are cut short. `start` (HH:MM) skips to that time of day."""
    prev = None
    with open(path, errors="replace") as fh:
        for line in fh:
            if stop.is_set():
                return
            skipping = bool(start) and _is_line(line) and line[11:16] < start
            for ev in parser.feed(line):
                t = ev.get("t")
                if not skipping and t is not None and prev is not None and t > prev:
                    if stop.wait(min(t - prev, max_gap_s) / speed):
                        return
                if t is not None:
                    prev = t
                hub.publish(lookup.annotate(ev) if ev["type"] == "tick" else ev)
    hub.publish({"type": "end", "t": prev})


def candidates_with_accepted(folder: str, arena: str) -> "dict | None":
    """The arena's golden-path candidates (airspace-golden.py), each marked `accepted` when it is in
    the accepted file; None when there is no candidate list yet."""
    path = os.path.join(folder, f"{arena}-golden-candidates.json")
    if not os.path.exists(path):
        return None
    with open(path) as fh:
        data = json.load(fh)
    accepted = set()
    gold = os.path.join(folder, f"{arena}-golden.json")
    if os.path.exists(gold):
        with open(gold) as fh:
            accepted = {(p["log"], p["t"]) for p in json.load(fh)["paths"]}
    rows = [dict(r, accepted=(r["log"], r["t"]) in accepted) for r in data["paths"] if r["verdict"] == "candidate"]
    return {"arena": arena, "transit_alt_m": data.get("transit_alt_m"), "paths": rows}


def accept_candidate(folder: str, arena: str, ident: int, log: str, t: float) -> dict:
    """Accept candidate `ident` as a golden path, if it is still the flight the page showed: the
    list is renumbered when logs are added, so the log and the re-arm time must match too."""
    path = os.path.join(folder, f"{arena}-golden-candidates.json")
    if not os.path.exists(path):
        return {"ok": False, "said": ["no candidate list for this arena"]}
    with open(path) as fh:
        rows = json.load(fh)["paths"]
    row = next((r for r in rows if r["id"] == ident and r["log"] == log and abs(r["t"] - t) < 0.01), None)
    if row is None:
        return {"ok": False, "said": ["that candidate is no longer in the list; reload the page"]}
    said = _golden_tool.accept_paths(os.path.join(folder, f"{arena}-golden.json"), arena, [row], {ident})
    return {"ok": row["verdict"] == "candidate", "said": said}


def safe_join(root: str, rel: str) -> "str | None":
    """`rel` under `root`, or None when it would leave it."""
    root = os.path.realpath(root)
    path = os.path.realpath(os.path.join(root, rel.lstrip("/")))
    return path if path == root or path.startswith(root + os.sep) else None


_TYPES = {".js": "text/javascript", ".html": "text/html; charset=utf-8", ".glb": "model/gltf-binary",
          ".json": "application/json", ".png": "image/png"}


def make_handler(hub: Hub, glb_of: dict, three_dir: str, page: str = PAGE, folder_of: "dict | None" = None,
                 rec_of: "dict | None" = None, reexport=None):
    folder_of = folder_of or {}
    rec_of = rec_of or {}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):       # quiet: the SSE stream would flood the terminal
            pass

        def _file(self, path: "str | None", cache: bool = False) -> None:
            if not path or not os.path.isfile(path):
                self.send_error(404)
                return
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", _TYPES.get(os.path.splitext(path)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "max-age=3600" if cache else "no-store")
            self.end_headers()
            with open(path, "rb") as fh:
                while True:
                    block = fh.read(1 << 20)
                    if not block:
                        break
                    self.wfile.write(block)

        def do_GET(self):  # noqa: N802 (http.server's name)
            route = self.path.split("?", 1)[0]
            try:
                if route in ("/", "/index.html"):
                    self._file(page)
                elif route.startswith("/three/"):
                    self._file(safe_join(three_dir, route[len("/three/"):]), cache=True)
                elif route.startswith("/arena/") and route.endswith(".glb"):
                    self._file(glb_of.get(route[len("/arena/"):-len(".glb")]))
                elif route.startswith("/arena/") and route.endswith("/candidates.json"):
                    arena = route[len("/arena/"):-len("/candidates.json")]
                    data = candidates_with_accepted(folder_of[arena], arena) if arena in folder_of else None
                    self._json(data if data is not None else {"arena": arena, "paths": []})
                elif route == "/snapshot":
                    body = json.dumps(hub.copy()).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif route == "/events":
                    self._events()
                else:
                    self.send_error(404)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _json(self, data, status: int = 200) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802 (http.server's name)
            # Only the page's own script sends this header; a plain cross-site form cannot, and a
            # script on another site would need a CORS preflight this server never answers.
            if self.path != "/accept" or self.headers.get("X-Airspace") != "1":
                self.send_error(403)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                arena = str(body["arena"])
                if arena not in folder_of:
                    self._json({"ok": False, "said": ["unknown arena"]}, 400)
                    return
                result = accept_candidate(folder_of[arena], arena, int(body["id"]), str(body["log"]), float(body["t"]))
                if result["ok"] and reexport is not None and rec_of.get(arena):
                    reexport(rec_of[arena])
                self._json(result)
            except (KeyError, ValueError, TypeError) as exc:
                self._json({"ok": False, "said": [f"bad request: {exc}"]}, 400)

        def _events(self) -> None:
            q, snap = hub.subscribe()
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                snap["type"] = "snapshot"
                snap["scenes"] = {k: os.path.exists(v) for k, v in glb_of.items()}
                self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                self.wfile.flush()
                while True:
                    try:
                        ev = q.get(timeout=15.0)
                        self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode())
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def arena_files(config: str) -> "tuple[dict, dict, dict]":
    """(map name -> arena, arena -> .glb path, arena -> record path)."""
    arena_of, height_of, _ = arenas_from_config(config)
    glb_of, rec_of = {}, {}
    for arena, height in height_of.items():
        folder = os.path.dirname(height)
        glb_of[arena] = os.path.join(folder, f"{arena}-airspace.glb")
        rec_of[arena] = os.path.join(folder, f"{arena}-airspace.npz")
    return arena_of, glb_of, rec_of


def reexport_in_background(record_path: str) -> None:
    """Rebuild one arena's scene after an accept, at low priority, without waiting for it."""
    subprocess.Popen(["nice", "-n", "15", sys.executable, EXPORT_SCENE, record_path],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--log", default="wingman.log", help="the log to follow (default wingman.log)")
    ap.add_argument("--replay", help="replay this archived log instead of following one")
    ap.add_argument("--speed", type=float, default=8.0, help="replay speed, times real time")
    ap.add_argument("--start", help="replay from this time of day, HH:MM")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default="wingman/config.yaml")
    ap.add_argument("--three", default=THREE_DIR)
    args = ap.parse_args(argv)
    if not os.path.exists(os.path.join(args.three, "three.module.js")):
        print(f"three.js is not at {args.three} (see test_screenshots/terrain/README.md)")
        return 1
    arena_of, glb_of, rec_of = arena_files(args.config)
    missing = sorted(a for a, p in glb_of.items() if not os.path.exists(p))
    if missing:
        print("no 3D scene yet for " + ", ".join(missing) + ": run export_scene.py on their records")
    source = args.replay or args.log
    hub = Hub("replay" if args.replay else "live", source)
    parser, lookup, stop = LineParser(arena_of), RecordLookup(rec_of), threading.Event()
    if args.replay:
        feeder = threading.Thread(target=replay, args=(args.replay, parser, lookup, hub, stop, args.speed, args.start),
                                  daemon=True)
    else:
        feeder = threading.Thread(target=follow, args=(args.log, parser, lookup, hub, stop), daemon=True)
    feeder.start()
    folder_of = {a: os.path.dirname(p) for a, p in rec_of.items()}
    server = ThreadingHTTPServer(("127.0.0.1", args.port),
                                 make_handler(hub, glb_of, args.three, folder_of=folder_of, rec_of=rec_of,
                                              reexport=reexport_in_background))
    server.daemon_threads = True
    print(f"{'replaying' if args.replay else 'following'} {source}: open http://127.0.0.1:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

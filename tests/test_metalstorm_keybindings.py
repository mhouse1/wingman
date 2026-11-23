"""Version-controlled keybindings (scripts/metalstorm-keybindings.py).

`make export-keybindings` writes a prefix's control entries into the repo, and
`make deploy-keybindings` loads them into another PC's game. What is under test:
only control entries leave the source, identity and per-machine settings never
do, and a deploy changes nothing but those entries.

Usage: uv run --active pytest tests/test_metalstorm_keybindings.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "metalstorm_keybindings",
    Path(__file__).parent.parent / "scripts" / "metalstorm-keybindings.py")
kb = importlib.util.module_from_spec(_spec)
sys.modules["metalstorm_keybindings"] = kb
_spec.loader.exec_module(kb)
sync = kb.sync

_HEAD = '[Software\\\\Wine\\\\Explorer] 1\n"Desktop"="Default"\n\n'
_TAIL = '\n[Software\\\\Wine\\\\X11 Driver] 1\n"Decorated"="N"\n'
_KBM = "air-combat-settings--default_liveBindings-KeyboardMouse-v2_h1"
_OVR = "air-combat-settings--default_inputBindingOverrides1_h2"
_SCHEME = "air-combat-settings--default_liveControlScheme-KeyboardMouse-v1_h3"
_VOLUME = "air-combat-settings--default_musicVolume_h4"
_HUD = "air-combat-settings--default_hudPartyColor_h5"
_TOKEN = "client-settings--default_auth_token_h6"
_CHAT = "chat-text:direct-chat-abc"
_SCREEN = "Screenmanager Resolution Width_h7"


def _prefix(root: Path, name: str, entries: dict) -> Path:
    prefix = root / name
    prefix.mkdir()
    body = _HEAD + sync._SECTION + " 1791741412\n#time=1dc0000000\n"
    body += "".join(f'"{k}"={v}\n' for k, v in entries.items())
    (prefix / "user.reg").write_text(body + _TAIL, encoding="utf-8")
    return prefix


def _entries(prefix: Path) -> dict:
    found, _, _ = sync.parse_section((prefix / "user.reg").read_text(encoding="utf-8"))
    return found


@pytest.fixture
def free(monkeypatch):
    monkeypatch.setattr(sync, "wait_until_free", lambda _p, _t: True)


@pytest.fixture
def veda(tmp_path):
    return _prefix(tmp_path, "Metalstorm", {
        _KBM: "hex:01,02,\\\n  03,04",
        _OVR: "hex:7b,00",
        _SCHEME: "dword:00000001",
        _VOLUME: "hex(4):00,00,00,e0,2a,ed,95,3f",
        _HUD: "hex:0a",
        _TOKEN: "hex:aa",
        _CHAT: "hex:68,69",
        _SCREEN: "dword:00000780",
    })


@pytest.fixture
def other_pc(tmp_path):
    (tmp_path / "other").mkdir()
    return _prefix(tmp_path / "other", "Metalstorm", {
        _KBM: "hex:ff",
        _VOLUME: "hex(4):00,00,00,00,00,00,00,00",
        _TOKEN: "hex:bb",
        _SCREEN: "dword:00000a00",
    })


def _run(*args) -> int:
    return kb.main(["metalstorm-keybindings.py", *[str(a) for a in args]])


def test_export_keeps_control_entries_only(veda, tmp_path, free):
    out = tmp_path / "kb.reg"
    assert _run("export", veda, out) == 0
    text = out.read_text(encoding="utf-8")
    assert set(kb.parse_file(text)) == {_KBM, _OVR, _SCHEME}
    for leaked in (_TOKEN, _CHAT, _VOLUME, _HUD, _SCREEN):
        assert leaked not in text
    assert text.startswith("Windows Registry Editor Version 5.00\n")


def test_deploy_writes_the_bindings_and_nothing_else(veda, other_pc, tmp_path, free):
    out = tmp_path / "kb.reg"
    _run("export", veda, out)
    assert _run("deploy", other_pc, out) == 0
    after = _entries(other_pc)
    want = _entries(veda)
    for k in (_KBM, _OVR, _SCHEME):
        assert after[k] == want[k]
    assert after[_TOKEN].strip().endswith("hex:bb")
    assert after[_VOLUME].strip().endswith("00,00,00,00,00,00,00,00")
    assert after[_SCREEN].strip().endswith("00000a00")
    assert _CHAT not in after
    text = (other_pc / "user.reg").read_text(encoding="utf-8")
    assert text.startswith(_HEAD) and text.endswith(_TAIL)
    assert list(other_pc.glob("user.reg.bak-*"))


def test_a_second_deploy_changes_nothing(veda, other_pc, tmp_path, free, capsys):
    out = tmp_path / "kb.reg"
    _run("export", veda, out)
    _run("deploy", other_pc, out)
    for b in other_pc.glob("user.reg.bak-*"):
        b.unlink()
    capsys.readouterr()
    assert _run("deploy", other_pc, out) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert not list(other_pc.glob("user.reg.bak-*"))


def test_dry_run_writes_nothing(veda, other_pc, tmp_path, free):
    out = tmp_path / "kb.reg"
    _run("export", veda, out)
    before = (other_pc / "user.reg").read_text(encoding="utf-8")
    assert _run("deploy", other_pc, out, "--dry-run") == 0
    assert (other_pc / "user.reg").read_text(encoding="utf-8") == before


def test_deploy_refuses_while_the_game_runs(veda, other_pc, tmp_path, monkeypatch):
    out = tmp_path / "kb.reg"
    monkeypatch.setattr(sync, "wait_until_free", lambda _p, _t: True)
    _run("export", veda, out)
    monkeypatch.setattr(sync, "wait_until_free", lambda _p, _t: False)
    before = (other_pc / "user.reg").read_text(encoding="utf-8")
    assert _run("deploy", other_pc, out) == 1
    assert (other_pc / "user.reg").read_text(encoding="utf-8") == before


def test_deploy_refuses_a_file_with_a_non_binding_key(other_pc, tmp_path, free):
    bad = tmp_path / "kb.reg"
    bad.write_text(kb.render_file({_TOKEN: f'"{_TOKEN}"=hex:aa\n'}, "x"), encoding="utf-8")
    assert _run("deploy", other_pc, bad) == 1
    assert _entries(other_pc)[_TOKEN].strip().endswith("hex:bb")


def test_windows_file_is_utf16_with_crlf(veda, tmp_path, free):
    out = tmp_path / "kb.reg"
    _run("export", veda, out)
    data = kb.windows_reg_bytes(kb.parse_file(out.read_text(encoding="utf-8")))
    assert data.startswith(b"\xff\xfe")
    text = data.decode("utf-16-le").lstrip("﻿")
    assert "\r\n" in text and "\n" not in text.replace("\r\n", "")
    assert "[HKEY_CURRENT_USER\\Software\\Starform\\Metalstorm]" in text


def test_the_tracked_file_holds_only_bindings():
    if not kb.DEFAULT_FILE.is_file():
        pytest.skip("no exported keybindings yet")
    entries = kb.parse_file(kb.DEFAULT_FILE.read_text(encoding="utf-8"))
    assert entries
    assert all(kb.is_binding(k) for k in entries)

"""Settings sync between Wine prefixes (scripts/sync-metalstorm-settings.py).

`make launch-game` runs it with --launch-step before every launch of an account
prefix, so `make g`, `g1` and `g2` fly the same key bindings and settings. What
is under test: settings are copied, identity never is, and a launch step never
fails a launch or writes under a live wineserver.

Usage: uv run pytest tests/test_sync_metalstorm_settings.py -q
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location(
    "sync_metalstorm_settings",
    Path(__file__).parent.parent / "scripts" / "sync-metalstorm-settings.py")
sync = importlib.util.module_from_spec(_spec)
sys.modules["sync_metalstorm_settings"] = sync
_spec.loader.exec_module(sync)

_HEAD = '[Software\\\\Wine\\\\Explorer] 1\n"Desktop"="Default"\n\n'
_TAIL = '\n[Software\\\\Wine\\\\X11 Driver] 1\n"Decorated"="N"\n'
_BINDINGS = "air-combat-settings--default_liveBindings-KeyboardMouse-v2_h1"
_TOKEN = "client-settings--default_auth_token_h2"
_VOLUME = "client-settings--default_audioVolume-v2_h3"
_LAST_FPS = "air-combat-settings--default_lastMatchAvFps_h4"
_REPLAY = "air-combat-settings--default_replayMatchIdForAutoEnterMatch_h5"


def _prefix(root: Path, name: str, entries: "dict | None") -> Path:
    prefix = root / name
    prefix.mkdir()
    body = _HEAD
    if entries is not None:
        body += sync._SECTION + " 1791741412\n#time=1dc0000000\n"
        body += "".join(f'"{k}"={v}\n' for k, v in entries.items())
    (prefix / "user.reg").write_text(body + _TAIL, encoding="utf-8")
    return prefix


def _entries(prefix: Path) -> dict:
    found, _, _ = sync.parse_section((prefix / "user.reg").read_text(encoding="utf-8"))
    return found


@pytest.fixture
def free(monkeypatch):
    """No wineserver is serving the target."""
    monkeypatch.setattr(sync, "wait_until_free", lambda _p, _t: True)


@pytest.fixture
def prefixes(tmp_path):
    main = _prefix(tmp_path, "Metalstorm", {
        _BINDINGS: "hex:01,02,\\\n  03,04",
        _VOLUME: "hex:0a",
        _TOKEN: "hex:aa",
        _LAST_FPS: "hex:3c",
        _REPLAY: "hex:99",
    })
    acct = _prefix(tmp_path, "Metalstorm-acct2", {
        _BINDINGS: "hex:ff",
        _TOKEN: "hex:bb",
    })
    return main, acct


def _run(*args) -> int:
    return sync.main(["sync-metalstorm-settings.py", *[str(a) for a in args]])


def test_bindings_and_settings_are_copied_and_the_login_is_not(prefixes, free):
    main, acct = prefixes
    assert _run(main, acct) == 0
    got = _entries(acct)
    assert got[_BINDINGS] == _entries(main)[_BINDINGS]     # multi-line value, whole
    assert got[_VOLUME] == _entries(main)[_VOLUME]         # key the target lacked
    assert got[_TOKEN] == f'"{_TOKEN}"=hex:bb\n'           # the target's own login
    text = (acct / "user.reg").read_text(encoding="utf-8")
    assert text.startswith(_HEAD) and text.endswith(_TAIL)


def test_last_session_state_is_not_copied(prefixes, free):
    """It changes every match, so copying it would rewrite the target at every launch."""
    main, acct = prefixes
    assert _run(main, acct) == 0
    assert _LAST_FPS not in _entries(acct)
    assert _REPLAY not in _entries(acct)


def test_a_second_run_changes_nothing_and_writes_no_backup(prefixes, free, capsys):
    main, acct = prefixes
    assert _run(main, acct, "--launch-step") == 0
    backups = sorted(p.name for p in acct.glob("user.reg.bak-*"))
    assert len(backups) == 1
    capsys.readouterr()
    assert _run(main, acct, "--launch-step") == 0
    assert sorted(p.name for p in acct.glob("user.reg.bak-*")) == backups
    assert "already matches" in capsys.readouterr().out


def test_launch_step_prints_one_line_not_the_key_list(prefixes, free, capsys):
    main, acct = prefixes
    assert _run(main, acct, "--launch-step") == 0
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    assert "copied 2 settings from Metalstorm into Metalstorm-acct2" in out[0]


def test_by_hand_a_prefix_in_use_is_refused_without_waiting(prefixes, monkeypatch):
    main, acct = prefixes
    before = (acct / "user.reg").read_text(encoding="utf-8")
    waits = []
    monkeypatch.setattr(sync, "wait_until_free",
                        lambda _p, t: waits.append(t) or False)
    assert _run(main, acct) == 1
    assert waits == [0.0]
    assert (acct / "user.reg").read_text(encoding="utf-8") == before


def test_launch_step_waits_for_wineserver_then_skips_without_failing(
        prefixes, monkeypatch, capsys):
    """A launch must go ahead, and nothing may be written under a live wineserver."""
    main, acct = prefixes
    before = (acct / "user.reg").read_text(encoding="utf-8")
    waits = []
    monkeypatch.setattr(sync, "wait_until_free",
                        lambda _p, t: waits.append(t) or False)
    assert _run(main, acct, "--launch-step") == 0
    assert waits == [sync._LAUNCH_WAIT_S]
    assert (acct / "user.reg").read_text(encoding="utf-8") == before
    assert "settings sync skipped" in capsys.readouterr().out


def test_launch_step_is_silent_for_the_source_prefix_itself(prefixes, free, capsys):
    """launch-game runs it for every prefix, `make g` on the main one included."""
    main, _ = prefixes
    assert _run(main, main, "--launch-step") == 0
    assert capsys.readouterr().out == ""
    assert _run(main, main) == 1


def test_launch_step_skips_a_prefix_that_has_never_run_the_game(tmp_path, free, capsys):
    """The first launch of a new account prefix must not be blocked by the sync."""
    main = _prefix(tmp_path, "Metalstorm", {_VOLUME: "hex:0a"})
    fresh = _prefix(tmp_path, "Metalstorm-acct3", None)
    missing = tmp_path / "Metalstorm-acct4"
    for target in (fresh, missing):
        assert _run(main, target, "--launch-step") == 0
        assert "settings sync skipped" in capsys.readouterr().out
        assert _run(main, target) == 1


def test_dry_run_writes_nothing_and_does_not_need_the_prefix_free(prefixes, monkeypatch):
    main, acct = prefixes
    before = (acct / "user.reg").read_text(encoding="utf-8")

    def must_not_ask(_p, _t):
        raise AssertionError("a dry run does not check for wineserver")

    monkeypatch.setattr(sync, "wait_until_free", must_not_ask)
    assert _run(main, acct, "--dry-run") == 0
    assert (acct / "user.reg").read_text(encoding="utf-8") == before
    assert not list(acct.glob("user.reg.bak-*"))


def test_no_identity_key_passes_the_allowlist():
    for name in ("client-settings--default_auth_token_h1",
                 "client-settings--default_selectedAccountId_h1",
                 "client-settings--default_generatedDeviceIdentifier_h1",
                 "chat-text:direct-chat-abc_h1",
                 "air-combat-settings--default_clear-creds_h1"):
        assert not sync.is_allowed(name), name

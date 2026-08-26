"""Key bindings extracted to their own module.

The mapping between a wingman action and the key it presses was buried in
controller.py. It now lives in `wingman/keybindings.py` so it can be found and
edited without reading the controller.

Two things need pinning: the extraction must not have broken the import paths
other modules and tests rely on, and the wingman hotkeys must not collide with
the keys wingman injects into the game.
"""

from wingman import controller as controller_module
from wingman import keybindings

# Keys wingman INJECTS into the game. These must match the game's own control
# settings — see the keybindings module docstring.
GAME_KEYS = {
    "NOSE_UP_KEY", "NOSE_DOWN_KEY", "ROLL_LEFT_KEY", "ROLL_RIGHT_KEY",
    "YAW_LEFT", "AFTERBURNER_KEY", "AIRBRAKE_KEY", "WINGSWEEP_KEY",
    "DEPLOY_FLARES_KEY", "FIRE_MACHINE_GUN", "FIRE_ACTIVE_WEAPON",
    "SWITCH_WEAPON", "SPECIAL_ABILITY", "PADLOCK_CAMERA",
}

# Keys wingman GRABS from the keyboard as its own controls.
HOTKEYS = {
    "TOGGLE_WEAPON_LOOP_KEY", "MISSION_J20_KEY", "MISSION_LOITER_KEY",
    "CANCEL_MISSION_KEY", "CAPTURE_SCREEN_SHOT", "AUTO_MISSION_KEY",
    "SIMULATE_RESPAWN_KEY",
}


def test_controller_still_re_exports_every_binding():
    """Existing imports (`from wingman.controller import NOSE_UP_KEY`) must
    keep working — several modules and four test files rely on them."""
    exported = [n for n in dir(keybindings) if not n.startswith("__")]
    assert exported, "keybindings module exports nothing"
    for name in exported:
        assert hasattr(controller_module, name), f"controller no longer re-exports {name}"
        assert getattr(controller_module, name) is getattr(keybindings, name), \
            f"{name} is a copy, not the same object"


def test_every_binding_is_defined():
    for name in GAME_KEYS | HOTKEYS:
        assert hasattr(keybindings, name), f"{name} missing from keybindings"
        assert getattr(keybindings, name), f"{name} is empty"


def test_no_game_key_is_also_a_wingman_hotkey():
    """A collision means pressing a wingman hotkey also flies the aircraft, or
    the hotkey listener reads wingman's own injection as manual takeover."""
    game = {getattr(keybindings, n) for n in GAME_KEYS}
    hot = {getattr(keybindings, n) for n in HOTKEYS}
    assert not (game & hot), f"key used for both a game control and a hotkey: {game & hot}"


def test_game_keys_are_unique():
    """Two actions on one key means one of them never happens."""
    seen = {}
    for name in GAME_KEYS:
        key = getattr(keybindings, name)
        assert key not in seen, f"{name} and {seen[key]} are both bound to {key!r}"
        seen[key] = name


def test_watched_maneuver_keys_are_flight_controls_plus_arrows():
    """ADR 070 d4: this set is what the hotkey listener treats as manual
    takeover, so it must cover the stick axes and nothing unrelated."""
    watched = set(keybindings._WATCHED_MANEUVER_KEYS)
    for name in ("NOSE_UP_KEY", "NOSE_DOWN_KEY", "ROLL_LEFT_KEY", "ROLL_RIGHT_KEY"):
        assert getattr(keybindings, name) in watched, f"{name} not watched"
    assert set(keybindings.ALT_FLIGHT_KEYS) <= watched
    assert keybindings.DEPLOY_FLARES_KEY not in watched, \
        "flares are fired programmatically every mission — watching them would " \
        "read wingman's own injection as manual takeover"


def test_emotes_are_documented_but_not_bound():
    """Design 003 wiring is not implemented; the list is a record, not config."""
    assert len(keybindings.EMOTES) == 10
    assert keybindings.EMOTES["EMOTE5"] == "Good luck"
    bound = {getattr(keybindings, n) for n in GAME_KEYS | HOTKEYS}
    assert not (set(keybindings.EMOTES) & bound), "emote names must not be key values"

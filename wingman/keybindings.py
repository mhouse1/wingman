"""In-game key bindings, and the emote list, in one place.

Extracted from `controller.py` so the mapping between a wingman action and the
key it presses is findable without reading the controller. Everything here is
re-exported by `wingman.controller`, so existing imports keep working — see
`tests/test_keybindings.py`.

**These must match the game's own control settings.** MetalStorm stores its
bindings in the Wine prefix registry (`inputBindingOverrides1/2` under
`Software\\Starform\\Metalstorm`), and wingman's `scripts/sync-metalstorm-settings.py`
copies them between accounts. Changing a value here without rebinding in-game
makes wingman press the wrong key; the failure looks like the aircraft ignoring
commands rather than like a configuration error.
"""

# --- Flight controls --------------------------------------------------------
NOSE_UP_KEY = 'i'          # FLIGHT_CONTROL_KEY
NOSE_DOWN_KEY = 'k'        # FLIGHT_CONTROL_KEY
ROLL_LEFT_KEY = 'j'        # FLIGHT_CONTROL_KEY
ROLL_RIGHT_KEY = 'l'       # FLIGHT_CONTROL_KEY
YAW_LEFT = ';'             # yaw axis - left rudder (ADR 070)
AFTERBURNER_KEY = 'e'
AIRBRAKE_KEY = 'd'
WINGSWEEP_KEY = 'w'

# --- Weapons and countermeasures -------------------------------------------
DEPLOY_FLARES_KEY = 'space'
FIRE_MACHINE_GUN = 'a'
FIRE_ACTIVE_WEAPON = 'f'
SWITCH_WEAPON = 'g'
SPECIAL_ABILITY = 'q'

# --- Camera -----------------------------------------------------------------
PADLOCK_CAMERA = 'p'

# --- Manual-takeover detection ---------------------------------------------
# Arrow keys also trigger GAME_BATTLE_MANUAL.
ALT_FLIGHT_KEYS = ('up', 'down', 'left', 'right')

# Keys the maneuver-key hotkey listener watches as a manual-takeover signal.
# Anything held programmatically from this set MUST be bracketed with
# _inc_programmatic_key / release grace, or its XTest auto-repeats read as the
# player and self-cancel the mission (ADR 070 d4).
_WATCHED_MANEUVER_KEYS = (NOSE_UP_KEY, NOSE_DOWN_KEY, ROLL_LEFT_KEY, ROLL_RIGHT_KEY,
                          *ALT_FLIGHT_KEYS)

# --- Wingman's own hotkeys --------------------------------------------------
# These are wingman controls, not game controls: they are grabbed from the
# keyboard rather than injected, so they must not collide with the game bindings
# above (test_keybindings.py asserts that).
TOGGLE_WEAPON_LOOP_KEY = 'x'   # toggle the weapon firing loop
MISSION_J20_KEY = 'u'          # start J20 mission
MISSION_LOITER_KEY = 'y'       # start loiter mission
CANCEL_MISSION_KEY = 'end'     # cancel the active mission
CAPTURE_SCREEN_SHOT = 'v'      # capture a screenshot (testing/debugging)
AUTO_MISSION_KEY = 'm'         # start an automatic mission from the detected
                               # game state (not implemented yet)
SIMULATE_RESPAWN_KEY = 'b'     # inject a fake respawn OCR result (testing)
FINISH_ROUND_THEN_EXIT = 'z'    # finish the round, exit wingman at the lobby,
                               # then close MetalStorm (ADR 094). Press again
                               # to cancel a pending exit.
# --- Available emotes in-game ----------------------------------------------
# Not wired up. Kept for the emote support requested in Design 003; the
# comments record the intended binding so the decision does not have to be
# remade later.
#
#   EMOTE1   Moving to       bind to numpad 1
#   EMOTE2   Help!
#   EMOTE3   Defend
#   EMOTE4   Attack          bind to T — pairs with Design 003 target painting,
#                            for marking targets for the weapon loop
#   EMOTE5   Good luck       bind to 'u', same key as the J20 mission so it is
#                            easy to reach at the start of a match
#   EMOTE6   Well Played
#   EMOTE7   Wow!
#   EMOTE8   Thanks!
#   EMOTE9   Good Game!
#   EMOTE10  Oops!
EMOTES = {
    "EMOTE1": "Moving to",
    "EMOTE2": "Help!",
    "EMOTE3": "Defend",
    "EMOTE4": "Attack",
    "EMOTE5": "Good luck",
    "EMOTE6": "Well Played",
    "EMOTE7": "Wow!",
    "EMOTE8": "Thanks!",
    "EMOTE9": "Good Game!",
    "EMOTE10": "Oops!",
}

# Overview
Specific key bindings recommended for wingman

# Keys
* Note under Setings > General you should set the Controls to Controller/Joystick without this, the other buttons work but the Pitch up and down doesnt
```
FireGuns=A,A
FireActiveWeapon=B,F
DeployFlares=Y,Space
SelectNextWeapon=X,G
SpecialAbility=L1,Q
Pitch=K,I
Yaw=;
Roll=J,L
Afterburner=E
Airbrakes=D
ToggleWingSweep=W
LookVertical=RightStickY
LookHorizontal=RightStickX
FreeLookMode=LeftShift,LMB
PadlockCamera=R3,P
UseEmote1=Keypad1
UseEmote2=Keypad2
UseEmote3=Keypad3
UseEmote4=T
UseEmote5=U
UseEmote6=keypad6
UseEmote7=Keypad7
UseEmote8=Keypad8
UseEmote9=Keypad9
```

* configured by defaul, you shouldnt need to edit
In settings > general
```
Up Direction=Normal
Turning Mode=Standard
```
# Copying the bindings to another PC

VEDA's bindings are version-controlled in `scripts/metalstorm-keybindings.reg`
(controls only: no volumes, resolution, login or chat). Close the game first in
both steps, because it writes its bindings to disk only when it quits.

On VEDA, after changing a binding in the game:
```
make export-keybindings      # game -> repo; then commit the .reg
```

On the other PC, after pulling:
```
make deploy-keybindings DRY=--dry-run   # list what would change
make deploy-keybindings                 # repo -> game
```

On Linux this writes the main Heroic prefix (backup `user.reg.bak-*` beside it;
`KEYBINDINGS_PREFIX=...` picks another), and the account prefixes pick it up at
their next launch. On Windows it runs `reg import`, after saving the old key to
`%TEMP%\metalstorm-before-*.reg`.

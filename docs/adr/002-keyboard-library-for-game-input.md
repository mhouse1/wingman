# ADR 002: Using Keyboard Library for Game Input

**Date**: 2026-01-10

**Status**: Accepted

**Context**: The MetalStorm Wingman prototype needs to simulate keyboard input to control the airplane (e.g., holding 'w' to pitch up). Initially, we implemented keyboard control using PyAutoGUI, which works well for mouse input (left mouse button clicks) but keyboard presses were not being registered by the game.

## Problem

PyAutoGUI successfully sends mouse button events (LMB) that the game recognizes, but keyboard key presses (e.g., 'w', arrow keys, 'T') are not detected by the game at all.

**Symptoms:**
- `pyautogui.click()` and `pyautogui.mouseDown()`/`mouseUp()` work perfectly
- `pyautogui.keyDown('w')`/`keyUp('w')` and `pyautogui.press('w')` are not recognized
- Manual keyboard input works normally in the game
- Keyboard events work in text editors and other applications

**Root Cause:**
Many games, especially flight simulators and action games, use **DirectInput** or **raw input APIs** to read keyboard state directly from the hardware, bypassing the standard Windows keyboard message queue that PyAutoGUI uses. PyAutoGUI sends `WM_KEYDOWN`/`WM_KEYUP` messages which work for most applications but are ignored by games using lower-level input methods.

## Decision

Use the **`keyboard` library** (already imported for hotkey detection) for simulating keyboard input to the game instead of PyAutoGUI.

### Implementation

Updated `Controller.loiter()` method in `controller.py`:

```python
if keyboard_module:
    keyboard_module.press('w')      # Send low-level keyboard event
    time.sleep(hold_seconds)
    keyboard_module.release('w')
else:
    # Fallback to pyautogui if keyboard not available
    pyautogui.keyDown('w')
    time.sleep(hold_seconds)
    pyautogui.keyUp('w')
```

### Why This Works

The `keyboard` library sends keyboard events at a lower level than PyAutoGUI:

1. **Kernel-level injection**: Uses Windows `SendInput` API with `KEYEVENTF_SCANCODE` flag, which sends scan codes directly
2. **Hardware simulation**: Mimics actual keyboard hardware signals that DirectInput can detect
3. **Same mechanism**: Uses the same input method that allows the library to detect key presses globally (hotkey detection)

## Alternatives Considered

### 1. PyAutoGUI (original approach)
- **Pros**: Simple API, already used for mouse control, cross-platform
- **Cons**: Sends high-level keyboard messages that games ignore
- **Verdict**: ❌ Rejected - doesn't work with DirectInput games

### 2. PyDirectInput
- **Pros**: Specifically designed for games, DirectInput-compatible
- **Cons**: Additional dependency, Windows-only, less maintained
- **Verdict**: ⚠️ Viable alternative if keyboard library fails

### 3. win32api (pywin32)
- **Pros**: Low-level Windows API access, full control
- **Cons**: Windows-only, complex API, requires understanding of scan codes and virtual keys
- **Verdict**: ⚠️ Too complex for current needs

### 4. AHK (AutoHotkey) via subprocess
- **Pros**: Extremely reliable for game input
- **Cons**: External dependency, requires AHK installation, subprocess overhead
- **Verdict**: ❌ Too heavyweight

### 5. pynput
- **Pros**: Cross-platform, similar to keyboard library
- **Cons**: Another dependency, less proven with games in our testing
- **Verdict**: ⚠️ Fallback option

## Consequences

### Positive

- ✅ **Keyboard input works**: Game recognizes all keyboard events
- ✅ **No additional dependencies**: Already using `keyboard` for hotkey detection
- ✅ **Consistent API**: Both input detection and simulation use same library
- ✅ **Reliable**: Works with DirectInput, raw input, and standard input methods
- ✅ **Fast**: Low latency, suitable for real-time flight control

### Negative

- ⚠️ **Windows-focused**: `keyboard` library works best on Windows (game is PC-only anyway)
- ⚠️ **Requires privileges**: May need admin rights for some games with anti-cheat
- ⚠️ **PyAutoGUI split**: Now using PyAutoGUI for mouse, keyboard library for keys (acceptable tradeoff)

### Neutral

- Mouse input remains on PyAutoGUI (works fine, no need to change)
- Fallback to PyAutoGUI if `keyboard` unavailable (for non-game testing)

## Technical Notes

### PyAutoGUI Keyboard Method
```python
# High-level - uses Windows message queue
pyautogui.keyDown('w')  # Sends WM_KEYDOWN
pyautogui.keyUp('w')    # Sends WM_KEYUP
```

### Keyboard Library Method
```python
# Low-level - uses SendInput with scan codes
keyboard_module.press('w')    # SendInput with KEYEVENTF_SCANCODE
keyboard_module.release('w')  # Hardware-level event
```

### Why Mouse Works Differently
Mouse input uses `SendInput` with `MOUSEEVENTF_*` flags which work consistently across applications and games. DirectInput primarily affects keyboard/gamepad input, not mouse.

## Future Considerations

- **Anti-cheat compatibility**: Some anti-cheat systems may block even low-level input simulation
- **Virtual input devices**: Tools like `vjoy` or `vigembus` create virtual hardware that's undetectable
- **Custom driver**: Ultimate solution would be a kernel-mode driver (extreme overkill)
- **Monitor for breakage**: Game updates might change input handling

## Validation

Tested successfully with:
- ✅ Text editor (Notepad) - both methods work
- ✅ MetalStorm flight game - `keyboard` library works, PyAutoGUI doesn't
- ✅ Loiter maneuver (`ctrl.loiter()`) - airplane pitches up correctly
- ✅ Main loop (`ctrl.fire()`) - still uses PyAutoGUI for LMB, works fine

## References

- [keyboard library documentation](https://github.com/boppreh/keyboard)
- [PyAutoGUI keyboard functions](https://pyautogui.readthedocs.io/en/latest/keyboard.html)
- [DirectInput vs. Windows Messages](https://docs.microsoft.com/en-us/windows/win32/dxtecharts/taking-advantage-of-high-dpi-mouse-movement)
- [SendInput API documentation](https://docs.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

---

**Decision made by**: Development Team  
**Supersedes**: N/A  
**Related ADRs**: None

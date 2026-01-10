# ADR 003: Mouse Movement for Flight Control Investigation

**Date**: 2026-01-10

**Status**: Rejected

**Context**: The MetalStorm Wingman prototype initially attempted to control airplane pitch (nose up/down) by simulating mouse movement, as many flight games use mouse-look controls where moving the mouse controls the aircraft direction.

## Problem

We need a way to programmatically control the airplane's pitch (nose up/down) and other flight maneuvers. The initial assumption was that the game used mouse-look controls like many modern flight games (e.g., War Thunder, DCS World).

## Investigation

We attempted multiple mouse movement strategies to pitch the airplane upward:

### Attempt 1: Smooth Absolute Movement
```python
# Move to absolute position with smooth interpolation
current_x, current_y = pyautogui.position()
pyautogui.moveTo(current_x, current_y - 96, duration=0.3)
```
**Result**: ❌ Mouse moved smoothly in menus but airplane didn't respond during flight

### Attempt 2: Discrete Relative Steps
```python
# Move in discrete 10-pixel steps with 10ms delays
for i in range(10):
    pyautogui.moveRel(0, -10)  # negative = upward
    time.sleep(0.01)
```
**Result**: ❌ Mouse moved visibly in steps, airplane still didn't respond

### Attempt 3: Very Small Steps for Smoothness
```python
# Move in 1-pixel steps with 1ms delays (100 tiny steps)
for i in range(100):
    pyautogui.moveRel(0, -1)
    time.sleep(0.001)
```
**Result**: ❌ Mouse moved very smoothly but still no airplane response

### Attempt 4: Click-and-Drag
```python
# Hold left mouse button while moving
pyautogui.mouseDown(button='left')
for step in range(steps):
    pyautogui.moveRel(0, -pixels_per_step)
    time.sleep(delay)
pyautogui.mouseUp(button='left')
```
**Result**: ❌ Incorrect assumption - game doesn't use click-drag for flight

### Attempt 5: Controller.continuous_move()
```python
# Use existing continuous_move method (background thread)
ctrl.continuous_move(dx_per_sec=0, dy_per_sec=-192, duration=0.5)
```
**Result**: ❌ Same issue - mouse moves but airplane doesn't respond

## Key Observations

1. **Mouse works in menus**: All movement methods successfully moved the cursor in menus and on desktop
2. **No flight response**: During flight, mouse movement had zero effect on airplane pitch/yaw
3. **LMB works for other actions**: Left mouse button click worked for firing weapons
4. **Manual mouse test failed**: Even manually moving the mouse during flight didn't control the airplane

## Root Cause

**The game does NOT use mouse-look flight controls.** Instead, MetalStorm uses **keyboard-based flight controls**:
- `w` (or numpad `8`) - Pitch up
- `s` (or numpad `2`) - Pitch down
- `a`/`d` or numpad `4`/`6` - Roll/yaw left/right

This is more similar to classic arcade flight games (e.g., Ace Combat) rather than simulator-style mouse-look controls.

## Decision

**Rejected**: Do not use mouse movement for flight control in MetalStorm.

**Alternative approach**: Use keyboard input for flight control (implemented via `keyboard` library - see ADR 002).

## Why Mouse-Look Seemed Reasonable

- Many modern flight games use mouse-look controls
- The presence of mouse button for firing suggested mouse-heavy controls
- Common pattern in PC flight games released in the past decade
- Game had "mouse-look" mentioned in some control schemes

## Actual Control Scheme

After investigation and testing:

```python
# Working solution - keyboard controls
keyboard_module.press('w')    # Pitch up
time.sleep(duration)
keyboard_module.release('w')
```

**Result**: ✅ Airplane responds correctly, pitches up as expected

## Consequences

### Why This Matters

- **Saved development time**: No need to optimize mouse movement algorithms
- **Better understanding**: Confirmed keyboard is the primary input method
- **Correct architecture**: Controller methods can focus on keyboard + mouse button clicks
- **Performance**: Keyboard input is lower latency than continuous mouse polling

### Implementation Impact

- Removed all mouse-movement-based flight control code
- Implemented `Controller.loiter()` using keyboard input
- Mouse remains used for:
  - Weapon firing (LMB clicks)
  - Potential future menu navigation
  - Target selection (if needed)

## Technical Notes

### Why Mouse Movement Didn't Work

Possible technical reasons (not definitively confirmed):

1. **Game design choice**: Developer intentionally disabled mouse-look for arcade-style controls
2. **Flight model**: Game's physics model only responds to discrete key states, not analog mouse input
3. **Control binding**: Mouse not bound to pitch/yaw in the game's control settings
4. **Input mode**: Game might have separate "mouse mode" and "keyboard mode" flight controls

### Mouse Movement Code That Was Tested

All of these patterns were attempted:
- `pyautogui.moveTo(x, y, duration)` - absolute positioning
- `pyautogui.moveRel(dx, dy)` - relative movement
- `pyautogui.drag(dx, dy, duration, button)` - click-drag
- Thread-based continuous movement with small intervals
- Various speeds: slow (0.3s), medium (0.1s), fast (0.001s per step)
- Various distances: 1px, 10px, 96px (1 inch), 200px

None produced any effect on airplane attitude during flight.

## Lessons Learned

1. **Don't assume control schemes**: Test actual game controls before implementing automation
2. **Observe user input first**: Watch what inputs actually affect the game
3. **Check game settings**: Control schemes might be configurable or mode-dependent
4. **Quick validation**: Simple manual test (moving mouse during flight) would have saved hours
5. **Document failed approaches**: Prevents others from repeating same investigation

## Alternative Games Where This Would Work

Mouse-look flight control works in:
- War Thunder (simulator mode)
- DCS World
- Microsoft Flight Simulator (with mouse yoke)
- IL-2 Sturmovik
- X-Plane (with mouse control enabled)

MetalStorm appears to be arcade-style, not simulator-style.

## Future Considerations

- If game adds mouse-look mode in future updates, code patterns from this investigation can be revisited
- For other games, try keyboard controls first before assuming mouse-look
- Consider hybrid approach: keyboard for flight, mouse for aiming weapons

## References

- [PyAutoGUI Mouse Control](https://pyautogui.readthedocs.io/en/latest/mouse.html)
- Original loiter function implementation attempts
- ADR 002: Keyboard Library for Game Input (working solution)

---

**Decision made by**: Development Team  
**Supersedes**: N/A  
**Related ADRs**: ADR 002 (Keyboard Library for Game Input)

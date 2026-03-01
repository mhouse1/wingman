# Respawn Detection to Mission Restart Flowchart

This document illustrates the complete flow from respawn detection through mission restart in the Wingman controller.

## Key States & Timings

- **respawn_delay_after_unlock**: 4 seconds (configured in main.py)
- **restart_retry_interval**: 2 seconds between restart attempts
- **Mission lock wait**: Up to 5 seconds for lock release

## Flow Overview

```mermaid
graph TD
    A["Main Loop: Check game_state"] --> B{"is_respawning?"}
    
    B -->|No| C{"was_respawning?"}
    B -->|Yes| D{"was_respawning?"}
    
    D -->|Yes| E["Log: RESPAWN ACTIVE<br/>Log respawn confidence"]
    D -->|No| F["Log: RESPAWN DETECTED<br/>cancel_mission()"]
    F --> G["Wait for mission lock release<br/>max 5 seconds"]
    G --> H["Set pending_mission_restart = True<br/>Set restart_not_before = now + 4s<br/>Set was_respawning = True"]
    
    E --> I{"pending_mission_restart<br/>AND retry interval?"}
    I -->|No| J["Sleep 1s, continue"]
    I -->|Yes| K{"Mission running?"}
    
    K -->|Yes| L["Update last_restart_attempt<br/>Sleep 1s, continue"]
    K -->|No| M{"time >= restart_not_before?"}
    
    M -->|No| N["Sleep 1s, continue"]
    M -->|Yes| O["Log: Attempting to restart<br/>restart_last_mission()"]
    O --> P{"Restart<br/>succeeded?"}
    P -->|Yes| Q["mission_active = True<br/>pending_mission_restart = False"]
    P -->|No| R["Log: Restart failed<br/>Will retry"]
    
    R --> J
    Q --> J
    L --> J
    N --> J
    
    C -->|Yes| S["Log: Gameplay resumed<br/>ready for missions<br/>Set was_respawning = False"]
    C -->|No| T["Continue main loop"]
    
    S --> U{"pending_mission_restart<br/>AND time >= restart_not_before?"}
    U -->|No| T
    U -->|Yes| V{"Mission running?"}
    V -->|No| W["Log: Attempting restart<br/>restart_last_mission()"]
    V -->|Yes| T
    W --> X{"Restart<br/>succeeded?"}
    X -->|Yes| Y["mission_active = True<br/>pending_mission_restart = False"]
    X -->|No| Y
    Y --> T
    
    J --> Z["Sleep rest of loop<br/>interval"]
    T --> Z
    Z --> A
```

## Key Branches

### 1. **Respawn Detection Phase**
- Triggered when `is_respawning` becomes true for the first time
- Immediately cancels any running mission
- Waits up to 5 seconds for the mission lock to be released
- Sets the restart timer: `restart_not_before = now + 4 seconds`

### 2. **Respawn Active Phase**
- While `is_respawning` is true, logs respawn confidence every loop
- Attempts mission restart when:
  - `pending_mission_restart` flag is set, AND
  - Retry interval (2s) has elapsed, AND
  - Mission lock is free, AND
  - 4-second delay has passed
- Retries on failure (logs "Restart failed, will retry")

### 3. **Gameplay Resume Phase**
- Triggered when `is_respawning` becomes false
- Logs "✓ Gameplay resumed - ready for missions"
- Sets `was_respawning = False` so this only triggers once
- Also attempts restart if delay has expired and lock is free
- Clears `pending_mission_restart` only after successful restart

## Important Notes

- The **4-second delay** is enforced from the moment the mission lock is released, not from the moment respawn was detected
- Both during respawn and after resume, the restart logic checks the same conditions (lock free + delay passed)
- A single `pending_mission_restart` flag persists across both respawn and resume phases
- Once restart succeeds, `pending_mission_restart` is cleared to prevent duplicate restarts

# ADR 006: Multi-Monitor Screen Selection

## Status
Accepted

## Context

The Wingman tool is used in environments where users may have multiple monitors connected to their system. By default, screen capture libraries (such as mss) can capture from any connected monitor, but the application must be able to select the correct monitor for analysis and automation. Hardcoding screen regions or assuming a single monitor leads to errors, confusion, and incorrect behavior in multi-monitor setups.

## Decision

- The configuration file (config.yaml) will include a `monitor` index under the `region` section, allowing users to specify which monitor to capture from (e.g., `monitor: 1` for primary, `monitor: 2` for secondary, etc.).
- The screen capture logic will use this monitor index to select the correct monitor using the mss library's monitor list.
- The region (left, top, width, height) will be interpreted as an offset within the selected monitor, not the global desktop.
- If the monitor index is invalid or not present, the application will default to the primary monitor and warn the user.
- Documentation and test scripts will be updated to explain how to select and test with different monitors.

## Consequences

- Users can reliably select which monitor to capture from, making the tool robust for multi-monitor setups.
- The configuration is more flexible and portable between systems with different monitor arrangements.
- The codebase is easier to maintain and extend for future features (such as auto-detecting active windows or supporting more advanced multi-monitor workflows).

## Related
- ADR 003: Grid-Based Screen Scanning Architecture
- config.yaml documentation
- capture.py implementation

# MetalStorm Wingman (prototype)

Prototype mission automation tool for MetalStorm (PC). Currently supports keyboard input control with preplanned execution steps.

Notes
- This is a prototype that executes predefined mission steps when activated with the begin mission key
- Screen capture and vision-based enemy detection are commented out and planned for future phases
- Tweak `config.yaml` for your display resolution and control settings

Setup a Python virtual environment

Using Astral `uv` (recommended)

If you prefer `uv` (Astral) for reproducible environments, install `uv` then initialize/sync the project:

Install `uv` (examples):

```bash
# recommended: pipx
pipx install uv
# or pip
pip install uv
# or use the standalone installer
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Install a Python version via `uv` (example):

```bash
uv python install 3.12
```

Initialize or convert the project (if you haven't already):

```bash
uv init    # creates pyproject.toml, .python-version, README, etc.
uv add -r requirements.txt   # import existing requirements
uv sync    # create .venv and install deps per lockfile
```

Run the prototype inside the uv-managed environment without activating:

With the game running windowed, adjust `config.yaml` control settings and region, then run:

```bash
uv run python -m wingman.main --dry-run
uv run python -m wingman.main
uv run python -m wingman.main --log-level DEBUG
```

Press the begin mission key (default: Enter) to start executing the mission steps. Press it again to pause.

Or activate the `.venv` created by `uv`:

macOS / Linux / WSL:
```bash
source .venv/bin/activate
```

Windows (PowerShell):
```powershell
.\.venv\Scripts\Activate.ps1
```

Notes:
 - `uv` will create a `.python-version` file to pin the Python version and a `uv.lock` file to lock dependencies.
 - See the Astral `uv` docs at https://docs.astral.sh/uv/ for more on `uv run`, `uv add`, and `uv sync`.

Controls configuration

The prototype currently executes preplanned mission steps (defined in the controller) when activated. Key bindings can be customized in `wingman/main.py`:

- `BEGIN_MISSION_KEY` — toggle mission execution on/off (default: 'enter')
- `CANCEL_MISSION_KEY` — cancel current mission (default: 'end')

The `controls` section in `wingman/config.yaml` supports fire button configuration for future use:

- `controls.left_mouse_button: true` — preferred; fires using left mouse button.
- `controls.fire_button: <key>` — legacy; use `left` for left-click or a keyboard key name to send presses.

If both are present, `left_mouse_button` takes precedence.



# how to quick run

Using powershell
```
powershell -ExecutionPolicy Bypass -File .\run-wingman.ps1
```
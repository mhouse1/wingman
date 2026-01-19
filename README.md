# MetalStorm Wingman (prototype)

Prototype autonomous wingman for MetalStorm (PC). Vision-first approach: screen capture detects enemy HUD markers and issues input actions.

Notes
- This is a prototype using screen capture + OpenCV detection. Later steps can add memory reading (invasive) or a virtual gamepad output.
- Tweak `config.yaml` for your display resolution and the enemy marker color.

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

With the game running windowed, adjust `config.yaml` HSV thresholds and region, then run:

```bash
uv run python -m wingman.main --dry-run
uv run python -m wingman.main
uv run python -m wingman.main --log-level DEBUG
```

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

The prototype supports a boolean `left_mouse_button` option and a legacy `fire_button` string in `wingman/config.yaml`:

- `controls.left_mouse_button: true` — preferred; fires using left mouse button.
- `controls.fire_button: <key>` — legacy; use `left` for left-click or a keyboard key name to send presses.

If both are present, `left_mouse_button` takes precedence.



# how to quick run

Using powershell
```
powershell -ExecutionPolicy Bypass -File .\run-wingman.ps1
```
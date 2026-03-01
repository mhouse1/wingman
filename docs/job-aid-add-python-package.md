# Job Aid: Adding New Python Packages with uv

This guide explains how to add new Python packages to your project using the uv package manager and `pyproject.toml`.

## Quick Start: Add a Package

### Option 1: Use `uv add` (Recommended)
The easiest way to add a package is to use the `uv add` command:

```sh
uv add package-name
```

For example, to add `requests`:
```sh
uv add requests
```

To add a package with a specific version:
```sh
uv add "requests==2.31.0"
```

This automatically:
- Adds the package to `[project] dependencies` in `pyproject.toml`
- Installs it in your virtual environment
- Updates `uv.lock` with the locked version

### Option 2: Add Development Dependencies

For testing tools, linters, or other dev-only packages, use `uv add --group dev`:

```sh
uv add --group dev pytest
uv add --group dev pytest-html
```

This adds packages to `[dependency-groups] dev` in `pyproject.toml`.

## Manual Edit (Advanced)

If you prefer to edit `pyproject.toml` directly:

1. **Production packages**: Add to `[project] dependencies`:
   ```toml
   [project]
   dependencies = [
       "requests>=2.31.0",
       "numpy",
   ]
   ```

2. **Development packages**: Add to `[dependency-groups]`:
   ```toml
   [dependency-groups]
   dev = [
       "pytest>=7.0",
       "pytest-html",
   ]
   ```

3. After editing, run `uv sync --all-groups` to install/update.

## Sync Your Environment

After adding packages, sync the environment:

```sh
# Install all dependencies (production + dev)
uv sync --all-groups

# Or just production dependencies
uv sync
```

## Verify Installation

List all installed packages:
```sh
uv pip list
```

Or check if a specific package is available:
```sh
python -c "import package_name; print(package_name.__version__)"
```

## Best Practices

- **Use `uv add`** instead of manually editing—it keeps `uv.lock` consistent
- **Development tools in `--group dev`**: pytest, black, mypy, etc.
- **Pin versions for reproducibility**: `package==1.2.3` or use constraints like `>=1.0,<2.0`
- **Run `uv sync` before committing** to ensure `uv.lock` is up to date
- **Check in `uv.lock`** to git for reproducible builds

## Common Commands

```sh
# Add a production package
uv add requests

# Add a dev package
uv add --group dev black

# Sync with all groups
uv sync --all-groups

# List installed packages
uv pip list

# Remove a package (edit pyproject.toml, then sync)
# (manually remove from [project] dependencies, then uv sync)
```

## Legacy: requirements.txt

This project has migrated from `requirements.txt` to `pyproject.toml`. The old requirements.txt approach is still supported but not recommended:

```sh
# Old approach (no longer used)
uv pip install -r requirements.txt
```

Use `uv add` and `pyproject.toml` instead.

---
For more details, see the [uv documentation](https://docs.astral.sh/uv/).

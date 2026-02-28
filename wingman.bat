@echo off
REM MetalStorm Wingman Launcher
REM Double-click this file to run the prototype with debug logging
REM
REM Manual run options (if not using this batch file):
REM   uv run python -m wingman.main --dry-run
REM   uv run python -m wingman.main
REM   uv run python -m wingman.main --log-level DEBUG
REM
REM Or using PowerShell directly:
REM   powershell -ExecutionPolicy Bypass -File .\run-wingman.ps1
REM
REM Use this command to run with debug (default):
powershell -ExecutionPolicy Bypass -File "%~dp0run-wingman.ps1" --log-level DEBUG %*

REM Use this command to run normally:
REM powershell -ExecutionPolicy Bypass -File "%~dp0run-wingman.ps1" %*
@echo off
REM Double-click this file to run the PowerShell launcher in a new window and bypass execution policy
REM powershell -ExecutionPolicy Bypass -File "%~dp0run-wingman.ps1" %*

REM Use this command to run with debug
powershell -ExecutionPolicy Bypass -File "%~dp0run-wingman.ps1" --log-level DEBUG %*
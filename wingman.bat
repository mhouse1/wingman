@echo off
REM MetalStorm Wingman Launcher
REM
REM Usage:
REM   wingman.bat                          -> INFO console only (clean output)
REM   wingman.bat --log-file wingman.log   -> INFO console + DEBUG written to wingman.log
REM   wingman.bat --log-level DEBUG        -> DEBUG on console (verbose)
REM
REM Via make:
REM   make r   -> wingman.bat                        (normal)
REM   make rd  -> wingman.bat --log-file wingman.log (debug to file)
REM
powershell -ExecutionPolicy Bypass -File "%~dp0run-wingman.ps1" %*

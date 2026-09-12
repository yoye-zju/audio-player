@echo off
title Audio Subtitle Player - Local Server
cd /d "%~dp0"

rem Prefer Python launcher, then python on PATH
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  set PY=py -3
) else (
  set PY=python
)

echo Starting local server on http://localhost:8765 ...
echo Press Ctrl+C in this window to stop.
%PY% server.py
echo.
echo Server stopped. Press any key to close this window.
pause >nul

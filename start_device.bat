@echo off
setlocal
cd /d "%~dp0"
title Plant Doctor - PC Simulator
if not exist ".venv\Scripts\python.exe" (
    echo Run start.bat once to set up the program, then open this file again.
    pause
    exit /b 1
)
echo Opening Plant Doctor at http://127.0.0.1:8765/device
echo Keep this window open while using the simulator.
".venv\Scripts\python.exe" app.py --device --open-browser
if errorlevel 1 pause

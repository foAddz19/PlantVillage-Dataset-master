@echo off
setlocal
cd /d "%~dp0"
title Leaf Lab
if not exist ".venv\Scripts\python.exe" (
    echo Setting up Python environment...
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import flask, PIL, numpy, sklearn, waitress" >nul 2>&1
if errorlevel 1 (
    echo Installing packages for the first launch. Internet is required.
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 goto failed
)
echo Opening Leaf Lab at http://127.0.0.1:8765
echo Keep this window open while using the app.
".venv\Scripts\python.exe" app.py --open-browser
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Could not start Leaf Lab. See the error above.
echo Python 3.11 or newer and an internet connection are required for setup.
pause
exit /b 1

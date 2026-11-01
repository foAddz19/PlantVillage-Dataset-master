@echo off
setlocal
cd /d "%~dp0"
title Leaf Lab - Paddy Doctor
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import flask, PIL, numpy, waitress, ai_edge_litert" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m pip install -r requirements-pi.txt
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" app.py --model paddy --inference-only --device --open-browser --dataset-dir datasets\paddy-competition %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo Could not start Paddy Doctor. See the error above.
pause
exit /b 1

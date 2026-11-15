@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0.venv\Scripts\pythonw.exe" (
    echo Point Colors has not been set up in this folder.
    echo Double-click Setup Windows.bat first.
    pause
    exit /b 1
)
start "Point Colors" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0rgbpoint_gui.py"
exit /b 0

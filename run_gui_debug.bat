@echo off
setlocal
cd /d "%~dp0"
echo Starting RGB Point Cloud Colorizer (Python debug mode)...
echo.

if not exist "%~dp0.venv\Scripts\python.exe" (
    echo Point Colors has not been set up in this folder.
    echo Double-click Setup Windows.bat first.
    pause
    exit /b 1
)
"%~dp0.venv\Scripts\python.exe" "%~dp0rgbpoint_gui.py"
set "POINTCOLORS_EXIT=%ERRORLEVEL%"
echo.
echo GUI process exited with code %POINTCOLORS_EXIT%.
echo If GUI did not open, send this window text and logs:
echo - rgbpoint_gui_launcher.log
echo - rgbpoint_gui_run.log
pause
exit /b %POINTCOLORS_EXIT%

@echo off
setlocal
cd /d "%~dp0"
echo Starting RGB Point Cloud Colorizer (Python debug mode)...
echo.

set "PY=%LocalAppData%\Programs\Python\Python311\python.exe"
if exist "%PY%" (
    "%PY%" "%~dp0rgbpoint_gui.py"
    goto :afterrun
)

where py >nul 2>nul
if %ERRORLEVEL%==0 (
    py -3 "%~dp0rgbpoint_gui.py"
    goto :afterrun
)

where python >nul 2>nul
if %ERRORLEVEL%==0 (
    python "%~dp0rgbpoint_gui.py"
    goto :afterrun
)

echo Python launcher not found.
echo Install Python 3.11+ and ensure py/python is available in PATH.
goto :finish

:afterrun
echo.
echo GUI process exited with code %ERRORLEVEL%.
echo If GUI did not open, send this window text and logs:
echo - rgbpoint_gui_launcher.log
echo - rgbpoint_gui_run.log
:finish
pause
endlocal

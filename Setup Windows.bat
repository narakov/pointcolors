@echo off
setlocal
cd /d "%~dp0"
echo Setting up Point Colors...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" %*
set "POINTCOLORS_SETUP_EXIT=%ERRORLEVEL%"
echo.
if not "%POINTCOLORS_SETUP_EXIT%"=="0" (
    echo Setup did not finish. See the message above and pointcolors_setup.log.
) else (
    echo Setup complete. Use the Point Colors shortcut on your desktop.
)
pause
exit /b %POINTCOLORS_SETUP_EXIT%

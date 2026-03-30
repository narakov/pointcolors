@echo off
setlocal
cd /d "%~dp0"

set "PYW=%LocalAppData%\Programs\Python\Python311\pythonw.exe"
if exist "%PYW%" (
    start "RGB Point Cloud Colorizer" "%PYW%" "%~dp0rgbpoint_gui.py"
    goto :done
)

where pyw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "RGB Point Cloud Colorizer" pyw -3 "%~dp0rgbpoint_gui.py"
    goto :done
)

where pythonw >nul 2>nul
if %ERRORLEVEL%==0 (
    start "RGB Point Cloud Colorizer" pythonw "%~dp0rgbpoint_gui.py"
    goto :done
)

echo Python GUI launcher not found.
echo Install Python 3.11+ and ensure pythonw/pyw is available in PATH.
:done
endlocal

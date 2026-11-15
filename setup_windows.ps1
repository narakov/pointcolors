param(
    [string]$Python = '',
    [switch]$NoShortcut,
    [switch]$NoLaunch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
# Do not inherit a different interpreter's library paths from a parent terminal.
Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
$pointColorsRoot = $PSScriptRoot
$pointColorsVenv = Join-Path $pointColorsRoot '.venv'
$pointColorsPython = Join-Path $pointColorsVenv 'Scripts\python.exe'
$pointColorsLog = Join-Path $pointColorsRoot 'pointcolors_setup.log'
$pointColorsExitCode = 0
$pointColorsTranscript = $false

function Find-CompatiblePython {
    param([string]$Executable, [string[]]$PrefixArguments = @())
    $pointColorsProbe = "import sys,struct,tkinter,venv,ensurepip; assert (3,12)<=sys.version_info[:2]<(3,15); assert struct.calcsize('P')==8; print(sys.executable)"
    try {
        $pointColorsResult = @(& $Executable @PrefixArguments -c $pointColorsProbe 2>$null)
        if ($LASTEXITCODE -eq 0 -and $pointColorsResult.Count -gt 0) {
            return [string]$pointColorsResult[-1]
        }
    } catch {
        # A missing Python version is expected while trying installed interpreters.
    }
    return $null
}

function Invoke-PythonStep {
    param([string]$Executable, [string[]]$Arguments, [string]$FailureMessage)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw $FailureMessage
    }
}

try {
    Start-Transcript -Path $pointColorsLog -Force | Out-Null
    $pointColorsTranscript = $true
    Write-Host 'Point Colors - Windows setup' -ForegroundColor Cyan
    Write-Host 'Python libraries stay inside this application folder.'
    Write-Host ''
    Write-Host '[1/4] Preparing Python...'

    if (Test-Path -LiteralPath $pointColorsPython -PathType Leaf) {
        if (-not (Find-CompatiblePython -Executable $pointColorsPython)) {
            throw 'The existing .venv is incompatible or damaged. It requires 64-bit Python 3.12-3.14. Recreate the application environment before retrying.'
        }
        Write-Host 'Reusing the existing application environment.'
    } else {
        $pointColorsBasePython = $null
        if ($Python) {
            $pointColorsBasePython = Find-CompatiblePython -Executable $Python
            if (-not $pointColorsBasePython) {
                throw 'The specified Python must be 64-bit Python 3.12-3.14 with tkinter, venv and ensurepip.'
            }
        } else {
            $pointColorsLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
            if ($pointColorsLauncher) {
                foreach ($pointColorsVersion in @('-3.12', '-3.13', '-3.14')) {
                    $pointColorsBasePython = Find-CompatiblePython -Executable $pointColorsLauncher.Source -PrefixArguments @($pointColorsVersion)
                    if ($pointColorsBasePython) { break }
                }
            }
            if (-not $pointColorsBasePython) {
                $pointColorsCommand = Get-Command python.exe -ErrorAction SilentlyContinue
                if ($pointColorsCommand -and $pointColorsCommand.Source -notlike '*\WindowsApps\*') {
                    $pointColorsBasePython = Find-CompatiblePython -Executable $pointColorsCommand.Source
                }
            }
        }
        if (-not $pointColorsBasePython) {
            throw 'Python was not found. Install 64-bit Python 3.12, 3.13 or 3.14 from https://www.python.org/downloads/windows/ with Tcl/Tk enabled, then run Setup Windows.bat again.'
        }
        Write-Host "Using: $pointColorsBasePython"
        Invoke-PythonStep -Executable $pointColorsBasePython -Arguments @('-m', 'venv', $pointColorsVenv) -FailureMessage 'Could not create the application environment.'
    }

    # Verify isolation before installing anything; never install into system Python.
    Invoke-PythonStep -Executable $pointColorsPython -Arguments @('-c', "import sys,pathlib; assert sys.prefix!=sys.base_prefix; assert pathlib.Path(sys.prefix).resolve()==pathlib.Path(sys.argv[1]).resolve()", $pointColorsVenv) -FailureMessage 'The application Python environment is not isolated correctly.'

    Write-Host '[2/4] Installing application libraries...'
    $pointColorsHasPip = $false
    try {
        & $pointColorsPython -m pip --version 2>$null | Out-Null
        $pointColorsHasPip = $LASTEXITCODE -eq 0
    } catch { }
    if (-not $pointColorsHasPip) {
        Invoke-PythonStep -Executable $pointColorsPython -Arguments @('-m', 'ensurepip', '--upgrade') -FailureMessage 'Could not prepare the Python package installer.'
    }
    Invoke-PythonStep -Executable $pointColorsPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:', '-r', (Join-Path $pointColorsRoot 'requirements.txt')) -FailureMessage 'Library installation failed. Check internet access and the messages above, then run setup again.'
    Invoke-PythonStep -Executable $pointColorsPython -Arguments @('-m', 'pip', 'check') -FailureMessage 'Installed application libraries are inconsistent. Run setup again.'

    Write-Host '[3/4] Checking the application and icon...'
    Invoke-PythonStep -Executable $pointColorsPython -Arguments @((Join-Path $pointColorsRoot 'rgbpoint_gui.py'), '--smoke-test') -FailureMessage 'The graphical interface could not start. Check the Python Tcl/Tk installation and the setup log.'

    Write-Host '[4/4] Creating the Point Colors desktop shortcut...'
    if (-not $NoShortcut) {
        Invoke-PythonStep -Executable $pointColorsPython -Arguments @((Join-Path $pointColorsRoot 'app_identity.py'), '--create-shortcut') -FailureMessage 'The desktop shortcut could not be created. The application can still be opened with run_gui.bat.'
    } else {
        Write-Host 'Shortcut creation skipped for this run.'
    }
    Write-Host ''
    Write-Host 'Point Colors is ready.' -ForegroundColor Green
    Write-Host 'Launch it from the desktop shortcut. Setup is not run during normal launches.'
    if (-not $NoLaunch) {
        Start-Process -FilePath (Join-Path $pointColorsVenv 'Scripts\pythonw.exe') -ArgumentList ('"' + (Join-Path $pointColorsRoot 'rgbpoint_gui.py') + '"') -WorkingDirectory $pointColorsRoot
    }
} catch {
    $pointColorsExitCode = 1
    Write-Host ''
    Write-Host ('Setup failed: ' + $_.Exception.Message) -ForegroundColor Red
    Write-Host "Log: $pointColorsLog"
} finally {
    if ($pointColorsTranscript) {
        Stop-Transcript | Out-Null
    }
}
exit $pointColorsExitCode

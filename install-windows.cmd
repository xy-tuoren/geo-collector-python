@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1

set "PATH=%PATH%;%LOCALAPPDATA%\Microsoft\WinGet\Links;%USERPROFILE%\.local\bin"
where.exe uv.exe >nul 2>&1
if errorlevel 1 (
    call :install astral-sh.uv
    if errorlevel 1 goto failed
)
where.exe adb.exe >nul 2>&1
if errorlevel 1 (
    call :install Google.PlatformTools
    if errorlevel 1 goto failed
)

where.exe uv.exe >nul 2>&1
if errorlevel 1 goto refresh_path
where.exe adb.exe >nul 2>&1
if errorlevel 1 goto refresh_path

uv.exe sync --locked --no-dev
if errorlevel 1 goto failed
uv.exe run --locked --no-dev python -c "import onnxruntime, rapidocr"
if errorlevel 1 goto failed
uv.exe run --locked --no-dev geo-collect entries >nul
if errorlevel 1 goto failed

echo Geo Collector installed. See INSTALL.md to connect your phone and start collecting.
popd
pause
exit /b 0

:install
where.exe winget.exe >nul 2>&1
if errorlevel 1 (
    echo WinGet is needed to install %~1. See INSTALL.md for manual setup.
    exit /b 1
)
winget.exe install --id "%~1" --exact --source winget --scope user
exit /b %errorlevel%

:refresh_path
echo Windows has not made uv or adb available yet. Open a new terminal and rerun install-windows.cmd.
:failed
echo Setup could not finish. See above for the error and INSTALL.md for help.
popd
pause
exit /b 1

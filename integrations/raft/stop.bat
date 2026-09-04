@echo off
setlocal

cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop-adapter.ps1" %*
set "STOP_EXIT_CODE=%ERRORLEVEL%"

if not "%STOP_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Stop script exited with code %STOP_EXIT_CODE%.
    pause
)

exit /b %STOP_EXIT_CODE%

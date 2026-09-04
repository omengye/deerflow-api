@echo off
setlocal

cd /d "%~dp0"

if not exist "adapter.toml" (
    echo [ERROR] adapter.toml was not found in:
    echo %CD%
    echo Copy adapter.example.toml to adapter.toml and configure it first.
    pause
    exit /b 1
)

echo Starting Raft-DeerFlow adapter...
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m raft_deerflow_adapter --config ".\adapter.toml" %*
) else (
    where uv >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] Neither .venv\Scripts\python.exe nor uv was found.
        echo Run uv sync in this folder, then try again.
        pause
        exit /b 1
    )
    uv run raft-deerflow-adapter --config ".\adapter.toml" %*
)
set "ADAPTER_EXIT_CODE=%ERRORLEVEL%"

if not "%ADAPTER_EXIT_CODE%"=="0" (
    echo.
    echo [ERROR] Adapter exited with code %ADAPTER_EXIT_CODE%.
    pause
)

exit /b %ADAPTER_EXIT_CODE%

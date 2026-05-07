@echo off
REM Start DeerFlow API service
cd /d "%~dp0"

REM Install dependencies (if not already installed)
if not exist ".venv" (
    echo Installing dependencies with uv...
    uv sync
)

REM Install readabilipy's Readability.js dependencies (Node.js-based extraction)
for /f "delims=" %%d in ('uv run python -c "import readabilipy.simple_json as m, os; print(os.path.join(os.path.dirname(m.__file__), 'javascript'))" 2^>nul') do set READABILIPY_JSDIR=%%d
if defined READABILIPY_JSDIR (
    if not exist "%READABILIPY_JSDIR%\node_modules" (
        where npm >nul 2>&1
        if not errorlevel 1 (
            echo Installing Readability.js dependencies...
            npm install --prefix "%READABILIPY_JSDIR%" --no-audit --no-fund --loglevel=error
        ) else (
            echo Warning: npm not found - Readability.js unavailable, falling back to pure-Python extraction.
        )
    )
)

REM Check for config
if exist ".\config.yaml" (
    set DEER_FLOW_CONFIG_PATH=.\config.yaml
    if not defined DEER_FLOW_HOME set DEER_FLOW_HOME=.\data\deerflow
    echo Using config: .\config.yaml
) else (
    echo No config.yaml found. Copy config.example.yaml and edit it.
    echo    copy config.example.yaml config.yaml
    exit /b 1
)

REM Start uvicorn
if not defined PORT set PORT=8000
if not defined HOST set HOST=0.0.0.0
echo Starting DeerFlow API on http://%HOST%:%PORT%
uv run uvicorn app:app --host %HOST% --port %PORT%

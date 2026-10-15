@echo off
REM ====================================================================
REM CredVault — Development server launcher for Windows 10/11
REM ====================================================================
REM Usage:  start_server.bat [port]
REM   port   HTTP port (default: 8000)
REM
REM Environment variables (optional overrides):
REM   POSTGRES_DB              Database name         (default: credvault)
REM   POSTGRES_USER            Database role          (default: credvault)
REM   POSTGRES_PASSWORD        Role password          (default: empty)
REM   POSTGRES_HOST            PostgreSQL host        (default: 127.0.0.1)
REM   POSTGRES_PORT            PostgreSQL port        (default: 5432)
REM   VAULT_IMPORT_BATCH_ROWS  Rows per import batch  (default: 250000)
REM   VAULT_IMPORT_WORKERS     Parse worker count     (default: 1 on Windows)
REM   VAULT_PROGRESS_INTERVAL  Progress event seconds (default: 1)
REM ====================================================================

setlocal

REM Resolve the directory this script lives in
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

REM Python UTF-8 mode for reliable Unicode handling in credential data
set "PYTHONUTF8=1"

echo.
echo  CredVault Development Server
echo  ============================
echo  Binding to 0.0.0.0:%PORT%
echo  Python UTF-8 mode enabled
if defined VAULT_IMPORT_WORKERS echo  Parse workers: %VAULT_IMPORT_WORKERS%
if defined VAULT_IMPORT_BATCH_ROWS echo  Batch rows: %VAULT_IMPORT_BATCH_ROWS%
echo  Press Ctrl+C to stop.
echo.

REM --- Verify Python is available and meets version requirement ---
python --version >nul 2>&1
if errorlevel 1 (
    echo  ERROR: Python not found.  Install Python 3.11+ and ensure it is on PATH.
    echo  Download: https://www.python.org/downloads/
    pause
    exit /b 1
)

REM --- Verify required dependencies ---
python -c "import django; import psycopg" >nul 2>&1
if errorlevel 1 (
    echo  Installing required dependencies...
    pip install -r requirements.txt --quiet
    if errorlevel 1 (
        echo  ERROR: Failed to install dependencies.
        echo  Run manually:  pip install -r requirements.txt
        pause
        exit /b 1
    )
)

REM --- Apply pending migrations automatically ---
echo  Applying pending migrations...
python manage.py migrate --no-input
if errorlevel 1 (
    echo.
    echo  ERROR: Database migration failed.
    echo  Check PostgreSQL is running and the connection settings are correct:
    echo    POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST / POSTGRES_PORT
    echo.
    echo  Create the database if it does not exist:
    echo    CREATE DATABASE credvault;
    echo.
    pause
    exit /b 1
)

REM --- Run the development server ---
python manage.py runserver 0.0.0.0:%PORT%
if errorlevel 1 (
    echo.
    echo  Server stopped with an error.
    echo.
    pause
)

endlocal

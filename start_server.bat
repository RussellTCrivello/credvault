@echo off
REM ====================================================================
REM CredVault — Development server launcher for Windows 10/11
REM ====================================================================
REM Usage:  start_server.bat [port]
REM   port   HTTP port (default: 8000)
REM
REM Environment variables (optional overrides):
REM   POSTGRES_DB        Database name         (default: credvault)
REM   POSTGRES_USER      Database role          (default: credvault)
REM   POSTGRES_PASSWORD  Role password          (default: empty)
REM   POSTGRES_HOST      PostgreSQL host        (default: 127.0.0.1)
REM   POSTGRES_PORT      PostgreSQL port        (default: 5432)
REM   VAULT_IMPORT_BATCH_ROWS  Rows per import batch (default: 250000)
REM   VAULT_IMPORT_WORKERS     Parse worker count   (default: auto)
REM ====================================================================

setlocal

REM Resolve the directory this script lives in
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8000"

REM Apply long-path support if running on Windows 10+ with Python 3.11+
set "PYTHONUTF8=1"

echo.
echo  CredVault Development Server
echo  ============================
echo  Binding to 0.0.0.0:%PORT%
echo  Press Ctrl+C to stop.
echo.

REM Apply any pending migrations automatically
python manage.py migrate --no-input
if errorlevel 1 (
    echo.
    echo  ERROR: Database migration failed. Check PostgreSQL is running
    echo  and the connection settings are correct.
    echo.
    pause
    exit /b 1
)

REM Run the development server
python manage.py runserver 0.0.0.0:%PORT%
if errorlevel 1 (
    echo.
    echo  Server stopped with an error.
    echo.
    pause
)

endlocal

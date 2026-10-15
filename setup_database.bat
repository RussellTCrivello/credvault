@echo off
REM ====================================================================
REM CredVault — Initial database setup for Windows 10/11
REM ====================================================================
REM Creates the database (if needed), applies all migrations, and
REM creates a superuser for the admin interface.
REM
REM Prerequisites:
REM   - PostgreSQL running and accessible
REM   - POSTGRES_* environment variables set (or defaults are fine)
REM   - Python 3.11+ with Django and psycopg installed
REM ====================================================================

setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHONUTF8=1"

echo.
echo  CredVault — Initial Setup
echo  =========================
echo.

REM Install Python dependencies if needed
echo  Checking Python dependencies...
python -c "import django; import psycopg" 2>nul
if errorlevel 1 (
    echo  Installing dependencies from requirements.txt...
    pip install -r requirements.txt
    if errorlevel 1 (
        echo  ERROR: Failed to install dependencies.
        pause
        exit /b 1
    )
)

REM Create the database if it does not exist (requires psql on PATH)
echo.
echo  Ensuring database exists...
set "PG_DB=%POSTGRES_DB%"
if "%PG_DB%"=="" set "PG_DB=credvault"
set "PG_USER=%POSTGRES_USER%"
if "%PG_USER%"=="" set "PG_USER=credvault"
set "PG_HOST=%POSTGRES_HOST%"
if "%PG_HOST%"=="" set "PG_HOST=127.0.0.1"
set "PG_PORT=%POSTGRES_PORT%"
if "%PG_PORT%"=="" set "PG_PORT=5432"

REM Try to create the database via psql; skip if psql is not available
where psql >nul 2>nul
if not errorlevel 1 (
    psql -h %PG_HOST% -p %PG_PORT% -U postgres -tc "SELECT 1 FROM pg_database WHERE datname='%PG_DB%'" 2>nul | findstr "1" >nul
    if errorlevel 1 (
        echo  Creating database %PG_DB%...
        psql -h %PG_HOST% -p %PG_PORT% -U postgres -c "CREATE DATABASE %PG_DB%;" 2>nul
        if errorlevel 1 (
            echo  WARNING: Could not create database automatically.
            echo  If it does not exist, create it manually:
            echo    CREATE DATABASE %PG_DB%;
        ) else (
            echo  Database %PG_DB% created.
        )
    ) else (
        echo  Database %PG_DB% already exists.
    )
) else (
    echo  psql not found on PATH — skipping database creation check.
    echo  Ensure database %PG_DB% exists on %PG_HOST%:%PG_PORT%.
)

REM Apply all migrations
echo.
echo  Applying migrations...
python manage.py migrate --no-input
if errorlevel 1 (
    echo.
    echo  ERROR: Migration failed. Check your database settings.
    pause
    exit /b 1
)
echo  Migrations applied.

REM Create superuser if none exists
echo.
echo  Checking for admin user...
python -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'admin_LogIntel.settings')
import django
django.setup()
from django.contrib.auth import get_user_model
User = get_user_model()
if not User.objects.filter(is_superuser=True).exists():
    print('NO_SUPERUSER')
else:
    print('HAS_SUPERUSER')
" > "%TEMP%\credvault_user_check.txt" 2>nul
findstr "NO_SUPERUSER" "%TEMP%\credvault_user_check.txt" >nul 2>nul
if not errorlevel 1 (
    echo  Creating superuser...
    python manage.py createsuperuser
) else (
    echo  Admin user already exists.
)
del "%TEMP%\credvault_user_check.txt" 2>nul

echo.
echo  Setup complete.
echo.
echo  Start the server with:  start_server.bat
echo  Or with PowerShell:     .\start_server.ps1
echo.

pause
endlocal

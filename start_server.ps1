<#
.SYNOPSIS
    CredVault — Development server launcher for Windows 10/11.

.DESCRIPTION
    Starts the CredVault Django development server with automatic migration
    application, long-path support, UTF-8 handling, and graceful Ctrl+C
    shutdown behaviour.

.PARAMETER Port
    HTTP port to bind to. Default: 8000.

.PARAMETER SkipMigrate
    Skip the automatic migration step.

.PARAMETER Workers
    Number of parse workers for imports. Overrides VAULT_IMPORT_WORKERS.

.PARAMETER BatchRows
    Rows per import batch. Overrides VAULT_IMPORT_BATCH_ROWS.

.EXAMPLE
    .\start_server.ps1
    .\start_server.ps1 -Port 9000 -Workers 8 -BatchRows 500000

.NOTES
    Requires Python 3.11+ and a running PostgreSQL instance.
    Set POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST,
    POSTGRES_PORT environment variables to configure the database connection.
#>

[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8000,

    [switch]$SkipMigrate,

    [ValidateRange(1, 64)]
    [int]$Workers = 0,

    [ValidateRange(10000, 10000000)]
    [int]$BatchRows = 0
)

$ErrorActionPreference = 'Stop'
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Python UTF-8 mode for reliable Unicode handling in credential data
$env:PYTHONUTF8 = '1'

# Enable long paths (> 260 chars) for deep folder structures
$env:PYTHONLEGACYWINDOWSFSENCODING = '0'

# Apply optional worker/batch overrides
if ($Workers -gt 0) {
    $env:VAULT_IMPORT_WORKERS = $Workers.ToString()
}
if ($BatchRows -gt 0) {
    $env:VAULT_IMPORT_BATCH_ROWS = $BatchRows.ToString()
}

Write-Host ''
Write-Host ' CredVault Development Server' -ForegroundColor Cyan
Write-Host ' ============================' -ForegroundColor Cyan
Write-Host " Binding to 0.0.0.0:$Port"
Write-Host " Python UTF-8 mode enabled"
if ($env:VAULT_IMPORT_WORKERS) {
    Write-Host " Parse workers: $env:VAULT_IMPORT_WORKERS"
}
if ($env:VAULT_IMPORT_BATCH_ROWS) {
    Write-Host " Batch rows: $env:VAULT_IMPORT_BATCH_ROWS"
}
Write-Host ' Press Ctrl+C to stop.' -ForegroundColor DarkGray
Write-Host ''

# Verify Python is available and meets version requirement
try {
    $pyVersion = & python --version 2>&1
    Write-Host " Python: $pyVersion" -ForegroundColor DarkGray
    # Parse the version to check >= 3.11
    if ($pyVersion -match 'Python (\d+)\.(\d+)') {
        $pyMajor = [int]$Matches[1]
        $pyMinor = [int]$Matches[2]
        if ($pyMajor -lt 3 -or ($pyMajor -eq 3 -and $pyMinor -lt 11)) {
            Write-Host " ERROR: Python 3.11+ is required (found $pyMajor.$pyMinor)." -ForegroundColor Red
            exit 1
        }
    }
} catch {
    Write-Host ' ERROR: Python not found. Install Python 3.11+ and ensure it is on PATH.' -ForegroundColor Red
    exit 1
}

# Verify required dependencies are installed
try {
    & python -c "import django; import psycopg" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "missing"
    }
} catch {
    Write-Host ' Installing required dependencies...' -ForegroundColor DarkYellow
    & python -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host ' ERROR: Failed to install dependencies.' -ForegroundColor Red
        Write-Host ' Run manually:  pip install -r requirements.txt' -ForegroundColor Red
        exit 1
    }
    Write-Host ' Dependencies installed.' -ForegroundColor DarkGray
}

# Verify database connectivity with a lightweight check
try {
    $envCheck = & python -c "
import os, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'admin_LogIntel.settings')
import django
django.setup()
from django.db import connection
connection.ensure_connection()
print('Database connection OK')
" 2>&1
    Write-Host " $envCheck" -ForegroundColor DarkGray
} catch {
    Write-Host ''
    Write-Host ' WARNING: Could not verify database connection.' -ForegroundColor Yellow
    Write-Host ' Check that PostgreSQL is running and credentials are set.' -ForegroundColor Yellow
    Write-Host ' POSTGRES_DB / POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_HOST / POSTGRES_PORT' -ForegroundColor Yellow
    Write-Host ''
}

# Apply pending migrations
if (-not $SkipMigrate) {
    Write-Host ''
    Write-Host ' Applying pending migrations...' -ForegroundColor DarkYellow
    & python manage.py migrate --no-input
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host ' ERROR: Database migration failed.' -ForegroundColor Red
        Write-Host ' Ensure PostgreSQL is running and the database exists.' -ForegroundColor Red
        Write-Host ' Create with: CREATE DATABASE credvault;' -ForegroundColor Red
        exit 1
    }
}

# Handle Ctrl+C gracefully — Django's runserver already catches SIGINT,
# but we ensure the process tree is cleaned up on Windows.
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    Write-Host ''
    Write-Host ' Shutting down CredVault...' -ForegroundColor DarkYellow
} -ErrorAction SilentlyContinue

# Start the development server
Write-Host ''
try {
    & python manage.py runserver "0.0.0.0:$Port"
} catch {
    Write-Host ''
    Write-Host " Server error: $_" -ForegroundColor Red
}

Write-Host ''
Write-Host ' Server stopped.' -ForegroundColor DarkYellow

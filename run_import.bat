@echo off
REM ====================================================================
REM CredVault — Bulk folder import launcher for Windows 10/11
REM ====================================================================
REM Usage:  run_import.bat <path_to_folder> [options]
REM
REM Examples:
REM   run_import.bat "C:\data\exports"
REM   run_import.bat "C:\data\exports" --ext txt --fast
REM   run_import.bat "C:\data\exports" --ext csv
REM
REM Environment variables (optional overrides):
REM   VAULT_IMPORT_BATCH_ROWS  Rows per import batch (default: 250000)
REM   VAULT_IMPORT_WORKERS     Parse worker count    (default: 1 on Windows)
REM
REM For maximum throughput on large imports, set:
REM   set VAULT_IMPORT_BATCH_ROWS=500000
REM   set VAULT_IMPORT_WORKERS=4
REM   run_import.bat "C:\data" --fast
REM ====================================================================

setlocal

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHONUTF8=1"

if "%~1"=="" (
    echo.
    echo  Usage: run_import.bat ^<path_to_folder^> [options]
    echo.
    echo  Options:
    echo    --ext EXT     File extension to import (default: txt)
    echo    --fast        Drop search indexes during import for speed
    echo    --quiet       Suppress per-file progress output
    echo.
    echo  Environment variables:
    echo    VAULT_IMPORT_BATCH_ROWS=250000   (rows per batch)
    echo    VAULT_IMPORT_WORKERS=4           (parse workers)
    echo.
    pause
    exit /b 1
)

echo.
echo  CredVault — Bulk Import
echo  =======================
if defined VAULT_IMPORT_BATCH_ROWS (
    echo  Batch size: %VAULT_IMPORT_BATCH_ROWS% rows
)
if defined VAULT_IMPORT_WORKERS (
    echo  Parse workers: %VAULT_IMPORT_WORKERS%
)
echo.

python manage.py importfolder %*
if errorlevel 1 (
    echo.
    echo  Import completed with errors. Check the output above.
    echo.
    pause
    exit /b 1
)

echo.
echo  Import complete.
echo.
endlocal

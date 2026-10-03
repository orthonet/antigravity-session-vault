@echo off
title Antigravity Session Vault - Automated Test Suite
cd /d "%~dp0"
echo =====================================================================
echo Running Antigravity Session Vault Automated Test Suite
echo Includes: Backend Catalog, Sync Engine, WAL Isolation, and Headless UI AppTests
echo =====================================================================
python -m unittest discover tests -v
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Test suite failed! Check error details above.
    pause
    exit /b %ERRORLEVEL%
)
echo.
echo [SUCCESS] All backend and UI integration tests passed successfully.
pause

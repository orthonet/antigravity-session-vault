@echo off
title Stopping Antigravity Session Vault...
echo Stopping any running instance of the dashboard (ports 8501-8505)...
for /l %%p in (8501,1,8505) do (
    for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":%%p" ^| findstr "LISTENING"') do (
        echo Terminating PID %%a on port %%p...
        taskkill /F /PID %%a >nul 2>&1
    )
)
echo Dashboard stopped.
timeout /t 2 >nul

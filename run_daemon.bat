@echo off
title Antigravity Backup Sync Daemon
cd /d "%~dp0"
echo Starting Antigravity continuous background sync daemon...
python core/daemon.py 30
pause

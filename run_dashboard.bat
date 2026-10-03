@echo off
title Antigravity Session Vault Dashboard
cd /d "%~dp0"
echo Starting Antigravity Session Vault on port 8501...
python -m streamlit run app.py --server.port 8501
pause

@echo off
cd /d "%~dp0"
set ENI_POOL_KEY=test
start "ENI-Gateway-9999" /min python -u gateway.py
timeout /t 3 /nobreak >nul
start "ENI-Dashboard-9988" /min python -u dashboard_server.py
echo LAUNCHED

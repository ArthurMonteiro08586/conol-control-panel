@echo off
chcp 65001 >nul 2>&1
title ENI :: Conol One-Click Start
color 0C

cls
echo.
echo  ╔═══════════════════════════════════════════════════╗
echo  ║  ENI :: CONOL ONE-CLICK START                      ║
echo  ║  CDP + Gateway + Dashboard + Browser               ║
echo  ╚═══════════════════════════════════════════════════╝
echo.

echo  [1/4] Запускаю Chrome CDP (порт 9228)...
call chrome_cdp.bat
timeout /t 3 /nobreak >nul

echo  [2/4] Запускаю Gateway (порт 9999)...
start "ENI Gateway" /min cmd /c "set ENI_POOL_KEY=test && python -u gateway.py"
timeout /t 2 /nobreak >nul

echo  [3/4] Запускаю Dashboard (порт 9988)...
start "ENI Dashboard" /min cmd /c "python -u dashboard_server.py"
timeout /t 3 /nobreak >nul

echo  [4/4] Открываю браузер...
start "" http://127.0.0.1:9988

echo.
echo  ═══════════════════════════════════════════════════
echo  ✅ CDP:       http://127.0.0.1:9228
echo  ✅ API:       http://127.0.0.1:9999/v1
echo  ✅ Dashboard: http://127.0.0.1:9988
echo  ✅ API ключ:  test
echo  ═══════════════════════════════════════════════════
echo.
echo  Для управления: start_all.bat
echo  FULL CYCLE:     start_all.bat → [F]
echo.
pause

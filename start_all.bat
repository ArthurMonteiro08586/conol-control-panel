@echo off
chcp 65001 >nul 2>&1
title ENI :: Conol Control Panel v7.0
color 0C

:menu
cls
echo.
echo  ╔══════════════════════════════════════════════════════════════╗
echo  ║         ENI :: CONOL CONTROL PANEL v7.0                      ║
echo  ║         Autoreg + API Gateway + Dashboard                    ║
echo  ╚══════════════════════════════════════════════════════════════╝
echo.
echo  ┌─ QUICK START ──────────────────────────────────────────────┐
echo  │ [1]  Запустить ВСЁ (CDP + Gateway + Dashboard)               │
echo  │ [2]  Только Gateway (API порт 9999)                          │
echo  │ [3]  Только Dashboard (веб-GUI порт 9988)                   │
echo  │ [C]  Запустить Chrome CDP (порт 9228)                       │
echo  ├─ РЕГИСТРАЦИЯ ───────────────────────────────────────────────┤
echo  │ [4]  Зарегистрировать N аккаунтов                           │
echo  │ [5]  Массовая рег из email-очереди                           │
echo  │ [6]  Добавить email в очередь                               │
echo  ├─ ФАРМ ─────────────────────────────────────────────────────┤
echo  │ [7]  Сфармить квесты на всех аккаунтах                      │
echo  ├─ FULL CYCLE ────────────────────────────────────────────────┤
echo  │ [F]  FULL CYCLE: CDP + Reg N + Quests + Gateway             │
echo  ├─ СТАТУС ────────────────────────────────────────────────────┤
echo  │ [8]  Статус системы                                         │
echo  │ [9]  Проверить все аккаунты                                 │
echo  │ [0]  Открыть Dashboard в браузере                           │
echo  ├─ СИСТЕМА ───────────────────────────────────────────────────┤
echo  │ [K]  Kill Gateway + Dashboard                               │
echo  │ [Q]  Выход                                                   │
echo  └─────────────────────────────────────────────────────────────┘
echo.
set /p choice="Выбор > "

if "%choice%"=="1" goto start_all
if "%choice%"=="2" goto start_gw
if "%choice%"=="3" goto start_dash
if "%choice%"=="4" goto reg_n
if "%choice%"=="5" goto reg_multi
if "%choice%"=="6" goto add_email
if "%choice%"=="7" goto quests
if "%choice%"=="8" goto status
if "%choice%"=="9" goto test_all
if "%choice%"=="0" goto open_browser
if /i "%choice%"=="C" goto start_cdp
if /i "%choice%"=="F" goto full_cycle
if /i "%choice%"=="K" goto kill_gw
if /i "%choice%"=="Q" exit
goto menu

:start_all
echo.
echo  [+] Запускаю Chrome CDP (порт 9228)...
call chrome_cdp.bat
timeout /t 2 /nobreak >nul
echo  [+] Запускаю Gateway (порт 9999)...
start "ENI Gateway" /min cmd /c "set ENI_POOL_KEY=test && python -u gateway.py"
timeout /t 2 /nobreak >nul
echo  [+] Запускаю Dashboard (порт 9988)...
start "ENI Dashboard" /min cmd /c "python -u dashboard_server.py"
timeout /t 3 /nobreak >nul
echo  [+] Открываю браузер...
start "" http://127.0.0.1:9988
echo.
echo  ✅ CDP:       http://127.0.0.1:9228
echo  ✅ Gateway:   http://127.0.0.1:9999/v1
echo  ✅ Dashboard: http://127.0.0.1:9988
echo.
pause
goto menu

:start_cdp
echo.
echo  [+] Запускаю Chrome CDP...
call chrome_cdp.bat
timeout /t 3 /nobreak >nul
curl -s http://127.0.0.1:9228/json/version 2>nul | python -c "import sys,json;d=json.load(sys.stdin);print(f'  ✅ CDP ready: {d.get(\"Browser\",\"?\")}')" 2>nul || echo  ❌ CDP не запустился
echo.
pause
goto menu

:full_cycle
echo.
echo  ═════════════════════════════════════════════
echo  FULL CYCLE: Reg + Quests + Gateway
echo  ═════════════════════════════════════════════
echo.
echo  [1/5] Запускаю Chrome CDP...
call chrome_cdp.bat
timeout /t 3 /nobreak >nul
curl -s http://127.0.0.1:9228/json/version 2>nul | python -c "import sys,json;d=json.load(sys.stdin);print(f'  ✅ CDP: {d.get(\"Browser\",\"?\")}')" 2>nul || echo  ⚠️ CDP не ответил
echo.
set /p fc_count="  [2/5] Сколько аккаунтов регать? (default 3): "
if "%fc_count%"=="" set fc_count=3
echo  [2/5] Регистрирую %fc_count% аккаунтов...
python -u eni_conol.py reg %fc_count%
echo.
echo  [3/5] Фарм квестов...
python -u eni_conol.py quests
echo.
echo  [4/5] Запускаю Gateway...
start "ENI Gateway" /min cmd /c "set ENI_POOL_KEY=test && python -u gateway.py"
timeout /t 2 /nobreak >nul
echo  [5/5] Запускаю Dashboard...
start "ENI Dashboard" /min cmd /c "python -u dashboard_server.py"
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:9988
echo.
echo  ═════════════════════════════════════════════
echo  ✅ FULL CYCLE COMPLETE
echo  ✅ API:      http://127.0.0.1:9999/v1
echo  ✅ Dashboard: http://127.0.0.1:9988
echo  ✅ API Key:  test
echo  ═════════════════════════════════════════════
echo.
pause
goto menu

:start_gw
echo.
echo  [+] Запускаю Gateway...
set ENI_POOL_KEY=test
python -u gateway.py
pause
goto menu

:start_dash
echo.
echo  [+] Запускаю Dashboard...
python -u dashboard_server.py
pause
goto menu

:reg_n
echo.
set /p regcount="Сколько аккаунтов注册ить? (default 5): "
if "%regcount%"=="" set regcount=5
echo  [+] Регистрирую %regcount% аккаунтов...
python -u eni_conol.py reg %regcount%
echo.
pause
goto menu

:reg_multi
echo.
set /p pe="Аккаунтов на email? (default 3): "
if "%pe%"=="" set pe=3
set /p mp="Параллельных потоков? (default 2): "
if "%mp%"=="" set mp=2
echo  [+] Массовая регистрация: %pe% acc/email, %mp% потоков...
python -u multi_reg.py %pe% %mp%
echo.
pause
goto menu

:add_email
echo.
set /p emailaddr="Email для очереди: "
if "%emailaddr%"=="" goto menu
echo  [+] Добавляю %emailaddr% в очередь...
python -u eni_conol.py queue add %emailaddr%
echo.
pause
goto menu

:quests
echo.
echo  [+] Запускаю фарм квестов на всех аккаунтах...
python -u eni_conol.py quests
echo.
pause
goto menu

:status
echo.
echo  ─── Статус системы ────────────────
python -u eni_conol.py status
echo.
echo  ─── Gateway Health ────────────────
curl -s http://127.0.0.1:9999/health 2>nul
echo.
echo  ─── Dashboard ─────────────────────
curl -s http://127.0.0.1:9988/api/state 2>nul | python -c "import sys,json; d=json.load(sys.stdin); print(f'Dashboard: {d[\"pool\"][\"total\"]} accounts, {d[\"queue\"][\"total\"]} queued')" 2>nul || echo Dashboard: не запущен
echo.
pause
goto menu

:test_all
echo.
echo  [+] Проверяю все аккаунты...
python -u eni_conol.py test
echo.
pause
goto menu

:open_browser
echo  [+] Открываю Dashboard...
start "" http://127.0.0.1:9988
goto menu

:kill_gw
echo.
echo  [+] Останавливаю Gateway...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :9999 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
echo  [+] Останавливаю Dashboard...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :9988 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
echo  [+] Останавливаю Chrome CDP...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :9228 ^| findstr LISTENING') do taskkill /PID %%a /F >nul 2>&1
echo  ✅ Остановлено.
echo.
pause
goto menu

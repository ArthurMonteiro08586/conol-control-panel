@echo off
rem PC Agent + Cloudflare Tunnel launcher
rem Поднимает исполнитель на ПК и пробрасывает его наружу через trycloudflare (QUIC ломается на этом провайдере — только http2!)
rem ВАЖНО: --config пустой, иначе cloudflared подхватывает ~/.cloudflared/config.yml с named tunnel и quicktunnel-домен отдаёт 404.

cd /d "%~dp0"

echo [1/2] Starting PC agent on :18099 ...
start "pc-agent" /min cmd /c ""C:\Users\User\AppData\Local\Programs\Python\Python311\python.exe" pc_agent_server.py --print-token"

echo [2/2] Starting cloudflared tunnel (http2, empty config) ...
if not exist "%TEMP%\cf_empty.yml" echo {} > "%TEMP%\cf_empty.yml"
start "cf-tunnel" /min cmd /c ""C:\Program Files (x86)\cloudflared\cloudflared.exe" tunnel --config "%TEMP%\cf_empty.yml" --no-autoupdate --metrics 127.0.0.1:20242 --protocol http2 --url http://127.0.0.1:18099"

echo.
echo Wait ~15s, then grab tunnel URL:
echo   findstr trycloudflare "%TEMP%\..\..\Users\User\tmp\cf_tunnel5.log"
echo Token: type agent_token.txt
pause

# Reverse Bridge — conol-агент видит твой ПК

ДВЕ схемы (обе рабочие, проверено 07.10.2026):

## Схема A (основная, E2E verified): CF Tunnel + промпт-инжекция
conol-модели исполняют tool-вызовы в СВОЕЙ e2b-песочнице (hostname e2b.local),
поэтому нативный tool-use через гейтвей не форсит локальное исполнение.
Рабочий путь: агенту в промпт даётся curl-инструкция + URL туннеля + токен.

```
[conol-агент в e2b] --curl HTTPS--> [xxx.trycloudflare.com] --tunnel--> [pc_agent_server.py :18099 на ПК]
```

E2E пруф: /info hostname=Nikita (НЕ e2b.local), /exec вернул 878 .session,
/screenshot 433KB PNG через туннель. gpt-5.6-luna выполнил все 3 шага (163s).

Запуск (Windows):
```bash
# 1. исполнитель на ПК
python pc_agent_server.py --print-token     # токен в agent_token.txt

# 2. туннель (ВАЖНО: --protocol http2 — QUIC рвётся на этом провайдере;
#    --config ПУСТОЙ yml — иначе подхватится ~/.cloudflared/config.yml
#    с named tunnel и quicktunnel-домен будет отдавать 404!)
echo {} > cf_empty.yml
cloudflared tunnel --config cf_empty.yml --no-autoupdate --metrics 127.0.0.1:20242 \
  --protocol http2 --url http://127.0.0.1:18099
# URL туннеля в логе: https://xxx.trycloudflare.com

# или всё сразу: start_bridge.bat
```

Endpoints pc_agent_server (POST + заголовок X-Agent-Token):
/exec, /read, /write (выкл по умолчанию), /list, /screenshot, /info, GET /health

Пример промпта для conol-агента — см. `_e2e_cf.py` в корне проекта.

## Схема B (эксперимент): XML tool-use через гейтвей
`agent_bridge.py` шлёт OpenAI tools через conol_gateway :9999 (гейтвей
конвертит в XML-промпт). Работает только если модель честно эмитит
`<function_call>` — gpt-5.6-luna/deepseek-v4-flash склонны исполнять в своей
песочнице вместо вызова инструмента. Держим как запасной путь.

```bash
python conol_gateway.py --host 127.0.0.1 --port 9999   # ENI_POOL_KEY=***
cp config.example.json config.json
python agent_bridge.py --selftest
python agent_bridge.py "задача"
```

## Безопасность

- `allow.shell / allow.write / allow.screenshot` — флаги в config.json.
  По умолчанию запись файлов ВЫКЛЮЧЕНА.
- `workdir` — песочница для файловых операций, пути вне неё отклоняются.
- config.json с ключами НЕ коммитится (.gitignore).
- shell_timeout 120s, вывод режется до 20KB.

## Требования

- Python 3.11, `pip install httpx pillow`
- Живой conol_gateway.py :9999 (или любой OpenAI-compat endpoint с tools)
- Скриншоты: Windows (PIL ImageGrab)

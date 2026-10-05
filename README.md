# ENI :: Conol Control Panel v7.2

Автоматическая регистрация аккаунтов conol.ai + OpenAI-совместимый API-шлюз + веб-дашборд.

## Quick Start

```bat
start.bat      ← One-click: Gateway + Dashboard + браузер
start_all.bat  ← Меню управления (регистрация, квесты, статус)
```

После запуска:
- **Dashboard:** http://127.0.0.1:9988
- **API:** http://127.0.0.1:9999/v1
- **API ключ:** из `config.json` → `gateway.api_key`

## Что нового в v7.2

- **Эскалация капча-цепочки** — когда сервер отбивает токен провайдера
  (`CAPTCHA_VERIFICATION_FAILED` 403), следующая попытка автоматически скипает
  этого провайдера и уходит на платный AntiCaptcha (`solve(action, skip={...})`,
  `LAST_PROVIDER`). Раньше холодный фри-токен жёг все 3 попытки.
- **Фикс gmail-алиаса** — `email_prefix` в конфиге уже несёт `+conol`,
  рег больше не выдаёт двойной `+conol+conol`.
- **⚠️ Статус рега (2026-10-03): conol.ai перевёл reCAPTCHA в Enterprise-режим.**
  Sitekey прежний (`6Lc3wmAt…`), токены минтятся с префиксом `0c` (Enterprise),
  но бэкенд отбивает их 403 даже при **полном submit живой формы в реальном
  Chrome** (проверено: человеческий submit → `CAPTCHA_VERIFICATION_FAILED`).
  Это серверный скоринг по репутации IP/браузера, не баг клиента. Для рега
  нужен чистый residential-IP; существующий пул полностью жив.
- **Квест-фарм проверен на новом сайте** — `/api/quests` + agent-sessions
  работают: на свежем аккаунте `note_written_by_agent` и `memory_written`
  flipped → completed (+300cr каждый). ~8 квестов × 300cr доступны на акк.

## Что было в v7.1

- **Фри-капча (Chrome CDP)** — reCAPTCHA v3 токены минтятся в локальном Chrome
  с тёплым профилем (~0.5s/токен, $0). Платный AntiCaptcha остался фолбэком.
  Порядок провайдеров: `config.json` → `captcha.providers`.
- **Второй почтовый провайдер: t-online.de** — выделенные ящики из дампа
  (17k+ адресов, IMAP `secureimap.t-online.de`). Занятые на conol адреса
  автоматически скипаются (hop до 12 на аккаунт). Gmail plus-alias остался.
- **Фикс стриминга** — tool-use в `stream=true` больше не течёт сырым
  `<function_call>` XML в content-дельты.
- **Меню/дашборд** — выбор провайдера и фри-капчи прямо из UI и start_all.bat.

## Компоненты

| Файл | Что делает |
|------|-----------|
| `conol_gateway.py` | OpenAI-совместимый шлюз v4 (stdlib, порт 9999): 14 моделей (канал 865), stream + tool-use (XML-эмуляция), ротация пула, rate-limit backoff |
| `conol_register.py` | Авторег: капча-цепочка, email-провайдеры, hop через занятые адреса, квест-кредиты |
| `conol_captcha.py` | Решатель reCAPTCHA v3: chrome_cdp (free) → anticaptcha (paid) |
| `conol_emails.py` | Провайдеры почты: gmail (+alias) / t-online.de (выделенные ящики), IMAP-поллер verify-ссылок |
| `conol_refresh.py` | Авто-refresh сессионных токенов пула |
| `conol_scale.py` | Супервизор масштабирования пула |
| `dashboard_server.py` | Веб-дашборд (порт 9988) |
| `conol_quest_farm.py` | Resumable квест-фармер всего пула (4 easy-квеста × 300cr на акк) |
| `deploy_conol_gateway.py` | Деплой шлюза на VPS: systemd, smoke-тест, синк пула, канал new-api |
| `start_all.bat` | Меню управления v7.2: quick start, рег, фарм, VPS (deploy/sync/health), full cycle |
| `conol_infer.py` | Низкоуровневый клиент conol.ai API (sessions/SSE) |
| `config.example.json` | Шаблон конфига (скопируйте в `config.json` и заполните) |

## Регистрация

```bash
# авто-провайдер (gmail), капча-цепочка cdp→anticaptcha
python -X utf8 conol_register.py --count 5

# только t-online.de ящики
python -X utf8 conol_register.py --count 5 --provider tonline

# только фри-капча (без платного AntiCaptcha вообще)
python -X utf8 conol_register.py --count 5 --free-captcha

# всё вместе: бесплатный конвейер
python -X utf8 conol_register.py --count 10 --provider tonline --free-captcha
```

Замеры (2026-10-03): t-online + фри-капча, полный цикл (register → verify-email →
sign-in → balance) = **19s, 600 кредитов, $0 расходов**; 5 solves через тёплый Chrome.

## Конфигурация (config.json)

| Секция | Ключи |
|--------|-------|
| `conol` | `base_url`, `password`, `site_key` |
| `gmail` | `address`, `app_password` |
| `captcha` | `providers` (порядок цепочки), `anticaptcha_keys`, `cdp_port` (9228) |
| `emails.tonline` | `enabled`, `creds_file` (формат `email:password` построчно), `state_file`, `imap_host` |
| `gateway` | `port` (9999), `api_key`, `host` |

Секреты читаются через `conol_secrets.py` (config.json → env `CONOL_*`).
`config.json` в git не попадает; для деплоя используйте `config.example.json`.

## Dashboard — что видно

- **Статус Gateway** в реальном времени (online/offline, активных акков, запросов)
- **Таблица аккаунтов** — email, имя, статус cookies, credits, дата регистрации
- **Управление регом** — кол-во, провайдер почты (auto/gmail/t-online), чекбокс фри-капчи
- **Email очередь**, **Модели**, **API Test** (tool-use в один клик), **Логи**

## API endpoints (шлюз)

| Method | Path | Описание |
|--------|------|----------|
| GET | `/health` | Статус шлюза + пул |
| GET | `/v1/models` | Список моделей |
| POST | `/v1/chat/completions` | Chat: stream + non-stream + tools (OpenAI tool_choice семантика) |
| GET | `/pool/stats` | Счётчики по аккаунтам |
| POST | `/pool/reload` | Перечитать пул (подхватывает новые реги) |

Tool-use: запрос с `tools` → шлюз инжектит XML-протокол в системный блок,
парсит `<function_call>` из ответа модели и возвращает нативные
`tool_calls` + `finish_reason: "tool_calls"`. Системный промпт запроса
сохраняется (XML-протокол дописывается к нему, не заменяет).

## Квест-фарм

```bash
python -X utf8 conol_quest_farm.py   # все live-акки, 4 easy-квеста x 300cr
```

- Resumable: прогресс в `conol_quest_farm_state.json` (gitignored), можно
  убить и перезапустить — продолжит с того же места.
- Акки с уже готовыми квестами скипаются мгновенно (1 GET /api/quests).
- Квест = agent-сессия (gpt-5.6-luna, effort low) + poll `/api/quests` до
  `completed: true`. Проверено на новом сайте 2026-10-04: +300cr/квест.
- Прогон 2026-10-05: 83,700cr за ~2.2ч (92/271 акков, идёт дальше —
  resumable, state переживает убийство процесса).

## Деплой на VPS (фарм-сервер)

```bash
python deploy_conol_gateway.py --selfcheck          # офлайн-проверка скрипта
python deploy_conol_gateway.py                      # dry run плана
python deploy_conol_gateway.py --apply              # установка + systemd + health
python deploy_conol_gateway.py --apply --smoke      # + реальный инференс (stream+non-stream)
python deploy_conol_gateway.py --sync --apply       # запушить пул + /pool/reload
python deploy_conol_gateway.py --channel --apply    # создать/обновить канал в new-api + test
```

Целевой хост: VPS 13.143.162.135 (new-api за api.reformboss.com), через
`tmp/_gwssh.py`. Сервис: `/etc/systemd/system/conol-pool.service` →
`/opt/conol-pool/` (конфиги 0600, ключ генерится на VPS `openssl rand -hex 24`).
На VPS уходит ТОЛЬКО проекция пула (name/session_token/expires/status/credits) —
cookies и email не покидают машину. Канал new-api: `conol-farm-pool`, группа
`conol` (изоляция от default), 14 моделей. Admin-токен new-api берётся из
`/root/.secrets/newapi_admin_token` (легаси `/opt/grok-gateway/admin_token.txt` — фолбэк).

## Порты

| Сервис | Порт |
|--------|------|
| API Gateway | 9999 |
| Dashboard | 9988 |
| Chrome CDP | 9228 |

## Тесты

```bash
python test_conol_pool.py          # 559 проверок пула/ротации
python test_gateway_tools.py       # tool-use шлюза
python test_conol_pid_slot.py      # pid-lock слоты
python test_conol_refresh_mutex.py # mutex refresh (25 проверок)
python test_conol_scale_slot.py    # scale-дефицит логика
```

Все plain-скрипты (не pytest), exit 0 = pass. Прогон 2026-10-05: selfcheck 79/79 (all blocks passed), gateway_tools 18/18, остальные 559/559, 25/25, 0/0.

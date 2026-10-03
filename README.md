# ENI :: Conol Control Panel v7.0

Автоматическая регистрация аккаунтов conol.ai + OpenAI-совместимый API-шлюз + веб-дашборд.

## Quick Start

```bat
start.bat      ← One-click: Gateway + Dashboard + браузер
start_all.bat  ← Меню управления (регистрация, квесты, статус)
```

После запуска:
- **Dashboard:** http://127.0.0.1:9988
- **API:** http://127.0.0.1:9999/v1
- **API ключ:** `test`

## Компоненты

| Файл | Что делает |
|------|-----------|
| `start.bat` | One-click запуск всего (Gateway + Dashboard + браузер) |
| `start_all.bat` | Меню управления (12 опций) |
| `gateway.py` | OpenAI-совместимый API-шлюз (порт 9999) |
| `dashboard_server.py` | Веб-дашборд (порт 9988) |
| `eni_conol.py` | CLI: регистрация, квесты, статус, тест |
| `multi_reg.py` | Массовая регистрация из email-очереди |
| `config.json` | Конфигурация (gmail, порты, пароли) |

## Dashboard — что видно

- **Статус Gateway** в реальном времени (online/offline, активных акков, запросов)
- **Таблица аккаунтов** — email, имя, статус cookies, credits, дата регистрации
- **Email очередь** — добавление/удаление, статусы
- **Модели** — 18 моделей conol.ai (gpt-5.6-luna, deepseek-v4-pro, claude-opus-4-8, ...)
- **Логи** — gateway, reg, multi_reg, quests — в реальном времени
- **Кнопки управления** — старт Gateway, регистрация, квесты, reload пула

## CLI команды

```bash
# Статус
python eni_conol.py status

# Регистрация
python eni_conol.py reg 5          # 5 новых аккаунтов
python multi_reg.py 3 2            # 3 acc/email, 2 потока из очереди

# Квесты
python eni_conol.py quests         # фарм на всех аккаунтах

# Проверка
python eni_conol.py test           # проверить все акки
```

## Подключение к OMP / Hermes

```yaml
custom_providers:
  - name: conol
    provider: openai
    base_url: http://127.0.0.1:9999/v1
    api_key: test
    models:
      gpt-5.6-luna: {ctx: 200000, cost: {input: 0, output: 0}}
      deepseek/deepseek-v4-pro: {ctx: 200000, cost: {input: 0, output: 0}}
      claude-opus-4-8: {ctx: 200000, cost: {input: 0, output: 0}}
      gemini-3-pro: {ctx: 200000, cost: {input: 0, output: 0}}
```

## API endpoints

| Method | Path | Описание |
|--------|------|----------|
| GET | `/health` | Статус шлюза |
| GET | `/v1/models` | Список моделей |
| POST | `/v1/chat/completions` | Chat (stream + non-stream + tools) |
| GET | `/queue` | Email очередь |
| POST | `/queue/add` | Добавить email |
| POST | `/queue/remove` | Удалить email |
| POST | `/pool/reload` | Перезагрузить пул |
| GET | `/pool/stats` | Статистика пула |

## Порты

| Сервис | Порт |
|--------|------|
| API Gateway | 9999 |
| Dashboard | 9988 |
| Chrome CDP | 9228 |

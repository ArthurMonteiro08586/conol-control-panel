# Reverse Bridge — conol-агент видит твой ПК

Реверс-схема: мозг агента на conol-серверах (бесплатные кредиты из пула),
исполнитель — этот воркер на ПК. ПК сам ходит наружу, открытых портов нет.

## Как работает

```
[conol.ai пул 271 акк] <- cookie/gateway
        ^
        |  /v1/chat/completions + tools (HTTP outbound)
        v
[agent_bridge.py на ПК]  ->  shell_exec / file_read / file_list / screenshot
                              file_write (disabled by default)
```

Модель получает tool-схемы, шлёт tool_calls, воркер исполняет локально
и возвращает результат. Цикл до финального ответа. Скриншот отдаётся
мультимодально (data:image/png;base64) — модель буквально видит экран.

## Быстрый старт

```bash
# 1. поднять гейтвей поверх пула (из корня проекта)
python conol_gateway.py            # :9999

# 2. конфиг
cp config.example.json config.json   # config.json в .gitignore!

# 3. самотест инструментов (без модели)
python agent_bridge.py --selftest

# 4. задача
python agent_bridge.py "посчитай сколько .session файлов в C:/Users/User/.orca/sessions"

# 5. интерактив с памятью диалога
python agent_bridge.py --repl
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

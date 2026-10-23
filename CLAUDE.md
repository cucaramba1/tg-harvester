# TG Harvester

Собирает посты из Telegram-каналов (`config/channels.yaml`), фильтрует локально, ночью разбирает лучшие через `claude -p`, присылает черновики в бота, после ✅ пишет в Obsidian. Спека и план: `docs/`. Состояние: `HANDOFF.md`.

## Команды
- `scripts/setup.sh status | logs | restart | night-now | stop`. Канал добавляется строкой в `config/channels.yaml` и `restart`.
- Тесты: `uv run pytest -q` (84 проходят).

## Правила
- Вызов Claude только через `harvester/brain.py`: модель `sonnet` по умолчанию (`CLAUDE_BATCH_MODEL`), лимит вызовов за ночь `max_calls_per_night` в `config/settings.yaml`.
- Когда лимит Claude кончается, ночь не обрывается: остаток разбирает локальный FreeLLMAPI (`router_fallback` в `config/settings.yaml`, `run_router` в `brain.py`, модель `FREELLM_MODEL`, по умолчанию `gemini-3.8-flash`). В запасном режиме не передаются «Мои цели» и картинки. Роутер должен работать на `127.0.0.1:3001` (launchd `com.cucaramba.freellmapi`). Замер на 99 постах: Sonnet 88, Gemini 82, Haiku 80 совпадений с решениями Некита.
- Telethon-сессия `data/harvester.session` своя. Не использовать её параллельно с живым процессом без нужды и не копировать.
- Перед правкой заметок делаются снапшоты `data/snapshots/<дата>/`. Заметки без источников и авторов в теле (правило vault).

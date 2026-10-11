# TG Harvester

Собирает посты из Telegram-каналов (`config/channels.yaml`), бесплатно фильтрует их на маке, ночью разбирает лучшие через `claude -p` и присылает черновики в бота. После ✅ пишет в Obsidian.

Спека: `docs/specs/2026-10-10-tg-harvester-design.md`. План: `docs/plans/2026-10-10-tg-harvester.md`.

## Запуск с нуля

1. `cp .env.example .env`, заполнить `BOT_TOKEN` (новый бот у @BotFather), `TG_API_ID` и `TG_API_HASH` (те же, что у telegram-mcp).
2. `uv sync && uv run python -m harvester login`: телефон, код из Telegram, пароль 2FA.
3. `uv run python -m harvester section Музыка`: раздел L2 «Музыка».
4. `scripts/setup.sh install`.

## Команды бота

- `/status`: сколько постов в каком статусе, фазы каналов.
- `/pilot`: 50 случайных постов на разметку 👍/👎.
- `/calibrate`: подобрать пороги фильтра по разметке.

## Обслуживание

- `scripts/setup.sh status | logs | restart | night-now | stop`.
- Лимит Claude за ночь: `max_calls_per_night` в `config/settings.yaml`.
- Добавить канал: строка в `config/channels.yaml`, потом `scripts/setup.sh restart`.
- Снапшоты заметок до правок: `data/snapshots/<дата>/`, хранятся 30 дней.

# Feature: journal

Вкладка «Журнал» (чёрный ящик) и весь API `/api/journal/*` — через
манифест `feature.yaml` (Этапы 4–5, ARCHITECTURE-V2 §2.2, §3.10).

## Что делает FeatureLoader

- монтирует API-роутер: `api_router: api.py:create_router` → мост к
  `src/journal/api.py:build_router()` — 18 эндпоинтов:
  - события: `GET /events`, `GET /event/{id}`, `GET /event/{id}/diff`
  - поиск/хронология: `GET /search`, `GET /timeline`
  - граф: `GET /graph` (json/mermaid), `GET /provenance?target=`
  - откат: `POST /rollback` (dry_run|execute, force), `POST /rollback/verify`
  - replay: `POST /replay/plan`, `POST /replay/execute`, `GET /replay/export`
  - ретенция: `GET /stats`, `GET /retention`, `POST /retention/sweep`
  - экспорт/отчёты: `GET /export`, `GET /reports`, `GET /reports/{name}`
- регистрирует вкладку: `GET /api/features` отдаёт `js_url`
  (`/features/journal/ui.js`), `app.js` подгружает скрипт сам;
- `ui.js` — обёртка, подключающая самодостаточный `/static/journal.js`.

## Бэкенд (Этап 5: единое поколение)

SQLite-поколение (Task 2): stdlib-only — SQLite WAL + FTS5 + JSONL +
тени файлов (откат с конфликт-детекцией). Точка входа — мягкая
интеграция `src/journal/integration.py` (`init_journal`,
`instrument_mcp_manager` — перехват всех tool calls, `shutdown_journal`).
PostgreSQL-бэкенд v1.0 выведен в `attic/journal-pg/`.

## Требования

- нет внешних зависимостей: sqlite3/hashlib/zipfile — stdlib.
- журнал включён всегда; при отказе инициализации старт продолжается
  без журнала (мягкое предупреждение в логе).
- включение/настройка — секция `journal:` в settings.yaml / env
  (см. `src/journal/config.py` и docs/JOURNAL.md).

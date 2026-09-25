# Attic — чердак проекта

Сюда перенесён код, выведенный из эксплуатации при **Этапе 5**
(ARCHITECTURE-V2 §3.10 «Чистка и обучение»). Файлы не импортируются
системой и не участвуют в сборке; хранятся для истории и археологии.

## journal-pg/ — PostgreSQL-поколение журнала (v1.0)

Первая реализация «чёрного ящика» поверх PostgreSQL
(`DatabasePool`, схема `journal.*`):

| Файл | Что было |
|---|---|
| `models.py` | pydantic-модели Action/Snapshot/Checkpoint |
| `storage.py` | BlobStore — контент-addressed хранение в PG |
| `action_graph.py` | ActionGraph — граф зависимостей действий в PG |
| `rollback_v1.py` | RollbackManager — откат по снапшотам PG |
| `replay_v1.py` | ReplayManager — воспроизведение сессий по PG |
| `retention_v1.py` | RetentionManager v1 (PG) |

**Почему выведено:** поколение не совпадало с актуальным рекордером
(сигнатура `JournalRecorder(cfg, project_root=...)` против
`JournalRecorder(pool, blobs)`) — блок инициализации в `main.py` падал в
`try/except` и журнал молча спал; зависимость от psycopg ломала импорт
пакета без БД; тесты/CLI/MCP-сервер/UI были написаны под SQLite-поколение.

**Победитель:** SQLite-поколение (Task 2, `src/journal/`) — stdlib-only
(SQLite WAL + FTS5 + JSONL + тени файлов), 42 smoke-теста, конфликт-детекция
при откате, ретенция по свободному месту.

## mcp-journal-v1/ — MCP-сервер журнала v1.0

`server.py` + `server.yaml` из `src/mcp_servers/journal/` — python-MCP
сервер поверх PostgreSQL-поколения (15 инструментов, включая
checkpoint-*). Актуальный MCP журнала декларативен:
`mcp_servers/journal/server.yaml` → `python -m src.journal.mcp_server`
(8 инструментов, SQLite-поколение).

## agents-legacy/ — классы-агенты до декларативной эры

`data_agent.py, deepseek_agent.py, document_agent.py, file_agent.py,
git_agent.py, image_agent.py, media_agent.py, mysql_agent.py,
onec_agent.py, postgres_agent.py, shell_agent.py` + мусорный каталог
`init-junk-dir/`.

Классы (`FileAgent`, `MySQLAgent`, …) не использовались нигде, кроме
импорта в `src/agents/__init__.py`; система живёт на
`BaseAgent` + декларациях `agents/*/agent.yaml` (37 агентов).
Живые компоненты пакета — `src/agents/base.py` и `src/agents/runtime.py`.

## Правило

Новый мёртвый код не копится в `src/` — переносится сюда с пояснением
в этом README (что, почему, что победило). Восстановление — обратный
перенос файла и импорта.

# MCP-серверы

**MCP (Model Context Protocol)** — стандарт, позволяющий LLM вызывать внешние
инструменты. Для каждого типа задач создаётся отдельный MCP-сервер, который
предоставляет агенту только безопасный и ограниченный набор операций.

В проекте сейчас **39 MCP-серверов** (полный список с описаниями —
[STRUCTURE.md](STRUCTURE.md), состояние в реальном времени — ⚙️ → «MCP»).
Ниже — базовые серверы, с которых начинался проект.

---

## MCP-сервер для файлов (Filesystem)

Даёт агенту возможность читать и изменять код проекта. Должен быть максимально безопасным.

- **Инструменты (33):** базовые — `read_file`, `write_file`, `apply_patch`,
  `list_files`, `list_tree`, `search_in_files`, `delete_file`,
  `read_file_range`, `move_file`, `rename_file`, `copy_file`, `current_root`;
  остальные — производные (пакетное чтение, статистика и т.п.)
- **Безопасность:** все пути проверяются на принадлежность корневой директории проекта (sandboxing).
  Для правки используется `apply_patch` (unified diff), а не прямая перезапись — это минимизирует риски.
  Учитывается `.gitignore` (отключается `respect_gitignore: false`).
- **Игнорируются:** `.git`, `.venv`, `venv`, `__pycache__`, `node_modules`, `.idea`,
  `.vscode`, `dist`, `build`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `target`, `.next`.

---

## MCP-серверы для баз данных (MySQL/SQLite и PostgreSQL)

Хотя для разных СУБД можно было бы использовать один сервер, они разделены для изоляции прав
и корректной работы с диалектами SQL.

- **Инструменты чтения:**
  - `list_schemas`
  - `list_tables`
  - `describe_table`
  - `run_query` (только `SELECT`, `WITH`, `EXPLAIN`, `SHOW`)
- **Инструменты записи:**
  - `execute_write_query` (`INSERT`, `UPDATE`, `DELETE`)
  - `execute_migration` (DDL)

  По умолчанию защищены и требуют подтверждения пользователя.

- **Безопасность:** подключение от имени пользователя с минимальными правами.
  Для write-операций — отдельный пользователь с ограниченными правами.
  Запрещены операции уровня `DROP DATABASE`, `LOAD_EXTENSION`, `COPY ... FROM PROGRAM`
  (postgres: также `pg_read_file`, `pg_ls_dir`; mysql: `ATTACH DATABASE`).
  `run_query` принимает только `SELECT`/`WITH`/`EXPLAIN`/`SHOW` (mysql —
  также `DESCRIBE`/`PRAGMA`), write-инструменты не принимают read-запросы.
- **Транзакционность:** все изменения выполняются в транзакциях с откатом при ошибке.

---

## MCP-сервер для 1С

Интеграция с 1С реализована через OData (`/odata/standard.odata`), работа — **только на чтение**.

- **Инструменты:**
  - `get_metadata`
  - `get_catalog`
  - `get_document`
  - `get_register`
- **Безопасность:** учётная запись с правами только на чтение нужных справочников и документов.
  Доступ на запись в 1С через OData — вне рамок текущей версии.
- **Семейство 1С-серверов:** помимо OData-чтения есть отдельные серверы
  `onec_query`, `onec_dcs`, `onec_forms`, `onec_metadata`, `onec_tests` —
  и `onec_designer` (конфигуратор: `load_config`, `unload_config`,
  `update_db`, `create_ib`) — операции записи, требующие подтверждения.
- **Готовые решения:** `mcp-1c`, `1c-mcp-toolkit`, `1c-odata-mcp` — можно заменить
  свой сервер одним из них.

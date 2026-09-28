# 🗺️ Что за что отвечает — подробная карта проекта

> Этот документ — полная «карта местности» репозитория: каждый каталог,
> каждый модуль и каждая декларация с пояснением, зачем они нужны и как
> связаны между собой. Числа соответствуют текущему состоянию `main`:
> **37 агентов, 39 MCP-серверов, 8 capabilities, 7 features,
> 32 harness-сценария, 2 loop-спека**.
>
> Смежные документы: [GETTING_STARTED.md](GETTING_STARTED.md) (установка и
> первый запуск), [CAPABILITIES.md](CAPABILITIES.md) (возможности),
> [ARCHITECTURE-V2.md](ARCHITECTURE-V2.md) (как система развивается),
> [JOURNAL.md](JOURNAL.md) (журнал), [DATABASE.md](DATABASE.md) (БД),
> [SECURITY.md](SECURITY.md) (политики и аудит).

---

## 1. Карта репозитория на одном экране

| Путь | За что отвечает |
|---|---|
| `run.py`, `run.sh`, `run.bat` | **Старт системы**: проверки окружения, автозапуск qwenproxy, клининг портов |
| `main.py` → `src/main.py` | **Сервер**: FastAPI + WebSocket, монтирование всех API |
| `install.py`, `install.sh`, `install.bat`, `setup.py`, `first_run.py` | Установка, настройка, мастер первого запуска |
| `src/` | Весь исходный код (Python): ядро, агенты, MCP, память, журнал, БД |
| `agents/<имя>/agent.yaml` | **Декларации 37 агентов**: кто они, какие loop и MCP используют |
| `mcp_servers/<имя>/server.yaml` | **Декларации 39 MCP-серверов**: команды запуска, инструменты, health |
| `capabilities/*.yaml` | Декларации 8 capabilities (vision, ocr, whisper, browser…) |
| `loops/*.yaml` | Декларативные циклы: `reasoning` (LLM ↔ tools) и `verification` |
| `features/<имя>/` | Подсистемы-фичи (Feature SDK): api.py + feature.yaml + ui.js |
| `config/*.yaml` | Статические конфиги: models, settings, memory, extraction, alerting |
| `harness/` | Тестовый полигон: сценарии, fixtures, pytest-прогоны |
| `db/*.sql` | Схемы PostgreSQL: init, ops, journal, analytics, AGE, CDC, поиск |
| `scripts/` | Инструменты разработки, миграции, тесты, рекордер fixtures |
| `tests/` | Юнит/регрессионные тесты ядра (без pytest-зависимости) |
| `extension/` | Браузерное расширение «LLM Agent Bridge» (Chromium + Firefox) |
| `src/web/` | Веб-интерфейс: чат, настройки, аналитика, интерактивный курс |
| `.github/workflows/` | CI: `validate.yml` и `harness.yml` (жёсткие гейты) |
| `attic/` | Архив: legacy-агенты и ранние версии журнала (не используется) |
| `data/` | Runtime-данные (профили, sandbox-прогоны harness, кэши) |
| `patches/` | История патчей из чат-сессий (MANIFEST.md) |
| `deploy/`, `docker-compose.yml`, `nginx.conf` | Эксплуатация: systemd, контейнеры, reverse-proxy |

---

## 2. Точка входа и жизненный цикл запуска

### 2.1. Файлы запуска (корень репозитория)

| Файл | Что делает |
|---|---|
| **`run.py`** | Главная точка входа. Проверяет Python и зависимости, запускает анти-zombie клининг портов (`src/port_cleanup.py`), при необходимости автостартует **qwenproxy** (локальный OpenAI-совместимый прокси к LLM), затем поднимает сервер. При занятом порте печатает готовые команды `findstr`/`taskkill` (контракт закреплён тестом `scripts/test_run_port_busy.py`) |
| **`main.py`** | Корневая точка входа без проверок окружения: просто передаёт управление `src.main` (когда окружение уже заведомо настроено) |
| **`setup.py`** | Единый интерактивный скрипт настройки и запуска (меню: установка зависимостей, `.env`, инициализация БД, старт) |
| **`first_run.py`** | Мастер первого запуска (онбординг): пошаговая первичная настройка, проверка доступности LLM и PostgreSQL |
| **`install.py` / `install.sh` / `install.bat`** | Установка: venv, `pip install -r requirements.txt`, playwright-браузеры |
| **`run.sh` / `run.bat`** | Обёртки запуска для Linux/macOS и Windows (92 строки bat решают проблему «окно закрылось и ошибки не видно») |
| **`build_exe.sh` / `build_exe.bat`** | Сборка standalone-исполняемого файла через PyInstaller (спека `llm_agent.spec`) |
| **`install_playwright.bat`** | Отдельная установка браузеров Playwright для Windows |

### 2.2. Что происходит при старте (порядок)

1. `run.py` → клининг портов → (опционально) qwenproxy → `uvicorn src.main:app`.
2. `src/main.py` поднимает **FastAPI**: WebSocket `/ws`, REST `/api/*`, статику веб-интерфейса, `/guide` (интерактивный курс), `/analytics` (дашборд).
3. `src/core/loader.py` читает декларации `agents/*/agent.yaml`, `mcp_servers/*/server.yaml`, `capabilities/*.yaml` и собирает **Registry** (`src/core/registry.py`) — единую точку правды.
4. `src/core/file_watcher.py` начинает следить за декларациями: hot-reload с debounce, ожиданием стабильности файла и **автооткатом** на последнюю валидную версию (`src/core/rollback.py`), если YAML сломали.
5. `src/db/autodetect.py` обнаруживает PostgreSQL (если есть) и подключает долговременные зеркала; без PG система полностью работоспособна на SQLite.
6. Фоновые задачи (`src/core/background.py`): обогащение памяти (Ollama-воркер), бэкапы, ретенция, analytics refresh.

---

## 3. Путь запроса: как система обрабатывает сообщение

Понимание этого потока — ключ к навигации по коду:

```
Браузер (src/web/app.js)
   │  WebSocket /ws
   ▼
src/main.py ────────────► ConversationSession (src/supervisor/session.py)
   │                          │  история диалога, сессии
   ▼                          ▼
Intent Layer (src/supervisor/intents.py)   ← быстрый путь без LLM
   │
   ▼
Supervisor (src/supervisor/supervisor.py)  ← Plan → Execute → Observe → Re-plan
   │  план = DAG шагов (src/supervisor/dag.py, параллельное исполнение)
   ▼
Оркестратор (src/orchestrator.py)          ← LLM-роутер: какой агент нужен
   │  + route_analytics.py запоминает, «какой агент был нужен на самом деле»
   ▼
AgentRuntime (src/agents/runtime.py) → BaseAgent (src/agents/base.py)
   │  исполняет agent-декларацию через LoopController
   ▼
LoopController (src/loop/controller.py)    ← цикл loops/reasoning.yaml
   │  LLM (src/llm_client.py) возвращает tool_calls → выполняем → снова LLM
   ▼
MCPManager.call_tool (src/mcp_manager.py)  ← approval-gate, политики, health
   │
   ▼
MCP-сервер (src/mcp_servers/<имя>/server.py)  ← реальное действие: файлы, БД, 1С…
   │
   ├──► journal.recorder (src/journal/)     ← «чёрный ящик»: тени, diff, DAG
   ├──► memory.facade (src/memory/)         ← 5 слоёв памяти
   └──► events (src/events.py) → Ollama enricher (фоновая разметка событий)
```

После ответа: `loops/verification.yaml` (если были правки — чекеры `src/loop/checks/`: тесты, линтер, git diff, запрещённые паттерны), результат уходит в WS, всё фиксируется в журнале и зеркалируется в PostgreSQL.

## 4. Ядро системы: `src/` (корневые модули)

| Модуль | За что отвечает |
|---|---|
| **`src/main.py`** | FastAPI + WebSocket сервер: точка монтирования всех роутов (чат `/ws`, REST-фичи, журнал `/api/journal/*`, аналитика), статика `src/web/` |
| **`src/orchestrator.py`** | **Оркестратор** — LLM-роутинг: по сообщению и snapshot определяет, какие агенты нужны, и выполняет задачу (`handle`). Контракт покрыт `scripts/test_orchestrator_handle.py` |
| **`src/mcp_manager.py`** | **MCPManager** — единая точка вызова инструментов: запуск/пул MCP-процессов (stdio), `call_tool`, approval-gate (запрос подтверждения по политикам), health-проверки |
| **`src/llm_client.py`** | OpenAI-совместимый клиент LLM: tool calling, стриминг, кэш ответов, фолбэк между провайдерами |
| **`src/llm_providers.py`** | Реестр LLM-провайдеров и выбор модели в чате (Task 24-c/24-d) |
| **`src/llm_errors.py`** | Человекочитаемая расшифровка ошибок LLM-провайдеров (403 region, куки, таймауты и т.п.) |
| **`src/cli.py`** | **CLI** (`python -m src.cli`): `validate`, `harness`, `migrate`, `profiles`, `snapshot`, `rollback`, `loops` — см. справочник в §14 |
| **`src/config.py`** | Глобальный конфиг (Pydantic `AppSettings`), читает `config/*.yaml` и `.env` |
| **`src/env_file.py`** | Общий помощник работы с `.env` (чтение/правка без потери комментариев) |
| **`src/policies.py`** | Постоянные approval-политики (что подтверждать, что разрешать; TTL; экспорт/импорт) |
| **`src/events.py`** | **Событийная шина** (Этап 4): подписка/публикация событий между подсистемами |
| **`src/cache.py`** | SQLite-кэш ответов LLM с chunks-реплеем и метриками; инвалидация по хэшам файлов (`src/file_state.py`) |
| **`src/file_state.py`** | Хэши состояния файлов проекта — чтобы кэш LLM корректно инвалидовался |
| **`src/runtime_config.py`** | Обёртка над runtime-конфигом ядра (для совместимости) |
| **`src/bridge_store.py`** | BridgeStore — файловый мост «расширение браузера ↔ агенты» (обмен захватами страниц) |
| **`src/web_chat.py`** | Веб-чаты DeepSeek/Qwen по кукам из расширения (Task 27) |
| **`src/web_cookies.py`** | Приём и хранение кук веб-чатов из расширения Bridge |
| **`src/chat_import.py`** | Импорт истории чатов с сайтов DeepSeek/Qwen |
| **`src/route_analytics.py`** | Route-аналитика — «обучение на своих данных»: сравнение ожидаемого и фактического агента (Этап 5) |
| **`src/port_cleanup.py`** | Анти-zombie клининг: поиск и убийство процессов, держащих порт перед стартом (регрессия: `tests/test_port_cleanup.py`) |

---

## 5. `src/core/` — реестр, схемы и живучесть

| Модуль | За что отвечает |
|---|---|
| `registry.py` | **Registry — единая точка правды**: агенты, MCP, capabilities, их статусы |
| `loader.py` | Загрузка деклараций из YAML-файлов, связывание реестра |
| `schema.py` | Pydantic-схемы деклараций: Agent, MCP, Capability (что обязателено в YAML) |
| `snapshot.py` | **Snapshot** — единый снимок состояния системы (агенты, health, метрики) для промптов и UI |
| `health.py` | Проверка требований агентов и MCP: пакеты, env, пути, TCP, capabilities |
| `capabilities.py` | Capability Resolver — детерминированный выбор провайдера (например, какой vision-бэкенд использовать) |
| `features.py` | **FeatureLoader** — «всё — директория с манифестом»: автоподхват `features/*/feature.yaml` |
| `profiles.py` | Профили настроек: default, development, 1c_development, production (см. `data/profiles/`) |
| `file_watcher.py` | File-watcher: debounce + cooldown + ожидание стабильности файла (регрессия: `tests/test_file_watcher_stability.py`) |
| `rollback.py` | Хранилище последних валидных деклараций с автовосстановлением после сломанного hot-reload |
| `history.py` | История snapshot'ов: A/B-слоты, diff между сборками, retention |
| `migrations.py`, `migrations/` | Плагинные миграции деклараций (`m_0_9_to_1_0`, `m_1_2_to_1_5`) |
| `audit.py` | Аудит изменений настроек (кто/когда/что менял) |
| `background.py` | Фоновые задачи: периодические и ежедневные |
| `metrics.py` | Prometheus-совместимый экспорт метрик |

---

## 6. Агенты и Supervisor

### 6.1. `src/agents/` — декларативная модель

| Модуль | За что отвечает |
|---|---|
| `base.py` | **BaseAgent** — исполняет agent-декларацию: собирает промпт из `agents/<имя>/prompt.md`, подставляет инструменты, запускает LoopController |
| `runtime.py` | **AgentRuntime** — реестр готовых к запуску агентов (ленивая инициализация) |

Декларация агента в `agents/<имя>/`:
- `agent.yaml` — имя, описание, какие MCP-серверы и loop использовать;
- `prompt.md` — системный промпт агента;
- `user.md` — подсказка пользователю (что агент умеет).

### 6.2. `src/supervisor/` — контекстный чат (Этапы 1–3)

| Модуль | За что отвечает |
|---|---|
| `session.py` | **ConversationSession** — состояние диалога чата (история, восстановление) |
| `intents.py` | **Intent Layer** — быстрый путь маршрутизации без LLM (слэш-команды, очевидные интенты) |
| `supervisor.py` | **Supervisor** — цикл Plan → Execute → Observe → Re-plan: программа сама распределяет действия агентов по контексту |
| `models.py` | Контракты (Plan, Step, Observe и т.д.) |
| `plans.py` | Реестр планов: хранение, статус, восстановление |
| `dag.py` | **DAG-исполнение** плана: независимые шаги — параллельно |

### 6.3. `src/prompts/` — промпты (41 файл)

`orchestrator.py` (промпт роутера строится динамически в SnapshotBuilder) и
`supervisor.py` (промпты Supervisor) — активные; `<имя>_agent.py` — промпты
отдельных агентов (исторический слой, постепенно заменяются на
`agents/<имя>/prompt.md`).

---

## 7. Loop-система: `src/loop/` + `loops/`

| Модуль | За что отвечает |
|---|---|
| `spec.py` | Загрузка Loop-спеков из `loops/*.yaml` |
| `controller.py` | **LoopController** — исполняет LoopSpec: бюджет шагов, ретраи, выход по финальному ответу |
| `steps.py` | Обработчики шагов (LLM-вызов, tool-call, проверка) |
| `state.py`, `context.py` | Состояние цикла и разделяемый контекст выполнения |
| `faults.py` | Fault injection для chaos-тестирования (используется harness'ом) |
| `telemetry.py` | Телеметрия циклов: SQLite + метрики (читается `src/harness/trace_reader.py`) |
| `checks/` | Чекеры verification: `tests.py`, `lint.py`, `git_diff.py`, `todos.py` |

Спеки:
- `loops/reasoning.yaml` — цикл **LLM ↔ tools**: продолжается, пока LLM возвращает tool_calls;
- `loops/verification.yaml` — запускается после reasoning, если были изменения: тесты, линтер, git diff.

## 8. Инструменты: `src/mcp_manager.py` + 39 MCP-серверов

**MCPManager** (`src/mcp_manager.py`) — единственная точка вызова
инструментов для агентов: управление процессами MCP-серверов (stdio),
approval-gate по политикам (`src/policies.py`), health-проверки, таймауты.
Агенты никогда не ходят к серверам напрямую — только через `call_tool`.

Каждый сервер живёт в двух местах:
- **`src/mcp_servers/<имя>/server.py`** — реализация (инструменты);
- **`mcp_servers/<имя>/server.yaml`** — декларация: как запустить, какие
  инструменты есть, что нужно для health (реестр подхватывает автоматически).

> Легаси-обёртки в корне `src/mcp_servers/` (`filesystem_server.py`,
> `postgres_server.py`, `mysql_server.py`, `onec_server.py`,
> `deepseek_server.py`) — переадресация на пакетные версии для совместимости.

### 8.1. Файлы и код

| Сервер (tools) | За что отвечает |
|---|---|
| **filesystem** (31) | Файловая система проекта с sandboxing: чтение/правка/патчи, move/rename/copy/symlink, поиск, архивы, diff |
| **git** (9) | Git-операции в PROJECT_ROOT: status, diff, log, branch, add, commit, reset |
| **github** (21) | PR, issues, actions через GitHub API |
| **code_analysis** (23) | Анализ кода: символы (`symbols.py` через tree-sitter), ссылки, рефакторинг, метрики |
| **lsp** (13) | Language Server Protocol: навигация, диагностика, hover, rename (`client.py`/`manager.py` — по LSP-серверу на язык) |
| **testing** (10) | Тесты: discover, run, coverage, flaky; автоопределение фреймворка (`frameworks.py`) |
| **build** (16) | Сборка, пакеты, Docker |
| **debug** (11) | Отладка и профилирование: py-spy, pdb, CPU/memory profile |
| **environment** (14) | venv, nvm, pyenv, версии инструментов |

### 8.2. Базы данных

| Сервер (tools) | За что отвечает |
|---|---|
| **postgres** (7) | PostgreSQL: схемы, таблицы, запросы, миграции; авто-детект драйвера |
| **mysql** (6) | MySQL и SQLite |
| **db_extended** (20) | Redis, MongoDB, MSSQL, ElasticSearch |
| **migration** (10) | БД-миграции: Alembic, Django, custom SQL |
| **memory** (9) | Память проекта для агентов: recall, remember, профиль проекта, процедуры, граф |

### 8.3. 1С:Предприятие (полный цикл)

| Сервер (tools) | За что отвечает |
|---|---|
| **onec** (4) | Чтение данных 1С через OData (только чтение) |
| **onec_designer** (13) | Управление конфигуратором 1cv8.exe: load/unload config, update DB, build cf/cfe/epf; `finder.py` ищет 1cv8, `runner.py` — async-обёртка, `log_parser.py` — парсер логов, `differ.py` — сравнение конфигурации |
| **onec_metadata** (12) | XML-выгрузка конфигурации: объекты, модули, расширения, adopt, diff |
| **onec_query** (8) | Язык запросов 1С: парсинг, валидация, конвертация SQL↔1С, оптимизация |
| **onec_dcs** (8) | СКД: чтение, создание, расчётные поля, параметры, анализ, валидация |
| **onec_forms** (7) | Управляемые формы: чтение, BSL-модули, обработчики |
| **onec_tests** (6) | Автотесты 1С: YAxUnit (unit) и Vanessa Automation (BDD) |

Логика 1С вынесена в чистые пакеты (используются и MCP, и фичами напрямую):
`src/onec_query/` (lexer/parser/validator/converter), `src/onec_metadata/`,
`src/onec_forms/`, `src/onec_dcs/`, `src/onec_tests/`.

### 8.4. Сети, наблюдение, инфраструктура

| Сервер (tools) | За что отвечает |
|---|---|
| **http** (12) | HTTP-клиент, GraphQL, WebSocket, SSE, load test |
| **network** (10) | ping, traceroute, DNS, SSL, порты, whois |
| **kubernetes** (38) | K8s через kubectl: pods, deployments, services, helm |
| **cicd** (13) | GitLab CI и Jenkins: pipelines, jobs, retry/trigger |
| **monitoring** (10) | Логи, Prometheus-запросы, Jaeger-трейсы |
| **security** (10) | Секреты, SAST, SCA, SBOM, хэши, пароли/ключи |
| **storage** (9) | S3/MinIO/локальное хранилище: upload/download/presign/sync |
| **frontend** (10) | Bundle-анализ, CSS, a11y, Lighthouse |
| **data** (13) | Данные: CSV, Parquet, JSON, YAML, TOML, XML, diff |
| **journal** (8) | Доступ агентов к журналу: query, search, timeline, provenance, rollback, replay-plan |
| **bridge** (4) | Браузер через расширение: вкладки, чтение страницы, захваты |
| **deepseek_web** (2) | DeepSeek через веб-куки (без API-ключа) |

### 8.5. Документы и медиа

| Сервер (tools) | За что отвечает |
|---|---|
| **document** (7) | Чтение PDF/DOCX/XLSX, таблицы, поиск, кэш извлечения |
| **image** (12) | Изображения: describe (Vision), OCR, классификация, resize/crop, favicon |
| **media** (10) | Аудио/видео: транскрипция, перевод, кадры, clip, ffmpeg |
| **documentation** (11) | Docstring'и, Mermaid, OpenAPI из FastAPI, changelog, TOC |
| **browser** (26) | Playwright: страницы, клики, формы, скриншоты (`session.py` — пул сессий) |

Реализации поддержки: `src/capabilities/vision.py` (Qwen-VL через OpenAI API,
LLaVA через Ollama), `src/media/` (ffmpeg_utils, whisper_runner),
`src/storage/` (S3/MinIO/local backend), `src/extraction/` (см. §10).

---

## 9. Capabilities и capabilities-провайдеры

`capabilities/*.yaml` — 8 деклараций взаимозаменяемых провайдеров:
`vision`, `ocr`, `whisper`, `browser`, `media`, `storage`, `1c_metadata`,
`1c_query`. **Capability Resolver** (`src/core/capabilities.py`) выбирает
конкретного провайдера по доступности (пакеты, env, TCP) — детерминированно,
без угадывания. Реализации: `src/capabilities/vision.py` и модули выше.

## 10. Извлечение контента: `src/extraction/` + `extractors/`

| Модуль | За что отвечает |
|---|---|
| `pipeline.py` | **Extraction Pipeline**: выбор extractor + fallback-цепочка + кэш |
| `detector.py` | Определение MIME: расширение + magic bytes |
| `cache.py` | SQLite-кэш результатов (TTL из `config/extraction.yaml`) |
| `extractors/pdf.py` | PDF: текст → OCR-fallback |
| `extractors/docx.py` | DOCX |
| `extractors/xlsx.py` | XLSX |
| `extractors/image.py` | Метаданные + OCR + опционально Vision |
| `extractors/audio.py` | Транскрипция через Whisper (`src/media/whisper_runner.py`) |
| `extractors/text.py` | Текстовые файлы |
| `extractors/registry.yaml` | Реестр экстракторов (какой MIME → какой extractor) |

---

## 11. Память: `src/memory/` (5 слоёв) + `src/db/`

### 11.1. Слои памяти

| Модуль | Слой | За что отвечает |
|---|---|---|
| `facade.py` | — | **Фасад памяти** — единая точка входа; PostgreSQL с fallback и dual-write |
| `working.py` | working | Компактизация контекста диалога |
| `episodic.py` (+`store.py`) | episodic | Эпизодическая память (события, диалоги) — SQLite → PG |
| `semantic.py`, `user.py` | semantic | Профиль проекта и пользователя |
| `vector.py` (+`retriever.py`) | vector | Векторная память на LanceDB (локально) / pgvector (PG) |
| `procedural.py` | procedural | Процедуры: «как решали такие задачи» |
| `embedder.py`, `embedders.py` | — | Embedding-модели (bge-m3 локально; фабрика для памяти агентов) |
| `chunker.py`, `tokens.py`, `summarizer.py` | — | Чанки, токены, summary через LLM |
| `graph.py` + `graph_parsers/` | — | Knowledge graph на SQLite; парсеры Python/JS/TS через tree-sitter |
| `config.py` | — | Конфиг памяти из `config/memory.yaml` |

### 11.2. PostgreSQL-слой: `src/db/` (19 модулей)

| Модуль | За что отвечает |
|---|---|
| `pool.py` | Connection pool (psycopg3, Windows-совместимость — Task 23) |
| `init_db.py` | Инициализация и проверка схемы (CLI: `scripts/init_db.py`) |
| `autodetect.py` | Автодетект PG при каждом запуске (Task 24-b) |
| `vector_store.py` | VectorStore на pgvector: three-tier поиск (bit → halfvec → full) |
| `hybrid_search.py` | Гибридный поиск: vector + BM25 через RRF |
| `search_helpers.py` | Полнотекстовый поиск: Snowball-морфология + синонимы |
| `memory_store.py`, `agent_memory.py` | Episodic-зеркало; память агентов на pgvector (Task 14) |
| `graph_store.py`, `age_store.py`, `age_sync.py` | Граф на recursive CTE; Apache AGE (Cypher); синхронизация PG → AGE |
| `replicator.py` | Фоновый репликатор «локальные данные → PostgreSQL» |
| `multi_instance.py` | Multi-instance: advisory locks + LISTEN/NOTIFY |
| `analytics.py` | Аналитические запросы, кэширование (6 materialized views) |
| `backup.py` | Бэкапы через pg_dump с ротацией |
| `retention.py` | Ретенция долговременных зеркал |
| `pg_metrics.py` | Метрики PostgreSQL в Prometheus-формате |

Схемы: `db/init.sql` (8 схем: memory, vectors, graph, cache, policies, audit, metrics, meta), `db/ops.sql` (зеркала чатов/планов), `db/journal.sql` (зеркало журнала), `db/analytics.sql`, `db/age_schema.sql`, `db/cdc_notify.sql`, `db/search_improvements.sql`.

---

## 12. Журнал («чёрный ящик»): `src/journal/` (16 модулей)

| Модуль | За что отвечает |
|---|---|
| `schema.py` | События, типы, статусы (event-sourcing) |
| `config.py` | Конфигурация: окружение, порог диска |
| `recorder.py` | **Центральная точка записи**: все tool-calls и правки проходят здесь |
| `shadows.py` | Теневые копии файлов ДО/ПОСЛЕ правки |
| `store.py` | SQLite (WAL + FTS5) для индекса/поиска + JSONL для полного потока |
| `dependencies.py` | Граф зависимостей действий (DAG провенанса) |
| `rollback.py` | **Откат** проекта к состоянию «как было» (конфликт-детекция по хэшам) |
| `replay.py` | Воспроизведение действий (replay) |
| `retention.py` | «Память копится, пока есть место»: порог диска → сжатие → эскалация |
| `reports.py` | Markdown-отчёты по сессиям и задачам |
| `api.py` | HTTP API `/api/journal/*` (18 эндпоинтов) |
| `mcp_server.py` | MCP-сервер journal (8 инструментов) для доступа агентов |
| `integration.py` | Связка журнала с остальными компонентами (хуки) |
| `cli.py` | `python -m src.journal.cli`: status, timeline, events, report |
| `utils.py` | Хэши, диск, маскировка секретов, безопасный I/O |

Веб-вкладка — `src/web/journal.js` (самодостаточная, без правки app.js);
зеркало в PG — `db/journal.sql`; документация — `docs/JOURNAL.md`.

---

## 13. Малая LLM, кластер, CDC

| Путь | За что отвечает |
|---|---|
| `src/ollama/client.py` | Асинхронный клиент Ollama (малая модель ~1 GB VRAM) |
| `src/ollama/enricher.py` | Обогащение событий: importance, topic, tags; fallback на эвристику |
| `src/ollama/worker.py` | Фоновый воркер: батчи + advisory lock |
| `src/background/micro_tasks.py` | Локальные фоновые микрозадачи на малой модели (Task 24-c) |
| `src/cluster/*` | Multi-instance: heartbeat и шина событий в Redis, WS-bridge между инстансами |
| `src/cdc/notify_worker.py` | CDC: PostgreSQL LISTEN/NOTIFY → Kafka |
| `src/cdc/kafka_publisher.py` | Kafka publisher с буферизацией (6 topics) |

## 14. Веб-интерфейс: `src/web/` + `features/`

| Файл | За что отвечает |
|---|---|
| `index.html` + `app.js` + `style.css` | Чат и панель настроек (11 табов: агенты, MCP, capabilities, политики, бэкапы, синонимы, AGE, CDC…) |
| `analytics.html` | Дашборд аналитики (`/analytics`, WebSocket realtime, авто-refresh) |
| `guide.html/css/js` | **Интерактивный курс** `/guide`: 10 уроков с живыми проверками API и квизами |
| `journal.js` | Вкладка «Журнал» (подключается без правки app.js) |
| `src/main.py` | Раздаёт статику и монтирует API фич |

**Features** (`features/<имя>/`) — подсистемы по канону Feature SDK
(Этап 4): каждый — директория с манифестом, API-роутер и UI:

| Фича | За что отвечает |
|---|---|
| `notes` | Заметки — **эталонный пример** Feature SDK |
| `journal` | Вкладка журнала: события, тени, diff, граф, провенанс, откат |
| `memory` | Семантический индекс прошлых задач (pgvector) |
| `history` | Чтение долговременных зеркал PostgreSQL (диалоги, планы) |
| `ops` | Эксплуатация: бэкапы и ретенция зеркал |
| `cluster` | Мультиинстанс-аналитика: реестр инстансов, heartbeat |
| `bridge` | REST-стык расширения браузера: вкладки, захваты, приём кук |

---

## 15. Конфигурация

| Файл | За что отвечает |
|---|---|
| `config/settings.yaml` | Общие настройки: агенты, MCP, лимиты, логирование |
| `config/models.yaml` | LLM: провайдеры, модели, дефолт (`qwen3.8-max`), правила выбора |
| `config/memory.yaml` | Память: какие слои включены, параметры |
| `config/extraction.yaml` | Экстракция: кэш, TTL, лимиты |
| `config/alerting.yaml` | Правила алертинга |
| `config/litellm.example.yaml` | Пример конфига LiteLLM-прокси |
| `.env.example` | Все переменные окружения с комментариями |
| `data/profiles/*.json` | Профили: default, development, 1c_development, production (+ пользовательские через `src.core.profiles`) |

---

## 16. Тестирование и качество

### 16.1. Harness — тестовый полигон агентов (`src/harness/` + `harness/`)

| Модуль | За что отвечает |
|---|---|
| `src/harness/schema.py` | Pydantic-схемы сценариев |
| `src/harness/loader.py` | Загрузка сценариев из `harness/scenarios/` |
| `src/harness/runner.py` | **HarnessRunner**: запись (`--record`) и воспроизведение сценариев |
| `src/harness/mocks.py` | Mock LLM и MCP с записью/воспроизведением fixtures |
| `src/harness/fixtures.py` | Подготовка sandbox-окружения сценария |
| `src/harness/sandbox.py` | Изолированная директория прогона (результаты в `data/harness_runs/`) |
| `src/harness/assertions.py` | Проверки результата |
| `src/harness/trace_reader.py` | Чтение loop-трейсов из телеметрии для assertions |
| `src/harness/baseline.py` | Эталоны + регрессия |
| `src/harness/benchmark.py` | N прогонов с агрегацией метрик |
| `src/harness/chaos.py` | Chaos-сценарии (FaultRule из Scenario) |
| `src/harness/reporter.py` | Отчёты: text/JSON/JUnit |

Сценарии — `harness/scenarios/` (**32 шт.**): `smoke/` (2 — файловые базовые),
`1c/` (7), `dev/` (17), `browser/`, `document/`, `image/`, `media/`,
`storage/` (по 1) и `chaos/` (1 — реакция на таймаут LLM).
Fixtures — `harness/fixtures/llm/*.json` (по одной на сценарий; для smoke-гейта
в CI они детерминированные; перезапись записями реального стека —
`scripts/record_harness_fixtures.py --real`).
Прогон: `python -m src.cli harness {run|tag|all} ... [--mock-llm] [--mock-mcp] [--record]`.

### 16.2. Тесты

| Каталог | За что отвечает |
|---|---|
| `tests/` | Регрессии ядра без pytest-зависимости: watcher, порты, куки, автодетект PG, провайдеры, журнал |
| `harness/tests/` | Pytest-прогон всех сценариев (`conftest.py`, `test_scenarios.py`) |
| `scripts/test_*.py` | Интеграционные тесты этапов (1–5), фич, моста, PG-контура |
| `scripts/ws_probe_*.py` | E2E-пробы WebSocket на живом сервере |
| `scripts/run_regression.sh` | Комплексный регрессионный прогон |
| `pytest.ini` | Конфигурация pytest |

### 16.3. CI: `.github/workflows/`

| Workflow | За что отвечает |
|---|---|
| `validate.yml` | Установка зависимостей (Python 3.12 — pgserver даёт колёса только до cp312) + `python -m src.cli validate` |
| `harness.yml` | **Жёсткие гейты**: `validate` → file-сценарии → весь smoke-набор (31) → все категории (32, chaos включительно). Fixtures детерминированные, поэтому CI зелёный без реального стека; пушится на `main`/`master`/`dev` и PR |

---

## 17. Скрипты разработки: `scripts/`

| Скрипт | За что отвечает |
|---|---|
| `record_harness_fixtures.py` | **Рекордер fixtures**: генерация детерминированных / запись реальных (--real) LLM-fixtures |
| `new_agent.py`, `new_feature.py` | Скелетонеры: новый агент / новая фича по канону |
| `init_db.py` | Инициализация схемы PostgreSQL |
| `migrate_lancedb_to_pg.py`, `migrate_sqlite_memory.py`, `migrate_graph_to_pg.py` | Миграции локальных данных → PostgreSQL |
| `e2e_db_pg.py`, `test_db_pg.py`, `test_ops_pg.py` | E2E и тесты PostgreSQL-контура |
| `build_extension.py`, `sign_firefox.py`, `make_extension_icons.py` | Сборка/подпись/иконки расширения Bridge |
| `fake_provider.py` | Фейковый OpenAI-совместимый провайдер для E2E |
| `export_report.py` | Экспорт отчётов из зеркал |
| `route_report.py` | Отчёт «какой агент был нужен на самом деле» |
| `test_stage*.py`, `test_*.py` | Тесты этапов и функций (см. §16.2) |

---

## 18. Расширение браузера: `extension/`

«LLM Agent Bridge» — мост «браузер ↔ агенты»:

| Путь | За что отвечает |
|---|---|
| `shared/bridge-core.js` | Общая логика моста (обе сборки) |
| `chromium/`, `firefox/` | Манифесты и background-скрипты под каждую платформу |
| `popup/`, `panel/` | UI расширения: список вкладок, статусы, настройки |
| `icons/` | Иконки (генерируются `scripts/make_extension_icons.py`) |
| `dist/` | Готовые сборки (Chromium/Firefox) |

API-сторона на сервере — фича `features/bridge/` + MCP-сервер `bridge`.

---

## 19. Инфраструктура и эксплуатация

| Файл | За что отвечает |
|---|---|
| `docker-compose.yml` | Контейнеры: postgres, age-postgres (Apache AGE), kafka, kafka-ui (+тома) |
| `nginx.conf` | Reverse-proxy для сервера |
| `deploy/llm-agent.service` | systemd-юнит |
| `deploy/env.example` | Пример окружения для деплоя |
| `requirements*.txt` | Зависимости: базовые, pg8000-вариант, psycopg3-вариант, freethreaded-вариант (pgserver: только Python ≤3.12) |
| `attic/` | Архив legacy-агентов и ранних версий журнала (история, не код в проде) |
| `patches/` | Манифест и история патчей чат-сессий |
| `data/` | Runtime: профили, sandbox-прогоны harness, кэши (в git — только профили) |

---

## 20. Справочник CLI

```bash
# Главный CLI системы
python -m src.cli validate                      # валидация деклараций (реестра)
python -m src.cli harness run <scenario> ...    # один сценарий
python -m src.cli harness tag --tag smoke ...   # категория (smoke)
python -m src.cli harness all ...               # все категории
#    флаги: --mock-llm --mock-mcp --record --keep
python -m src.cli migrate                       # миграции деклараций
python -m src.cli profiles                      # профили настроек
python -m src.cli snapshot                      # снимок состояния
python -m src.cli snapshots                     # история снимков
python -m src.cli rollback                      # откат деклараций
python -m src.cli loops                         # информация о loop-спеках

# CLI журнала
python -m src.journal.cli status|timeline|events|report

# Запуск/установка
python run.py            # полный старт с проверками
python -m src.main       # только сервер
```

---

## 21. Куда что добавлять (быстрые рецепты)

- **Новый агент** → `python scripts/new_agent.py <имя>` (создаст `agents/<имя>/` по канону) или вручную: `agent.yaml` + `prompt.md` + `user.md`.
- **Новый MCP-сервер** → `src/mcp_servers/<имя>/server.py` + декларация `mcp_servers/<имя>/server.yaml` (реестр подхватит автоматически; `src/core/health.py` проверит требования).
- **Новая подсистема-фича** → `python scripts/new_feature.py <имя>` (эталон — `features/notes/`).
- **Новый capability-провайдер** → декларация в `capabilities/*.yaml` + реализация в `src/capabilities/`.
- **Новый harness-сценарий** → YAML в `harness/scenarios/<категория>/`, fixtures через `scripts/record_harness_fixtures.py`.
- **Новый чекер verification** → `src/loop/checks/` + регистрация в `checks/__init__.py`.
- **Миграция схемы деклараций** → плагин в `src/core/migrations/`.

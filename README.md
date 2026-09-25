# 🤖 LLM Agent — мультиагентная система правки файлов и БД

Локальный веб-чат с LLM, который через **MCP-серверы** управляет файлами
проекта и базами данных. Поддерживает 38 MCP-серверов, 36 агентов, полный
цикл 1С-разработки, интеграцию с PostgreSQL+pgvector, Ollama (малая LLM),
Apache AGE, Kafka (CDC), Kubernetes.

---

## 📚 Документация

| Документ | Что внутри |
|---|---|
| **[docs/GETTING_STARTED.md](docs/GETTING_STARTED.md)** | подробная инструкция: установка, первый запуск, мастер настройки, `.env`, PostgreSQL, расширение, диагностика |
| **[docs/CAPABILITIES.md](docs/CAPABILITIES.md)** | полное описание возможностей: чат, модели, 37 агентов, 39 MCP, память, журнал, bridge, аналитика |
| **[docs/FAQ.md](docs/FAQ.md)** | решения типовых проблем (окно bat закрывается, кодировки, порт занят, 403 region, куки протухли…) |
| [docs/ARCHITECTURE-V2.md](docs/ARCHITECTURE-V2.md) | архитектура системы |
| [docs/JOURNAL.md](docs/JOURNAL.md) | журнал действий: откат и реплей |
| [docs/DATABASE.md](docs/DATABASE.md) | базы данных, PostgreSQL-интеграция |
| [docs/providers.md](docs/providers.md) | провайдеры LLM и как добавить своего |
| [docs/MCP_SERVERS.md](docs/MCP_SERVERS.md) | справочник MCP-серверов |
| [docs/SECURITY.md](docs/SECURITY.md) | политики, подтверждения, аудит |

🎓 **Интерактивный курс** — внутри системы: запустите сервер и откройте
`http://127.0.0.1:8000/guide` (или кнопка 🎓 в шапке чата): 10 уроков
с живыми проверками API, квизами и прогрессом.

---

## ✨ Возможности

### Ядро
- 🧠 **Мультиагентный оркестратор** — LLM-роутер
- 📋 **Декларативный реестр** — агенты, MCP, capabilities в YAML
- 📸 **Snapshot** — единый снимок системы
- 🔄 **Loop-система** — декларативные циклы (reasoning, verification, retry)
- 🎛 **Hot-reload** — file-watcher с откатом
- 🛡 **Approval gate** — политики, TTL, экспорт/импорт

### Память
- 🧩 **5 слоёв** — working, episodic, semantic, vector, procedural
- 🐘 **PostgreSQL + pgvector** — основное хранилище (Часть 1)
- 🔍 **Гибридный поиск** — vector + BM25 через RRF
- 🌳 **Snowball-морфология** — русский язык + синонимы
- 🧠 **Ollama (малая LLM)** — фоновое обогащение событий
- 📊 **Analytics** — 6 materialized views + дашборд + WebSocket
- 🕸 **Apache AGE** — property graph (Cypher)

### Инструменты
- 🔧 **38 MCP-серверов**:
  - Файлы, Git, Shell, Database Extended
  - Code Analysis, LSP, Testing, Build, Debug
  - HTTP, Security, GitHub, Migration, CI/CD
  - Document, Image, Browser, Media, Storage
  - 1С: Designer, Metadata, Query, DCS, Forms, Tests
  - Network, Monitoring, Frontend, Data, Kubernetes
- 📚 **Extractors** — PDF, DOCX, XLSX, Image, Audio, Video
- 🌐 **Vision** — Qwen-VL, LLaVA, Tesseract

### Инфраструктура
- 📡 **CDC** — PostgreSQL NOTIFY → Kafka
- 💾 **Auto-backup** — pg_dump с ротацией
- 📈 **Prometheus metrics** — PG + система
- 🌐 **Multi-instance** — advisory locks + LISTEN/NOTIFY
- ☸️ **Kubernetes MCP** — pods, deployments, services, helm

### Разработка
- 🤖 **36 агентов**: file, mysql, postgres, onec*, deepseek, git,
  shell, document, image, browser, media, storage, code_analysis,
  lsp, testing, build, debug, http, security, github, migration,
  db_extended, documentation, cicd, environment, network,
  monitoring, frontend, data, kubernetes
- 🧪 **Harness** — сценарии, fixtures, assertions, chaos, baseline
- 🔄 **CI-интеграция** — GitHub Actions, GitLab CI

---

## 🏗️ Архитектура


```
llm-agent/
├── run.py                 # точка входа: проверки + автозапуск qwenproxy + uvicorn
├── setup.py               # интерактивный установщик (проверка окружения, .env, pip)
├── main.py                # системный лаунчер (проверки окружения)
├── src/
│   ├── main.py            # FastAPI + WebSocket сервер (lifespan: все подсистемы)
│   ├── config.py          # Pydantic-настройки (env → AppSettings)
│   ├── orchestrator.py    # LLM-роутинг между агентами
│   ├── mcp_manager.py     # MCP: запуск серверов, journal-перехват tool-call
│   ├── policies.py        # approval gate, TTL-политики, экспорт/импорт
│   ├── core/              # реестр: loader, schema, health, snapshot,
│   │                      # history, profiles, audit, rollback, metrics,
│   │                      # file_watcher, background, migrations
│   ├── loop/              # декларативные циклы: reasoning, verification,
│   │                      # reflection, retry, feedback, watchdog...
│   ├── agents/            # AgentRuntime — обёртки над LoopController
│   ├── memory/            # 5 слоёв памяти + retriever + graph (tree-sitter)
│   ├── journal/           # 📓 event sourcing: recorder, action_graph,
│   │                      # rollback, replay, retention, storage (blobs)
│   ├── db/                # PostgreSQL: pool, vector_store, memory_store,
│   │                      # graph_store, hybrid_search, analytics, AGE,
│   │                      # CDC, backup, pg_metrics, multi_instance
│   ├── ollama/            # малая LLM: client, enricher, worker
│   ├── mcp_servers/       # 37 MCP-серверов
│   ├── harness/           # сценарии, mocks, reporter (JUnit XML)
│   ├── extraction/        # PDF, DOCX, XLSX, Image, Audio
│   ├── cluster/           # Redis bus, heartbeat, WS bridge
│   └── web/               # index.html, app.js, style.css, analytics.html
├── agents/                # декларации агентов (agent.yaml + prompt.md)
├── mcp_servers/           # декларации MCP-серверов (server.yaml)
├── capabilities/          # vision, ocr, whisper, media, storage...
├── loops/                 # YAML-объявления петель
├── config/                # settings.yaml, memory.yaml, models.yaml...
├── db/                    # init.sql, analytics.sql, journal.sql, age_schema.sql
├── harness/               # scenarios/, fixtures/, baselines/, tests/
├── docs/                  # ARCHITECTURE.md, STRUCTURE.md...
└── scripts/               # миграции LanceDB→PG, SQLite→PG
```

---

## 🚀 Запуск

### Авто-установщик на ПК (рекомендуется для первого развёртывания)

В корне проекта интерактивные установщики — сами проверят Python,
создадут `.venv`, поставят зависимости, спросят про PostgreSQL
(вопросы можно принять по умолчанию — Enter), запишут `.env`, накатят
схему и прогонят смоук-тест сервера.

**Рекомендуется `install.py`** — не зависит от причуд cmd.exe (кодировки,
CRLF, «!») и при любой ошибке печатает причину и ждёт Enter, а не
закрывает окно молча:

```bat
:: Windows 10/11 — основной способ:
python install.py

:: Альтернатива (если python-установщик недоступен):
install.bat
```

```bash
# Linux / macOS:
bash install.sh        # или: python3 install.py
```

Тихий режим без вопросов (для скриптов): `python install.py --auto` /
`install.bat --auto` / `bash install.sh --auto`.

> **Python 3.11+**, рекомендуем 3.13 (обычная сборка). Free-threaded сборки
> (3.13t/3.14t) тоже работают: установщик сам возьмёт requirements-freethreaded.txt
> — без lancedb, sentence-transformers, tree-sitter, duckdb, aiokafka, faster-whisper;
> psycopg чистый, без бинарника (PG-фичи отдают 503, пока libpq не появится в PATH),
> embedder — hashing, векторное хранилище — postgres/файлы. Полный граф зависимостей
> набора проверен dry-run резолвером pip под cp314t/win_amd64 (2026-09): сходится.
> t-сборка определяется устойчиво, в том числе на Windows (где sysconfig не отдаёт
> Py_GIL_DISABLED): по суффиксу расширений (`.cp314t-win_amd64.pyd`), флагу сборки,
> abiflags и пути интерпретатора. Если requirements-freethreaded.txt потерялся —
> install.py сам сгенерирует усечённый набор из requirements.txt; если полный
> requirements.txt не сошёлся на Python 3.14+ (нет колёс либо sdist-конфликт
> pydoc-markdown — воспроизводится и на обычном cp314) — pip-установка
> автоматически повторится на усечённом наборе.

Шаблон всех переменных окружения с комментариями — [deploy/env.example](deploy/env.example).

### Первый запуск — мастер настройки

При первом старте `run.py` **сам запустит мастер настройки** (`first_run.py`):
определит корень проекта, создаст все каталоги и базы данных в папке проекта
(`data/`), интерактивно спросит настройки AI-моделей и запишет `.env`.

Ручной запуск и флаги:

```bash
python first_run.py                  # интерактивный мастер
python first_run.py --defaults       # без вопросов: каталоги + базы + .env
python first_run.py --reconfigure-ai # заново спросить только настройки AI
python first_run.py --reset          # сбросить и пройти мастер заново
```

AI-модели: **основной путь — браузер/куки** (веб-чат DeepSeek: F12 →
Application → Cookies → `ds_session_id` и др.), **второстепенный —
OpenAI-совместимый API** (DeepSeek/OpenAI/Ollama — пресеты в мастере).

Базы, которые мастер создаёт в `data/`: `cache.sqlite`, `policies.sqlite`,
`loop_telemetry.sqlite`, `audit.db`, `extraction.sqlite`; память
(`memory.sqlite`, `graph.sqlite`, `vector.lance`) система создаёт при первом
старте сервера. Все пути относительные, абсолютный корень прописывается в
`.env` автоматически (`PROJECT_ROOT`).

### Windows 10/11 x64

1. **Python 3.11+** — https://www.python.org/downloads/
   При установке отметь ☑ «Add python.exe to PATH».

2. **Распакуй проект**, открой PowerShell в папке проекта:

```powershell
cd D:\Downloads\llm-agent

# Виртуальное окружение
python -m venv .venv
.venv\Scripts\activate

# Зависимости
pip install --upgrade pip
pip install -r requirements.txt
```

3. **Запуск** (всё проверит и поднимет сам):

```powershell
python run.py
```

`run.py` автоматически: проверит Python/зависимости → подберёт доступный
PostgreSQL-драйвер (psycopg3 → psycopg2 → pg8000) → запустит qwenproxy
(если включён) → поднимет сервер на `http://127.0.0.1:8000`.

Интерактивная установка с проверкой окружения: `python setup.py`.

4. **Открой в браузере**: http://127.0.0.1:8000

5. **Browser MCP (Playwright)** — нужен только для веб-агента:

```powershell
install_playwright.bat
```

6. **Готовый exe (опционально)**: запусти `build_exe.bat` — получишь
   одиночный лаунчер `dist\llm-agent.exe` (сборка 5-15 минут).
   Важно: exe собирается только НА Windows (PyInstaller не кросс-компилирует).

### Linux (Ubuntu/Debian, Fedora, Arch)

```bash
cd ~/llm-agent

# Виртуальное окружение
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

# Запуск
python run.py
# или: chmod +x run.sh && ./run.sh

# Browser MCP (опционально, для веб-агента)
pip install playwright && playwright install chromium

# Готовый бинарник (опционально): dist/llm-agent
./build_exe.sh
```

Системные зависимости (опционально, для отдельных MCP):

```bash
# Debian/Ubuntu
sudo apt install python3-venv python3-dev ripgrep fd-find ffmpeg tesseract-ocr

# Fedora
sudo dnf install ripgrep fd-find ffmpeg tesseract
```

Автозапуск как сервис (systemd) — см. `deploy/llm-agent.service`.

### Docker (инфраструктура)

PostgreSQL работает нативно на Windows. AGE и Kafka — только через Docker:

```bash
docker compose up -d          # поднимет AGE-PostgreSQL (5433) и Kafka (9092)
```

---

## 🐘 PostgreSQL (рекомендуется)

Без PostgreSQL система работает на SQLite + LanceDB (всё отключаемо).
С PostgreSQL включаются: векторная память, аналитика, AGE, CDC, авто-бэкапы
и **зеркала долговременного хранения** (журнал операций, диалоги чата,
планы Supervisor — через PgReplicator, «чтоб ничего не потерялось»).

```powershell
# 1. Создать базу (один раз)
psql -U postgres -c "CREATE USER llmagent WITH PASSWORD 'secret';"
psql -U postgres -c "CREATE DATABASE llmagent OWNER llmagent;"

# 2. Применить схему (идемпотентно: init.sql + journal + ops + аналитика)
python scripts/init_db.py
python scripts/init_db.py --check     # только проверить состояние

# 3. .env
PG_ENABLED=true
DATABASE_URL=postgresql://llmagent:secret@localhost:5432/llmagent
PRIMARY_VECTOR_STORE=postgres
# PG_REPLICATE=1          # репликация журнала/сессий/планов в PG (по умолчанию вкл)
# PG_REPLICATE_INTERVAL=15 # период цикла репликатора, сек
```

Либо через Docker: `docker compose up -d postgres` (образ pgvector/pgvector:pg16,
схема применяется автоматически при первом старте).

### Windows: пул работает на любом event loop

psycopg в async-режиме несовместим с ProactorEventLoop (стандарт Windows),
а менять политику глобально нельзя — на selector-лупе не работают
MCP stdio-серверы. Пул `src/db/pool.py` решает это сам:

- весь psycopg-трафик идёт через **фоновый selector-луп** в потоке-демоне
  (мост через `run_coroutine_threadsafe`) — Windows/Linux без разницы;
- перед подключением — **TCP-пробник** host:port: если PostgreSQL не
  запущен, пул вообще не создаётся — ни ретраев, ни спама в логе;
- неудачный `open()` закрывает пул (никаких пулов-зомби);
- при недоступном PG в логе — **одна понятная строка** с host:port и
  причиной (раз в 60с, после серии неудач — раз в 300с).

```text
PgReplicator: подключение PostgreSQL: нет ответа от localhost:5432:
соединение отклонено (PostgreSQL не запущен или порт закрыт).
Проверьте DSN (localhost:5432/llmagent) или отключите PostgreSQL в .env
```

Если PostgreSQL не нужен вовсе — добавьте в `.env`: `PG_REPLICATE=0`
(репликатор не стартует) и `PG_ENABLED=false` (память без попыток PG).

Состояние хранилища и репликатора: **GET /api/db/status**.
Просмотр зеркал в браузере — вкладка **«📜 История»** (фича `features/history`):
диалоги, планы, события журнала, полнотекстовый поиск, экспорт сессии в
Markdown. Экспорт из терминала: `scripts/export_report.py --help`.

Ещё три вкладки поверх PG (Task 14, детали в docs/DATABASE.md §10):

- **«🧠 Память»** (`features/memory`) — семантическая память агентов на
  pgvector: прошлые диалоги/планы/ошибки индексируются в `memory.tasks`,
  поиск по смыслу (vector → FTS → ILIKE), ручная индексация и очистка;
- **«🌐 Кластер»** (`features/cluster`) — мультиинстанс-аналитика:
  реестр инстансов (heartbeat + метрики в `ops.instances`), нагрузка
  по зеркалам за окно, топ инструментов/агентов;
- **«🛠 Эксплуатация»** (`features/ops`) — бэкапы pg_dump (список/создание)
  и ретенция зеркал (dry-run + подтверждаемое удаление, авто-режим
  `PG_RETENTION_DAYS>0`).

Перенос существующих данных: `scripts/migrate_sqlite_memory.py`,
`scripts/migrate_graph_to_pg.py`, `scripts/migrate_lancedb_to_pg.py`.
Подробности — в [docs/DATABASE.md](docs/DATABASE.md).

### Ollama (малая LLM для обогащения памяти)

```bash
# Установить Ollama: https://ollama.com/download
ollama pull qwen2.5:1.5b-instruct   # ~1 GB, целиком в 2GB VRAM
```

---

## 📓 Journal — журнал действий (откат и реплей)

Каждый tool-call агентов перехватывается и записывается: снимки файлов
до/после (тени), граф зависимостей, конфликт-детекция при откате.
Этап 5 (ARCHITECTURE-V2 §3.10): единый SQLite-бэкенд (stdlib, без
PostgreSQL), API `/api/journal/*` монтируется фичей `features/journal`.

- **События/поиск**: `GET /api/journal/events?task_id=…`,
  `GET /api/journal/search?q=…`, таймлайн `GET /api/journal/timeline`
- **Diff и провенанс**: `GET /api/journal/event/{id}/diff`,
  `GET /api/journal/provenance?target=src/app.py`
- **Откат**: `POST /api/journal/rollback {"event_id": "…", "mode": "dry_run"}`
  → `{"mode": "execute"}`; проверка целей `POST /api/journal/rollback/verify`
- **Реплей**: `POST /api/journal/replay/plan` → `POST /api/journal/replay/execute`
- **Retention**: память копится, пока есть место (`min_free_gb`,
  сжатие → эскалация) — `GET /api/journal/retention`
- **Отчёты**: `GET /api/journal/reports`, экспорт `GET /api/journal/export?format=md`
- **В чате**: «Покажи журнал последних действий», «Откати изменение X»
- **MCP**: декларация `mcp_servers/journal/server.yaml`
  (`python -m src.journal.mcp_server`, 8 инструментов)

---

## 🌐 Bridge — расширение браузера (Chromium / Firefox)

«Мозг в браузере»: расширение — тонкий клиент, вся логика на локальном
сервере llm-agent. Агенты видят открытые вкладки и умеют читать
страницы через расширение; пользователь захватывает страницы и
выделения одной командой из контекстного меню; чат с агентами живёт
в side panel / боковой панели — там же открываются вопросы из
омнибокса (`ag <запрос>`) и меню.

**Сборка и установка** (один раз):

```bash
python scripts/build_extension.py     # → extension/dist/bridge-{chromium,firefox}/ + zip
```

- **Chromium/Chrome/Edge/Яндекс**: `chrome://extensions` → «Загрузить
  распакованное» → папка `extension/dist/bridge-chromium`
- **Firefox**: `about:debugging#/runtime/this-firefox` → «Загрузить
  временное дополнение» → `manifest.json` из
  `extension/dist/bridge-firefox` (хост-разрешения выдаются вместе с
  установкой; готовый подписанный пакет — `extension/dist/*.zip`)

**Постоянная установка в Firefox — авто-подпись через web-ext** (нужен
Node.js и бесплатная пара ключей AMO):

```bash
# ключи: https://addons.mozilla.org/developers/addon/api/key/
set AMO_JWT_ISSUER=user:...& set AMO_JWT_SECRET=...&   (Windows)
export AMO_JWT_ISSUER=user:... AMO_JWT_SECRET=...      (Linux/macOS)
python scripts/sign_firefox.py                # build + lint + sign
# → extension/dist/signed/…xpi — открой его в Firefox: постоянная
#   установка в ОБЫЧНОМ Firefox, без режима отладки
```

`sign_firefox.py` сам пересобирает раскладку, гоняет `web-ext lint`
(сводку печатает; ошибки блокируют только при `--strict`), подписывает
channel `unlisted` (свое распространение без публикации в каталоге;
`--channel listed` — публикация на AMO с ревью) и находит готовый .xpi.
Первая подпись навсегда привязывает id `llm-agent-bridge@local` к твоему
аккаунту AMO; повторная подпись той же версии запрещена — подними
`version` в манифестах. Без Node.js скрипт печатает инструкцию (код 3),
без ключей — тоже (код 2).

**Что умеет:**

- **Контекстное меню**: «Захватить выделенное/страницу в LLM Agent»
  (текст уходит на сервер, вкладка «Мост» в web-UI показывает журнал);
  «Спросить агента про выделенное/страницу» (открывает панель и сразу
  задаёт вопрос с контекстом); «Суммаризировать страницу» — полный
  текст уходит в захваты, в чат ставится готовая задача с началом
  текста и ссылкой на захват (агент дочитывает через
  `bridge_get_capture`)
- **Горячая клавиша «разбудить агента»** — `Alt+Shift+B` (меняется:
  `chrome://extensions/shortcuts` или about:addons → «Управление
  сочетаниями клавиш»): открывает панель чата, фокусирует ввод и
  подхватывает выделенный текст активной вкладки (без автосообщения —
  допиши вопрос и Enter)
- **Омнибокс**: `ag` + Tab + запрос → вопрос открывается в панели чата
- **Панель чата**: тот же WS-протокол, что web-UI — планы Supervisor,
  стриминг токенов, апрувы инструментов и планов, сессии
  восстанавливаются после перезапуска
- **Вкладки для агентов**: расширение периодически отправляет снапшот
  открытых вкладок; статус-бейдж ON/OFF — связь с сервером
- **Чтение страниц**: только по явной задаче агента — `chrome.scripting`
  извлекает основной текст (≤40 000 симв.), без content-скриптов на
  каждой странице

**Агентская часть (MCP «bridge», 4 инструмента, все read-only):**
`bridge_tabs` (список вкладок), `bridge_read_tab` (прочитать вкладку по
`tab_id`/`url_contains`, ждёт ответ расширения), `bridge_search_captures`
(поиск по захватам), `bridge_get_capture`. Примеры в чате: «Прочитай
вкладку с документацией X и перескажи», «Что я сохранял из браузера
про Y?»

**REST моста** (`features/bridge/api.py`, монтируется FeatureLoader):
long-poll `GET /api/bridge/pull?wait=15`, `POST /api/bridge/capture`,
`POST /api/bridge/read`, захваты/задачи/вкладки `/api/bridge/*` —
вкладка «🌐 Мост» в настройках web-UI. Хранилище — файлы
`data/bridge/` (tabs.json, jobs.json, captures.jsonl; TTL задач 600 с,
авто-requeue зависших 90 с, захватов ≤ 500). CORS для extension-схем
настроен в `src/main.py` (переменная `EXTENSION_ORIGIN_REGEX`).

---

## 💻 Выбор модели в чате + локальные микрозадачи (Ollama)

Селект «Модель» в шапке чата показывает **все провайдеры группами**
(qwenproxy, DeepSeek API, Локальная Ollama, OpenAI) с живыми бейджами
доступности; локальные модели помечены 💻 «без интернета». Выбор
сохраняется автоматически в `runtime.yaml` и восстанавливается после
перезагрузки страницы. Запросы уходят именно на провайдера выбранной
модели (`config/models.yaml → providers`); у моделей с
`supports_tools: false` (локальная 1.5b) инструменты молча не
отправляются — крошки не ломают схему tool-calling.

Рядом — селект «Последние чаты»: переключение между сессиями с рендером
истории, «＋ Новый чат» — новая сессия.

Фоновый воркер микрозадач (`src/background/micro_tasks.py`) на той же
малой модели Ollama делает «косметику» без облака: авто-заголовки чатов
и планов (2–4 слова; ручные не перезаписываются), ключевые слова для
каждого сообщения (задел под пре-фильтр pgvector-памяти) и дайджест
журнала (⚙️ → «Система» → «Дайджест», кнопка «Обновить сейчас»). Пока
в чате активна локальная модель, воркер паузится — одна модель на 2 ГБ
VRAM без свопов.

API: `GET /api/models` (агрегатор), `POST /api/model/select`,
`GET /api/sessions`, `GET /api/digest/latest`, `POST /api/digest/refresh`.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `LOCAL_MICRO_TASKS_ENABLED` | `1` | `0` — выключить фоновые микрозадачи |
| `LOCAL_MICRO_MODEL` | из `OLLAMA_MODEL` | модель микрозадач |
| `LOCAL_MICRO_INTERVAL` | `60` | период цикла воркера, сек |
| `LOCAL_MICRO_DIGEST_INTERVAL` | `3600` | период дайджеста, сек |
| `DEEPSEEK_API_KEY` | — | ключ DeepSeek API (провайдер в селекте) |
| `QWENPROXY_URL` | `http://127.0.0.1:7936/v1` | адрес qwenproxy |

---

## 🧪 Тестирование

```bash
# Smoke-проверка окружения
python -c "import src.main" 

# Harness-сценарии (детерминированный прогон с mock-LLM)
python -m src.cli harness run --suite dev

# Юнит-тесты
pytest harness/tests -v

# Тест-сьюты скриптами (без pytest), в т.ч. мост расширения:
python scripts/test_bridge.py        # 78 проверок: BridgeStore, API,
                                     # FeatureLoader-контракт, CORS, MCP,
                                     # wake-agent/суммаризация, sign_firefox
python scripts/test_pg_windows_compat.py   # 70 проверок: TCP-пробник,
                                     # мост на фейках psycopg, fail-fast
                                     # без спама, репликатор, фасад фолбэка
                                     # + живой PostgreSQL (pgserver)
python tests/test_models_micro.py    # 56 проверок: провайдеры моделей,
                                     # роутинг endpoint’ов, отсечение tools,
                                     # заголовки/теги/дайджест микрозадач
```

---

## ⚙️ Настройка

Все параметры — в `.env` (скопируй из `.env.example`) и `config/settings.yaml`.
Агенты и модули включаются/выключаются прямо в UI: ⚙️ → «Агенты»/«MCP».
Профили настроек («разработка», «продакшен», «1С») — вкладка «Профили».

### Supervisor (планы в чате)

Чат по умолчанию работает через Supervisor (ARCHITECTURE-V2, Этапы 2–3): LLM
составляет план из шагов; шаги с зависимостями (`depends_on`) исполняются
волнами — независимые шаги волны идут **параллельно**, результат предыдущего
шага становится входом следующего; при ошибке шага наблюдатель принимает
решение по контексту ответа (продолжить / перепланировать / стоп), в UI
видна дорожка шагов со стримом каждого шага.

Планы переживают переподключение WS: клиент хранит `session_id` и после
reconnect получает `session_restored` (история + последний план); состояние
планов доступно через REST: `GET /api/plans?session_id=…` и
`GET /api/plans/{plan_id}` (дампы в `data/plans/`). Итоги планов уходят в
долгую память (Memory) и подмешиваются планировщику в следующих сессиях.

| Переменная | По умолчанию | Описание |
|---|---|---|
| `SUPERVISOR_ENABLED` | `1` | `0` — прежний режим без планов (route → агенты подряд) |
| `SUPERVISOR_MAX_STEPS` | `12` | максимум шагов одного плана |
| `SUPERVISOR_MAX_REPLANS` | `2` | максимум перепланировок на один запрос |
| `SUPERVISOR_MAX_SECONDS` | `1800` | стенка на весь план (сек) |
| `SUPERVISOR_PARALLEL` | `1` | `0` — всегда последовательно (как в Этапе 2) |
| `SUPERVISOR_MAX_CONCURRENT` | `3` | максимум шагов одной волны параллельно |

Опасные планы (destructive-агенты) и планы с `needs_approval` показываются
целиком с кнопками «Выполнить план / Отклонить».

### Route-аналитика (тюнинг роутинга на своих данных)

Этап 5 (ARCHITECTURE-V2 §3.10): каждое решение роутинга (Intent Layer,
LLM-роутер, план Supervisor) и итог его выполнения пишутся в
`data/routing/decisions.jsonl` (ротация 10 МБ) и публикуются в шину
(`route.decision` / `route.outcome`). Отчёт «какой агент был нужен на
самом деле» — выбираемость агентов, доли источников, частые ошибки,
запросы без выбора, агенты-«невидимки» — и конкретные подсказки,
какие `routing_hints` усилить:

```bash
python scripts/route_report.py                  # печать в консоль
python scripts/route_report.py --days 7 --out docs/route-report.md
```

Выключатель: `ROUTE_ANALYTICS=0` — файл не пишется, работа чата не меняется.

### Feature-система (новые подсистемы без правок ядра)

Подсистема = директория `features/<id>/` с манифестом `feature.yaml`
(ARCHITECTURE-V2, Этап 4). При старте `FeatureLoader` монтирует
API-роутер (`api_router: api.py:create_router`), регистрирует вкладку UI
(`ui_tab`) и при наличии прогоняет миграции — **ноль правок** `main.py` /
`index.html`. Реестр: `GET /api/features`; события фич — через шину
`src/events.py` (`events.publish("notes.changed", {...})`), WS-мост
доставляет их подключённым клиентам как `{"type": "event", ...}`.

```bash
python scripts/new_feature.py my_feature --title "Моя фича" --with-api --with-ui
python scripts/new_agent.py my_agent --title "Мой агент" --keywords "тест"
```

Сгенерированные заготовки проходят реестр автоматически; пример живой
фичи — `features/notes/` (заметки: API + вкладка в настройках + события).

## ❓ Troubleshooting

### LLM-провайдер: проверка связи и ошибки роутинга

Проверить связь с провайдером без запуска сервера:

```bash
python first_run.py --check-llm
```

Команда делает реальный тестовый запрос к `LLM_BASE_URL` и расшифровывает
ошибку по-русски. Те же подсказки (модуль `src/llm_errors.py`) появляются
в чате вместо сырых сообщений вроде `Error code: 403 - {...}`.

| Ошибка | Что означает | Решение |
|--------|--------------|---------|
| `403 unsupported_country_region_territory` | OpenAI блокирует ваш регион (РФ/РБ) — запрос ушёл на `api.openai.com` | Смените провайдера: `python first_run.py --reconfigure-ai` → DeepSeek API / VseGPT / ProxyAPI / Ollama (локально) / qwenproxy (браузер/куки) |
| `401 invalid_api_key` | Ключ не принят | Проверьте `LLM_API_KEY` в `.env` |
| `404 model_not_found` | Провайдер не знает имя модели | Имя в дропдауне должно совпадать с моделями провайдера: `LLM_MODEL` в `.env`, `config/models.yaml` |
| `429 insufficient_quota` | Исчерпан баланс/квота | Пополните аккаунт или смените провайдера |
| `Connection error` | Провайдер не запущен / неверный URL | Для qwenproxy: `npm i -g qwenproxy-cli` → запустите `qpx` → вкладка `[5] Accounts` → `A` (email и пароль от chat.qwen.ai) → проверка `python first_run.py --check-llm` (сам чинит старый порт 3456) |
| `429 rate_limit` | Лимит запросов | Подождите и повторите |

### Прочее

| Проблема | Решение |
|----------|---------|
| `ModuleNotFoundError: uvicorn` | Не активировано .venv → `.venv\Scripts\activate` |
| psycopg не ставится | `pip install psycopg2-binary` или `pg8000` — драйвер подберётся сам |
| Порт 8000 занят | `python run.py --port 8001` |
| «Ollama недоступна» | Не критично — обогащение отключится, память работает |
| AGE «extension not available» | Нормально без Docker — граф отключается автоматически |
| Journal: «инициализация не удалась» в логе | Смотри traceback выше строки — журнал мягкий, работа продолжается без него; восстанови `data/journal/` из бэкапа |
| qwenproxy не стартует | Проверь `QWENPROXY_ENABLED=false` если используешь чистый API |
| Открыл `index.html` как файл (`file://`) — статика/WS не работают | Запускай через `run.bat`/`run.sh` → `http://127.0.0.1:8000` |

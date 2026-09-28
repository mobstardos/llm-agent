# Архитектура

> Обзор того, **как система устроена**: слои, путь запроса, память,
> журнал, инструменты, расширение. Карта «какой файл за что отвечает» —
> [STRUCTURE.md](STRUCTURE.md); история дизайна Supervisor-цикла —
> [ARCHITECTURE-V2.md](ARCHITECTURE-V2.md); установка и запуск —
> [GETTING_STARTED.md](GETTING_STARTED.md).

---

## 1. Общая схема

```
    браузер (app.js)          расширение Bridge            REST-клиенты
         │  WS /ws   │  HTTP /api/*  │   │                    │
         └───────────┴───────┬───────┴───┴────────────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  src/main.py — FastAPI       │  lifespan: реестр деклараций,
              │  WS-концентратор + статика   │  MCP-старт, фичи, журнал, PG
              └──────────────┬───────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  src/supervisor/             │  Intent Layer → Plan →
              │  Plan → Execute → Observe    │  DAG-исполнение → Re-plan
              └──────────────┬───────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  src/orchestrator.py         │  route() / handle() /
              │  выбор и запуск агентов      │  run_agent()
              └──────────────┬───────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  BaseAgent + LoopController  │  reasoning / verification /
              │  (src/agents, src/loop/)     │  retry, бюджеты, телеметрия
              └──────────────┬───────────────┘
                             ▼
              ┌──────────────────────────────┐
              │  src/mcp_manager.py          │  ЕДИНАЯ точка tool-calls:
              │  MCPManager.call_tool()      │  политики → approval → журнал
              └──────────────┬───────────────┘
                             ▼
        39 MCP-серверов (mcp_servers/*/server.yaml): filesystem, git,
        shell, postgres, memory, deepseek_web, browser, document, …
```

Поперечные подсистемы (используются всеми слоями):

| Подсистема | Код | Суть |
|---|---|---|
| Событийная шина | `src/events.py` | `publish/subscribe`, изоляция падений подписчиков, мост в WS |
| Память | `src/memory/` | 4 горизонта — см. §5 |
| Журнал действий | `src/journal/` (16 модулей) | что произошло, что изменить, как откатить — см. §6 |
| LLM-слой | `src/llm_client.py`, `llm_providers.py`, `llm_errors.py` | провайдеры из `config/models.yaml`, фолбэки, ретраи, дружелюбные ошибки |
| Конфигурация | `src/core/loader.py`, `schema.py`, `registry.py` | pydantic-валидация деклараций; ошибка одной не ломает остальные |
| Данные | `src/db/` (19 модулей) | SQLite / LanceDB / PostgreSQL / AGE / Kafka / Redis — см. §8 |

---

## 2. Ключевые числа

| Сущность | Количество | Где |
|---|---|---|
| Агенты | 37 | `agents/*/agent.yaml` |
| MCP-серверы | 39 | `mcp_servers/*/server.yaml` |
| Capabilities | 8 | `capabilities/*.yaml` |
| Фичи | 7 | `features/*/feature.yaml` |
| Loop-спеки | 2 | `loops/reasoning.yaml`, `loops/verification.yaml` |
| Harness-сценарии | 32 (9 групп) + 32 fixtures | `harness/scenarios/`, `harness/fixtures/llm/` |

---

## 3. Путь запроса

### 3.1 Основной путь: Supervisor (по умолчанию)

Включён флагом `SUPERVISOR_ENABLED=1` (дефолт; `0` — откат на legacy-путь §3.2).

```
WS-сообщение пользователя
  │
  1. main.py: hello/session_id → восстановление сессии (session_restored:
     история + последний план из PlanRegistry, data/plans/*.json)
  2. ConversationSession (supervisor/session.py): история диалога,
     активный план, предпочтения; дамп в data/sessions/
  3. Intent Layer (supervisor/intents.py) — детерминированные проверки
     без LLM (миллисекунды): @упоминание агента, скоринг routing_hints
     (keywords/negative_keywords), «продолжай/а теперь», болтовня.
     Низкая уверенность → LLM-роутинг с историей диалога
  4. Supervisor.plan (supervisor/supervisor.py): LLM возвращает
     Plan {steps[{id, agent, task, depends_on}], reply}.
     Болтовня/простой вопрос → пустой план → прямой ответ без агентов
  5. DAG-исполнение (supervisor/dag.py): шаги волнами по depends_on,
     параллельно внутри волны (семафор SUPERVISOR_MAX_CONCURRENT=3);
     план без зависимостей — последовательно
  6. Каждый шаг = Orchestrator.run_agent() → BaseAgent.run()
     с бюджетами из декларации агента и loop-спеки
  7. LoopController (src/loop/controller.py): итерации reasoning,
     проверка verification, retry; fault-инъекции для хаос-сценариев;
     LLM-вызов с ретраями (LLM_CALL_MAX_ATTEMPTS=3)
  8. Инструменты: BaseAgent → MCPManager.call_tool() — §4
  9. Observe: успех → следующий шаг; провал → retry/другой агент/отчёт;
     новая задача → re-plan (≤ 2 раз)
 10. Синтез результатов → ответ пользователю; события
     (plan/step_start/token/step_done/plan_done) через шину → WS-мост →
     «дорожка шагов» в UI
```

### 3.2 Fallback: прямой роутинг

`SUPERVISOR_ENABLED=0`: `Orchestrator.route()` (подбирает агентов по
routing_hints + LLM) → `handle()` выполняет выбранных агентов
последовательно на одном запросе. Путь сохранён как деградация и
как самый простой сценарий «один вопрос — один агент».

### 3.3 REST-потоки

Всё, что не чат, идёт мимо Supervisor: `/api/*` — настройки, реестры
агентов/MCP, вкладки фич (`GET /api/features`), журнал, бэкапы,
диагностика (`/api/db/status`, `/api/models`), планы
(`GET /api/plans[/{id}]`).

---

## 4. Единая точка инструментов

Любой вызов инструмента любым агентом проходит через
`MCPManager.call_tool()`. Это единственное место, где:

- применяются **политики** (`src/policies.py`): режим агента
  (safe/destructive) и классы опасности инструментов;
- срабатывает **approval** — диалог подтверждения у пользователя для
  опасных действий;
- пишется **журнал** (`instrument_mcp_manager`): аргументы, результат,
  хэши файлов «до/после» (тени) — для провенанса и отката;
- учитываются таймауты, рестарты и health-check серверов.

Пер-агентный доступ к MCP-серверам задаётся в `agent.yaml`
(блок `mcp_servers`), реестр запускает только нужные экземпляры.

---

## 5. Память: четыре горизонта

| Горизонт | Хранилище | Что делает | Настройка |
|---|---|---|---|
| **Working** | в памяти процесса | контекст диалога: бюджеты секций (system/profile/recent/summary/recall), компакция на порогах 0.60/0.80 | `config/memory.yaml` |
| **Episodic** | SQLite `data/memory.sqlite` | сырые сообщения (90 дней), события (180), саммари (730); автосаммари в конце сессии и ежедневно в 03:00 | `config/memory.yaml` |
| **Semantic** | `data/project_profile.yaml/.md` | профиль проекта, авто-регенерация раз в 24 ч | `config/memory.yaml` |
| **Векторный/графовый** | LanceDB / PG (pgvector) / Apache AGE | поиск по смыслу (`hybrid_search`), связи сущностей | `.env` (`PRIMARY_VECTOR_STORE`, `AGE_*`) |

Фасад — `src/memory/facade.py`: один интерфейс для агентов и
Supervisor. Эмбеддер выбирается `AGENT_MEMORY_EMBEDDER` (`auto` —
bge-m3 или быстрый hashing; `hash` — без загрузок). Микрозадачи
(заголовки, теги, дайджесты) уходят на локальную Ollama — облачная
модель не тратится (`LOCAL_MICRO_TASKS_ENABLED`).

---

## 6. Журнал действий

`src/journal/` — 16 модулей: `schema, config, utils, shadows, store,
dependencies, recorder, retention, replay, rollback, reports,
integration, api, cli, mcp_server`.

- **Хранилище**: SQLite (WAL) + FTS5-полнотекст + JSONL-стрим.
- **Recorder** на contextvars: события пишутся из любой глубины кода
  без проброса параметров; мягкие хуки — нет рекордера, нет затрат.
- **Тени (shadows)**: хэши и содержимое файлов до/после инструментальных
  вызовов → точечный **rollback** с детекцией конфликтов и **replay**
  последовательности событий.
- **Retention**: при заполнении диска (порог ~5 ГБ) — сжатие и эскалация.
- **Доступ**: 18 REST-эндпоинтов (фича `features/journal`), 8
  MCP-инструментов, CLI (`python -m src.journal.cli`), вкладка
  «Журнал» и Rollback в веб-интерфейсе.

Философия: журнал отвечает на три вопроса — *что произошло? что именно
изменить? как это откатить?* — и ничего не навязывает слоям выше.

---

## 7. Расширение: «всё — директория с манифестом»

| Что добавляем | Куда | Манифест | Автоматически |
|---|---|---|---|
| Агент | `agents/<id>/` | `agent.yaml` | MCP-доступ, роутер-хинты, карточка в UI, бюджеты |
| MCP-сервер | `mcp_servers/<id>/` | `server.yaml` | запуск/остановка, health, список инструментов |
| Capability | `capabilities/<id>.yaml` | capability-схема | провайдеры для `requires` |
| Loop-спека | `loops/<id>.yaml` | `LoopSpec` | доступна агентам через `LoopSpecLoader` |
| Фича (подсистема) | `features/<id>/` | `feature.yaml` | API-роутер, вкладка UI, WS-события, миграции |

Загрузчик (`src/core/loader.py`) валидирует декларации pydantic-схемами
(`src/core/schema.py`); ошибка одной декларации не ломает остальные.
Фичи подключает `FeatureLoader` (`src/core/features.py`): роутер из
`api.py:create_router`, вкладка из реестра `GET /api/features` — без
правок `main.py`/`index.html`. Скелетонеры: `scripts/new_agent.py`,
`scripts/new_feature.py`. Проверка целостности —
`python -m src.cli validate`.

---

## 8. Данные и хранилища

| Хранилище | Что лежит | Когда нужно |
|---|---|---|
| `data/*.sqlite` | журнал, эпизодическая память, кэш извлечения, сессии | всегда (локальные файлы) |
| LanceDB | векторные индексы | всегда (или `PRIMARY_VECTOR_STORE=postgres/files`) |
| PostgreSQL + pgvector | зеркала журнала/сессий/планов, память агентов, аналитика, CDC | опционально (`PG_ENABLED`) |
| Apache AGE (порт 5433) | property-граф сущностей | опционально (`AGE_ENABLED`) |
| Kafka (9092) | поток CDC-событий | опционально (`KAFKA_ENABLED`) |
| Redis (6379) | шина мульти-инстанса | опционально (`CLUSTER_ENABLED`) |
| `data/plans/`, `data/sessions/` | состояние планов и диалогов | Supervisor (переживает рестарт) |
| `logs/` | логи сервера | всегда |

Без PostgreSQL система полноценно работает на локальных файлах;
подробности и схема — [DATABASE.md](DATABASE.md).

---

## 9. LLM-слой

- **Реестр провайдеров** — `config/models.yaml`: любой OpenAI-совместимый
  endpoint описывается блоком `providers:` с подстановкой `${VAR:default}`;
  `/api/models` опрашивает `…/v1/models` и показывает причины
  недоступности.
- **Пути**: qwenproxy (браузерный роутер Qwen, порт 7936), куки веб-чатов
  DeepSeek/Qwen, прямые API (DeepSeek, VseGPT, ProxyAPI, OpenRouter…),
  Ollama локально (11434), LiteLLM-шлюз для не-OpenAI API.
- **Надёжность**: цепочка фолбэков (`LLM_FALLBACKS`), ретраи вызова
  (3 попытки), кэш ответов (`CACHE_ENABLED`), дружелюбные ошибки —
  403-регион/401/404/429 объясняются пользователю человеческим языком
  (`src/llm_errors.py`).

---

## 10. Отказоустойчивость и безопасность

| Механизм | Где | Что даёт |
|---|---|---|
| Бюджеты агента | `agent.yaml` + `LOOP_MAX_ITERATIONS/TOKENS/HARD_LIMIT` | агент не уходит в бесконечный цикл |
| Бюджеты плана | Supervisor: max_steps (12), max_replans (2) | Supervisor не «разгоняется» |
| Ретраи LLM | `LLM_CALL_MAX_ATTEMPTS=3` | переходный сбой провайдера не роняет шаг |
| Fault-инъекции | `src/loop/faults.py` | хаос-тесты восстановления |
| Политики + approval | `src/policies.py`, `MCPManager` | опасные действия — только после подтверждения |
| Snapshot + A/B | `src/core/snapshot.py` | роутер-промпт из фактического состояния; эксперименты |
| Аудит | `src/core/audit.py` | кто и что менял в настройках |
| Журнал + rollback | `src/journal/` | откат изменений файлов с детекцией конфликтов |
| Алерты | `config/alerting.yaml` | баннер в UI: БД недоступна, LLM медленный, MCP падает |
| Health | `src/core/health.py`, `/api/db/status` | `{"ok": true}` как признак живости |

---

## 11. Тестирование и CI

- **CLI**: `python -m src.cli validate` — валидация всех деклараций;
  `harness run/tag/all` — прогон сценариев (`--mock-llm --mock-mcp`).
- **Harness** (`src/harness/`): runner с бюджетами, mock-LLM/MCP,
  chaos-фолты, fixtures (записанные ответы LLM для replay), baselines,
  benchmark, отчёты. 32 сценария в 9 группах (smoke, dev, document,
  browser, image, media, storage, chaos, 1c).
- **CI** (`.github/workflows/`): `validate.yml` + `harness.yml` —
  жёсткий гейт: validate → файловые сценарии → smoke (31) → all (32).
- Юнит/смоук-тесты — `tests/`; сценарные проверки — `scripts/test_*.py`.

---

## 12. Карта документации

| Документ | О чём |
|---|---|
| [GETTING_STARTED.md](GETTING_STARTED.md) | установка, настройка, порты, диагностика |
| [STRUCTURE.md](STRUCTURE.md) | карта репозитория: какой файл за что отвечает |
| [ARCHITECTURE-V2.md](ARCHITECTURE-V2.md) | дизайн Supervisor-цикла и фич-манифестов, этапы |
| [DATABASE.md](DATABASE.md) | PostgreSQL, схема, автодетект, AGE/Kafka |
| [CAPABILITIES.md](CAPABILITIES.md) | система capabilities |
| [providers.md](providers.md) | подключение LLM-провайдеров |
| [FAQ.md](FAQ.md) | типовые проблемы |
| [JOURNAL.md](JOURNAL.md) | подсистема журнала подробно |

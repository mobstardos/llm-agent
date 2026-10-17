# План декомпозиции `src/main.py` (2 640 строк)

> Рекомендация 1 из технического отчёта. Этот документ описывает,
> как распилить монолитный `src/main.py` на FastAPI-роутеры без
> изменения поведения системы. Оценка: 2-3 спринта, сложность —
> средняя (механический рефакторинг, но требует внимания к
> shared state и lifespan).

## Контекст

`src/main.py` — крупнейший файл проекта (2 640 строк, 95 top-level
функций, 86 FastAPI-эндпоинтов). Содержит в одном модуле:

- Объявление FastAPI-приложения и `lifespan` (инициализация всех
  подсистем: cache, policies, llm, memory, registry, mcp, journal,
  ollama, supervisor, background scheduler, file watcher).
- 86 HTTP-эндпоинтов, разбитых на 16 функциональных групп.
- WebSocket `/ws` (главный обработчик чата, ~480 строк).
- Вспомогательные функции (`_do_backup`, `_task_memory_cleanup`,
  `_task_auto_index`, `_parse_policies_file`, `_classify_policy`,
  `_sync_mcp`, `_sync_runtime`, `_policies_store`).
- Промежуточное состояние в глобальном `state: dict` (policies,
  orchestrator, journal, ws_clients, plans, memory, mcp, registry,
  llm, cache, supervisor).
- Обработчики статических страниц (`/`, `/analytics`, `/guide`,
  `/favicon.ico`, `/metrics`).

Проблема: любое изменение в одном эндпоинте требует перечитывать
весь файл. Pull requests конфликтуют. Code review неэффективен.
Тестирование изолированных групп эндпоинтов невозможно без поднятия
всего приложения.

## Цель

Распилить `src/main.py` на 16 модулей-роутеров (по одному на
функциональную группу эндпоинтов) + 1 `app.py` с инициализацией
FastAPI-приложения и lifespan + 1 `state.py` с типизированным
глобальным состоянием. WebSocket `/ws` вынести в отдельный модуль.

## Структура после рефакторинга

```
src/
├── main.py              ← точка входа: создаёт app, импортирует роутеры, стартует uvicorn
├── app.py               ← FastAPI-приложение + lifespan + middleware
├── state.py             ← типизированное глобальное состояние (AppState)
├── routes/              ← 16 модулей-роутеров
│   ├── __init__.py
│   ├── static.py        ← GET /, /favicon.ico, /metrics, /analytics, /guide
│   ├── plans.py         ← GET /api/plans, /api/plans/{plan_id}
│   ├── features.py      ← GET /api/features
│   ├── models.py        ← GET /api/models, POST /api/model/select
│   ├── sessions.py      ← GET /api/sessions
│   ├── policies.py      ← 12 эндпоинтов /api/policies/* (backup, import, export...)
│   ├── chats.py         ← POST /api/chats/import, /api/digest/*
│   ├── project.py       ← GET/POST /api/project, /api/project/set
│   ├── registry.py      ← 25 эндпоинтов /api/registry/* (агенты, mcp, capabilities, profiles, audit, rollback)
│   ├── cache.py         ← GET /api/cache/stats
│   ├── database.py      ← 6 эндпоинтов /api/db/* (health, migrate-vectors, migrate-memory, autodetect, status)
│   ├── enrichment.py    ← GET /api/enrichment/status
│   ├── analytics.py     ← 10 эндпоинтов /api/analytics/* (overview, daily, hourly, agents, topics, tools, files, mv-info)
│   ├── graph.py         ← 6 эндпоинтов /api/age/* (stats, cypher, impact, who-uses, concepts, sync) + /api/cdc/status
│   ├── search.py        ← 3 эндпоинтов /api/search/* + 3 эндпоинта /api/synonyms
│   └── backup.py        ← 4 эндпоинта /api/backup/* (list, create, restore, delete)
└── ws/
    ├── __init__.py
    └── chat.py          ← WebSocket /ws (480 строк, главный обработчик чата)
```

Итог: вместо одного файла на 2 640 строк — 18 файлов, самый большой
из которых — `routes/registry.py` (~600 строк, 25 эндпоинтов) и
`ws/chat.py` (~480 строк). Средний размер — ~150-200 строк на
модуль-роутер.

## Распределение 86 эндпоинтов по модулям

| Модуль | Эндпоинтов | Строк (оценка) | Сложность |
|---|---|---|---|
| `routes/registry.py` | 25 | ~600 | Средняя — зависит от state + registry + audit |
| `routes/policies.py` | 12 | ~350 | Низкая — чистые CRUD + file I/O |
| `routes/analytics.py` | 10 | ~200 | Низкая — read-only запросы к materialized views |
| `routes/database.py` | 6 | ~120 | Низкая — обёртки над src/db/* |
| `routes/graph.py` | 7 | ~150 | Низкая — обёртки над src/db/age_* |
| `routes/search.py` | 6 | ~120 | Низкая — обёртки над hybrid_search |
| `routes/backup.py` | 4 | ~80 | Низкая — обёртки над BackupManager |
| `routes/static.py` | 5 | ~80 | Низкая — возврат FileResponse |
| `routes/policies.py` | 12 | ~350 | (см. выше) |
| `routes/plans.py` | 2 | ~30 | Тривиальная |
| `routes/models.py` | 2 | ~60 | Тривиальная |
| `routes/chats.py` | 3 | ~80 | Низкая |
| `routes/project.py` | 2 | ~40 | Тривиальная |
| `routes/features.py` | 1 | ~20 | Тривиальная |
| `routes/sessions.py` | 1 | ~20 | Тривиальная |
| `routes/cache.py` | 1 | ~20 | Тривиальная |
| `routes/enrichment.py` | 1 | ~50 | Низкая |
| `ws/chat.py` | 1 (WS) | ~480 | **Высокая** — стриминг токенов, approval gate, сессии, восстановление |
| `app.py` | — | ~250 | Высокая — lifespan с инициализацией всех подсистем |
| `state.py` | — | ~80 | Низкая — dataclass с типами |

## Архитектурные решения

### 1. Типизированное глобальное состояние (state.py)

Сейчас состояние раскидано по глобальному `state: dict` (строки 334,
357, 446, 2175, 2460 и др.) — это не типобезопасно и затрудняет
рефакторинг. Заменить на dataclass:

```python
# src/state.py
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class AppState:
    """Глобальное состояние приложения, инициализируется в lifespan."""
    cache: Optional['ResponseCache'] = None
    policies: Optional['PolicyStore'] = None
    llm: Optional['LLMClient'] = None
    memory: Optional['Memory'] = None
    registry: Optional['Registry'] = None
    mcp: Optional['MCPManager'] = None
    orchestrator: Optional['Orchestrator'] = None
    plans: Optional['PlanRegistry'] = None
    journal: Optional['Journal'] = None
    supervisor: Optional['Supervisor'] = None
    ws_clients: set = field(default_factory=set)
    local_chat_active_until: float = 0.0

# Глобальный синглтон
state = AppState()

# В маршрутах — доступ через зависимости FastAPI:
from fastapi import Depends
from src.state import state

def get_policies() -> PolicyStore:
    if state.policies is None:
        raise HTTPException(503, "Policies not initialized")
    return state.policies

@router.get("/api/policies")
async def policies_list(policies: PolicyStore = Depends(get_policies)):
    return policies.list_all()
```

### 2. FastAPI Router pattern

Каждый модуль-роутер экспортирует `router = APIRouter(prefix=...)`.
Регистрация в `app.py`:

```python
# src/app.py
from fastapi import FastAPI
from .state import state
from .routes import (
    static, plans, features, models, sessions, policies,
    chats, project, registry, cache, database, enrichment,
    analytics, graph, search, backup,
)
from .ws.chat import ws_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # ... инициализация state (250 строк) ...
    yield
    # ... shutdown ...

app = FastAPI(lifespan=lifespan)
app.include_router(static.router)
app.include_router(plans.router, prefix="/api/plans", tags=["plans"])
app.include_router(policies.router, prefix="/api/policies", tags=["policies"])
app.include_router(registry.router, prefix="/api/registry", tags=["registry"])
# ... и т.д. для всех 16 модулей ...
app.include_router(ws_router)
```

### 3. WebSocket — отдельный модуль

`ws/chat.py` (~480 строк) — самый сложный кусок. Содержит:

- Accept + сессия журнала (recorder `_cv_session`)
- Async queue для входящих сообщений
- Pending approvals dict (Future для каждого tool-call, ждущего подтверждения)
- ConversationSession (supervisor.session)
- Intent classification (supervisor.intents)
- Token streaming через LLM client
- Plan execution через supervisor
- Session restore (hello protocol)
- Disconnect cleanup

Выносится в `src/ws/chat.py` как `ws_router = APIRouter()` с одним
`@ws_router.websocket("/ws")`. Никаких изменений в логике — только
перемещение.

### 4. Lifespan — выделить в `app.py`

`lifespan(app)` сейчас ~200 строк (строки 137-340) и инициализирует
10 подсистем. Оставить в `app.py`, но разбить на шаги через
вспомогательные функции:

```python
# src/app.py
async def _init_postgres(state, settings):
    ...

async def _init_cache_and_policies(state, settings):
    ...

async def _init_llm_and_memory(state, settings):
    ...

async def _init_registry_and_mcp(state, settings):
    ...

async def _init_journal_and_supervisor(state, settings):
    ...

async def _init_background_tasks(state, settings):
    ...

@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    cfg = get_yaml_config()
    await _init_postgres(state, s)
    _init_cache_and_policies(state, s)
    _init_llm_and_memory(state, s)
    await _init_registry_and_mcp(state, s)
    _init_journal_and_supervisor(state, s)
    _init_background_tasks(state, s)
    yield
    # ... shutdown ...
```

## Этапы рефакторинга (3 спринта)

### Спринт 1.A — Подготовка (3-4 дня)

- [ ] Создать `src/state.py` с dataclass AppState.
- [ ] Создать пустые `src/routes/__init__.py` и `src/ws/__init__.py`.
- [ ] Добавить `get_*()` dependency-injection функции в `state.py`.
- [ ] Создать `src/app.py` с FastAPI-приложением и lifespan
      (скопировать из main.py, делегировать инициализацию в
      вспомогательные функции).
- [ ] В `src/main.py` оставить только `from src.app import app;
      if __name__ == "__main__": uvicorn.run(app, ...)` — точка входа.
- [ ] Запустить smoke-тест: сервер должен стартовать, все эндпоинты
      должны работать (через `python -m src.main`).

### Спринт 1.B — Механическое извлечение роутеров (5-7 дней)

Извлекать эндпоинты по одной функциональной группе, от простой к
сложной. Порядок:

1. `routes/static.py` (5 эндпоинтов, ~80 строк) — простые FileResponse.
2. `routes/cache.py`, `routes/features.py`, `routes/sessions.py`,
   `routes/plans.py`, `routes/project.py`, `routes/models.py` —
   тривиальные (1-2 эндпоинта каждый).
3. `routes/database.py`, `routes/enrichment.py`, `routes/graph.py`,
   `routes/search.py`, `routes/backup.py` — обёртки над src/db/*.
4. `routes/analytics.py` — 10 read-only эндпоинтов.
5. `routes/policies.py` — 12 эндпоинтов с CRUD и file I/O.
6. `routes/registry.py` — 25 эндпоинтов, самая большая группа.
7. `routes/chats.py` — 3 эндпоинта с file upload.

После каждого шага запускать `python -m src.main` и прогонять
ручной smoke-тест: `curl http://127.0.0.1:8000/api/<группа>` —
сервер должен отдавать тот же ответ, что и до рефакторинга.

### Спринт 1.C — WebSocket (3-4 дня)

- [ ] Вынести `ws_endpoint` в `src/ws/chat.py` как `ws_router`.
- [ ] Перенести все локальные замыкания (incoming, pending_approvals,
      make_session, restore_hello, и т.д.) — без изменений логики.
- [ ] Заменить доступ к `state["..."]` на `state.<field>` через
      dependency injection.
- [ ] Smoke-тест: открыть веб-чат, отправить сообщение, дождаться
      ответа, проверить approval flow, проверить восстановление сессии.

### Спринт 1.D — Cleanup (2-3 дня)

- [ ] Удалить из `src/main.py` все перенесённые функции.
- [ ] Оставить только точку входа (5 строк).
- [ ] Запустить `pytest` (если есть тесты), `ruff check`, `mypy`.
- [ ] Прогнать harness-сценарии: `python -m src.harness scenarios/smoke/`.
- [ ] Обновить `docs/STRUCTURE.md` с новой структурой `src/routes/`.
- [ ] Отметить выполненную рекомендацию 1 в `SPRINT-ROADMAP.xlsx`.

## Риски и мигтации

| Риск | Вероятность | Митигация |
|---|---|---|
| Circular imports (state ← routes ← app) | Высокая | `state.py` не импортирует routes. `routes/*` импортируют только `state` и `src/*` подсистемы. `app.py` импортирует routes последним. |
| Shared mutable state race conditions | Низкая (всё в одном event loop) | Dependency injection через `Depends(get_policies)` — FastAPI вызывает функцию на каждый запрос, но возвращает тот же объект. |
| WebSocket handler — регрессия стриминга | Средняя | Перед рефакторингом записать e2e-тест (Playwright, открыть чат, отправить сообщение, проверить токены). После — прогнать тот же тест. |
| Lifespan ordering — подсистемы зависят от порядка | Высокая | Сохранить тот же порядок инициализации, что и в текущем lifespan (PG → cache → policies → llm → memory → registry → mcp → journal → supervisor → background). |
| Пропущенные хелперы (например, `_policies_store()`) | Средняя | Перед рефакторингом каждой группы — `grep -n "_helper_name" src/main.py` и вынести хелпер в `src/routes/_helpers.py` или в сам модуль-роутер. |

## Критерии готовности

- [ ] `src/main.py` ≤ 30 строк (точка входа + uvicorn.run).
- [ ] `src/app.py` ≤ 250 строк (FastAPI-инициализация + lifespan).
- [ ] `src/state.py` ≤ 100 строк (dataclass + get_* функции).
- [ ] Ни один файл в `src/routes/` не превышает 600 строк.
- [ ] Все 86 эндпоинтов работают (smoke-тест через `curl`).
- [ ] WebSocket `/ws` работает (ручной тест веб-чата).
- [ ] `ruff check src/` — без ошибок.
- [ ] `mypy --strict src/` — без ошибок (опционально, если включён).
- [ ] `pytest tests/` — без регрессий.

## Связанные рекомендации

Этот рефакторинг — предпосылка для:
- **Рекомендация 6** (mypy-gate): типизированное состояние упрощает
  проверку типов.
- **Рекомендация 7** (pytest workflow): теперь можно писать тесты на
  отдельные роутеры через `TestClient(app)` с моками зависимостей.
- **Рекомендация 12** (logging вместо print): при выносе из main.py
  заменить print() на logger в каждом роутере.

## Связанные документы

- **`ARCHIVES-DIFF.md`** — документация разницы архивов (рекомендация 3).
- **`SPRINT-ROADMAP.xlsx`** — трекер всех 14 задач.
- Технический отчёт `llm-agent-archives-analysis.pdf` — исходный анализ.

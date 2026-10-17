# Архитектура V2: структура, унификация расширения, чат-оркестрация

> Документ-предложение. Фиксирует (1) как унифицировать добавление агентов,
> MCP-серверов и подсистем, (2) как перевести чат на модель «Supervisor:
> программа сама распределяет действия агентов по контексту диалога и
> ответов». Привязано к реальному коду репозитория.

---

## 0. Резюме

Проект уже имеет сильный декларативный фундамент: реестр агентов
(`agents/*/agent.yaml`), MCP-серверов (`mcp_servers/*/server.yaml`),
capabilities и loop-спеков (`loops/*.yaml`), единую точку tool-calls
(`MCPManager.call_tool`), LoopController с бюджетами и телеметрией,
snapshot/A-B и журнал действий.

Двух не хватает:

1. **Расширяемость неоднородна.** Добавить агента — легко (декларация),
   но добавить *подсистему* (как журнал) — значит вручную патчить
   `main.py`, `index.html`, `orchestrator.py`. Нет «feature»-абстракции,
   скелетонера и событийной шины.
2. **Чат не контекстен.** Роутинг однораундовый (`route()` видит только
   текущий запрос), выбранного списка агентов достаточно — `handle()`
   выполняет их последовательно на одном и том же тексте, результаты
   одного агента не передаются следующему, репланирования нет. Программа
   не «сама распределяет действия по контексту ответа» — она делает один
   статический выбор и запускает его.

Предложение вводит **единый принцип «всё — директория с манифестом»**
(включая подсистемы: `features/*/feature.yaml`) и **Supervisor-цикл
Plan → Execute → Observe → Re-plan** поверх существующих `route()` /
`BaseAgent.run()` / `LoopController`, без их поломки.

---

## 1. Диагноз: что есть и где боль

### 1.1 Что уже хорошо (сохраняем как есть)

| Механизм | Где | Почему это ценно |
|---|---|---|
| Декларативный реестр | `src/core/loader.py`, `src/core/schema.py` | Ошибка одной декларации не ломает остальные; pydantic-валидация |
| Роутер-хинты в yaml | `routing_hints.keywords / negative_keywords / description_for_router` | Данные для маршрутизации уже лежат рядом с агентом |
| Единая точка tool-calls | `src/mcp_manager.py → call_tool()` | Журнал/политики/approval перехватывают ВСЕ вызовы в одном месте |
| Loop-спеки | `loops/*.yaml`, `src/loop/controller.py` | reasoning/verification/retry — декларативно, с бюджетами и телеметрией |
| Snapshot + A/B | `src/core/snapshot.py` | Роутер-промпт строится из фактического состояния агентов |
| Журнал | `src/journal/` | Мягкие хуки: нет рекордера — нет затрат |
| Дружелюбные ошибки LLM | `src/llm_errors.py` | 403-регион/401/404/429 объяснены пользователю, а не сырым JSON |

### 1.2 Боль (конкретно по коду)

1. **Роутинг без памяти диалога.** `Orchestrator.route()` получает только
   `query`. `memory.record_user_message()` вызывается в WS, но в промпт
   роутера история не попадает: «а теперь прогони тесты» маршрутизируется
   в вакууме.
2. **Нет исполнителя плана.** После `route()` возвращается список агентов,
   и `handle()` запускает их **на одном и том же `query`** подряд. Результат
   первого агента не становится входом второго (нет pipeline), решение
   «а нужен ли второй» не пересматривается (нет observe/re-plan).
3. **Новый тип функционала = ручные патчи.** Журнал подключался через
   INTEGRATION.md с точечными правками `main.py` (2 блока), `orchestrator.py`,
   `index.html`. Каждый следующий «журнал» повторит этот путь.
4. **UI-вкладки монолитны.** `src/web/app.js` — единый файл; `journal.js`
   сделан самодостаточным как исключение, а не как правило.
5. **Легаси-классы.** `src/agents/*_agent.py` (11 файлов): большинство не
   используется нигде, кроме `src/agents/__init__.py`; система живёт на
   `BaseAgent` + декларациях.
6. **`on_step` мёртв.** Callback объявлен в `LoopContext`, но контроллер его
   не вызывает — UI не видит шагов (обнаружено при восстановлении
   `handle()`).
7. **Импорт-гигиена.** `src/journal/__init__.py` тянет `storage → db.pool →
   psycopg`: импорт хука без установленного psycopg валился с traceback
   (теперь кэшируется и молчит, но сам факт — сигнал о правиле).

### 1.3 Что починено сегодня (баг восстановления песочницы)

- `Orchestrator.handle()` **отсутствовал** (вызов в `src/main.py:1957` падал
  бы с `AttributeError` сразу после роутинга) — метод восстановлен и
  покрыт тестом `scripts/test_orchestrator_handle.py` (21 проверка):
  агрегация результатов, изоляция падения агента, journal-хуки, счётчики.
- Пять файлов `src/journal/` (`__init__, recorder, replay, retention,
  rollback`) восстановлены из архива `llm-agent-journal.zip` (состояние
  Task 2): восстановленная из /tmp копия содержала устаревший `recorder.py`
  без contextvars `_cv_*`, из-за чего `journal.integration` вообще не
  импортировался.
- `main.py`: в `handle()` передаётся `agents=route["agents"]` — убран
  повторный LLM-вызов роутинга.

---

## 2. Принцип унификации: «всё — директория с манифестом»

Одно правило для любого нового элемента:

```
<kind>/<id>/
├── <kind>.yaml      # манифест: pydantic-схема, валидируется при загрузке
├── prompt.md        # LLM-часть (если есть)
├── api.py           # FastAPI-роутер (если есть): create_router(...) -> APIRouter
├── ui.js            # самодостаточная вкладка UI (если есть)
└── README.md        # назначение, переменные окружения, примеры
```

Загрузчик сканирует каталоги вида `<kind>/`, ошибки валидации отдельной
декларации не ломают остальные (уже реализовано в `DeclarationLoader`).

### 2.1 Карта расширения (что добавить → куда)

| Что добавляем | Куда | Манифест | Что происходит автоматически |
|---|---|---|---|
| **Агент** | `agents/<id>/` | `agent.yaml` | requirements-проверка, привязка MCP-серверов, роутер-хинты, карточка в UI, бюджеты |
| **MCP-сервер** | `mcp_servers/<id>/` | `server.yaml` | запуск/остановка, список инструментов, health, per-agent доступ |
| **Capability** | `capabilities/<id>.yaml` | capability-схема | провайдеры и ops для `requires` |
| **Loop-спека** | `loops/<id>.yaml` | `LoopSpec` | доступна агентам через `LoopSpecLoader.get()` |
| **Feature (подсистема)** | `features/<id>/` | `feature.yaml` (новое) | API-роутер в FastAPI, вкладка UI, WS-события, MCP-сервер, миграции |

### 2.2 Feature SDK (закрывает боль №3 и №4)

`feature.yaml` — манифест подсистемы:

```yaml
id: journal
version: "1.0.0"
title: Журнал действий
requires:
  python_packages: [{name: aiosqlite, level: soft}]
api_router: api.py:create_router     # FastAPI include_router(...) при старте
ui_tab: {file: ui.js, title: Журнал, icon: "📜"}
ws_events: [journal.event]           # события, которые фича шлёт в /ws
mcp_server: ../mcp_servers/journal/server.yaml   # если есть
migrations: migrations/              # прогон при первом старте
permissions: {read: [user], write: [admin]}
```

Механика (новый `FeatureLoader`, ~80 строк):
1. сканирует `features/*/feature.yaml`;
2. импортирует `api.py:create_router` и делает `app.include_router()`;
3. кладёт `ui.js` в реестр вкладок: `GET /api/features` →
   `[{id, title, icon, js_url}]`;
4. `app.js` при загрузке рендерит вкладки **из реестра**, а не из
   захардкоженного списка (одноразовый рефакторинг + сам `journal.js`
   уже соответствует контракту «самодостаточная вкладка»).

Итог: новая подсистема = директория + манифест. **Ноль правок**
`main.py` / `index.html`.

### 2.3 Скелетонер (снимает порог «как правильно»)

```
python scripts/new_agent.py my_agent --template reasoning
python scripts/new_feature.py my_feature --with-api --with-ui
```

Генерирует директорию по карте §2.1: yaml-заготовку с комментариями по
каждому полю, prompt.md/user.md, тест-заглушку, затем подсказывает
запустить `python -m src.cli validate` (валидатор уже есть).

### 2.4 Событийная шина (лёгкая, asyncio, stdlib)

`src/events.py`:

```python
async def publish(kind: str, payload: dict) -> None   # task.started, agent.started,
def subscribe(kind: str, handler) -> None             # tool.called, plan.updated, ...
```

- Журнал сегодня монкипатчит `MCPManager.call_tool` — шина делает это
  штатным подписчиком (`tool.called`), монкипатч остаётся как
  fallback-режим.
- UI-логи, мониторинг, будущие фичи — подписчики без правок ядра.
- Пересылка в WS: один мост `events → websocket` вместо ручных
  `send_json` из глубин кода.

### 2.5 Правила гигиены (закрепить после сегодняшних багов)

1. **Мягкие хуки**: сторонний модуль ищется один раз, тихо, кэшируется
   (образец — `_journal()` в `src/orchestrator.py`); запрет
   `logger.exception` на каждый вызов при недоступном стеке.
2. **`__init__.py` пакетов — пустой или лёгкий**: пакетный init не должен
   импортировать БД/сеть (случай `journal/__init__ → psycopg`).
3. **Легаси**: `src/agents/*_agent.py`, не упомянутые вне `__init__`,
   перенести в `attic/` (или удалить) после проверки импортов;
   `src/agents/__init__.py` — реэкспорт только `BaseAgent`, `AgentRuntime`.
4. **Каждому фиксу — тест**: найденные сегодня баги (handle, recorder)
   ловятся юнит-тестом за минуты; правило «баг → тест в scripts/».

---

## 3. Чат-оркестрация: Supervisor (Plan → Execute → Observe → Re-plan)

### 3.1 Целевой пользовательский опыт

Пользователь пишет в обычный чат окна и видит:

1. **Мгновенный отклик** — «понял, план из 3 шагов»: дорожка шагов
   (агент + задача) прямо под сообщением.
2. **Стрим** — токены ответа агента текут в свою ветку шага; шаги с
   инструментами показывают счётчик tool-calls.
3. **Апрувы** — для `mode: destructive`/`dangerous_tools` всплывает уже
   существующий approval-диалог; опционально — «показать план целиком
   перед запуском».
4. **Синтез** — финальное сообщение собирает результаты шагов в один
   ответ человеку.
5. **Контекст** — следующее сообщение («а теперь прогони тесты») понимается
   в контексте выполненного плана, а не с нуля.

### 3.2 Цикл Supervisor

```
сообщение пользователя
        │
        ▼
┌──────────────────┐   скоринг ключевых слов из routing_hints,
│  Intent Layer    │   @упоминания, «продолжай/а теперь», уверенность
└────────┬─────────┘
         │ (низкая уверенность → LLM-роутинг с историей диалога)
         ▼
┌──────────────────┐   Plan {steps[{id, agent, task, depends_on}], reply}
│  Supervisor.plan │   простой вопрос → пустой план + прямой ответ
└────────┬─────────┘
         ▼
┌──────────────────┐   шаги по DAG: параллельно где можно,
│  Executor        │   каждый шаг = BaseAgent.run() (бюджеты из yaml)
└────────┬─────────┘
         ▼
┌──────────────────┐   Observe(step_result): успех → следующий шаг;
│ Supervisor.observe│  ошибка → retry / другой агент / report;
└────────┬─────────┘  появилась новая задача → re-plan (≤ N раз)
         │
         ▼
   итоговое сообщение пользователю (синтез результатов)
```

**Ключевое отличие от текущего `handle()`**: решение о следующем действии
принимается **по контексту ответа агента** (успех/ошибка/содержимое) и
**по контексту диалога** (история сессии) — ровно то поведение, которое
требуется: «программа сама распределяет действия агентов в зависимости от
контекста ответа».

### 3.3 Intent Layer — быстрый путь до LLM

Дешёвые детерминированные проверки (мс, без токенов):

| Сигнал | Источник | Действие |
|---|---|---|
| `@agent_id` в тексте | пользователь | прямой выбор, без LLM |
| Скоринг keywords / negative_keywords | `routing_hints` из yaml (уже есть) | уверенность > порога → выбор без LLM |
| «продолжай / ещё / дальше» | эвристика фраз | повтор/продолжение активного плана |
| Приветствие/благодарность/болтовня | эвристика | пустой план → прямой ответ LLM без агентов |
| Явная ошибка предыдущего шага | сессия | re-plan с учётом ошибки |
| Уверенность низкая | — | LLM-роутинг (как сегодня, но с историей) |

Бонус: при недоступности LLM-провайдера (история с 403) ключевая часть
сценариев продолжает работать.

### 3.4 ConversationSession — состояние диалога

```python
@dataclass
class ConversationSession:
    id: str                          # = ws-сессия
    history: list[Msg]               # role, content, agent?, step_id?
    active_plan: Plan | None
    step_results: dict[str, StepResult]   # сжатые результаты шагов
    prefs: dict                      # модель, auto-approve, ...
```

- Живёт в памяти, дампится в `data/sessions/<id>.jsonl` (перезапуск
  сервера не теряет чат).
- В промпт роутера/планировщика идут: последние N сообщений (сжатые) +
  сводка активного плана (выполненные/ожидающие шаги).
- `Memory facade` (`src/memory/`) остаётся для долгосрочной семантики —
  сессия про «текущий разговор».

### 3.5 Контракты (pydantic, стиль `src/core/schema.py`)

```python
class PlanStep(BaseModel):
    id: str
    agent: str            # должен быть в known-агентах (фильтр как в route())
    task: str             # конкретная подзадача для агента (НЕ сырой запрос)
    depends_on: list[str] = []
    why: str = ""

class Plan(BaseModel):
    intent: str
    steps: list[PlanStep] = []
    reply: str = ""               # что сказать пользователю сразу
    needs_approval: bool = False  # показать план перед выполнением

class ObserveDecision(BaseModel):
    action: Literal["continue", "replan", "finish", "ask_user"]
    updated_plan: Plan | None = None
    message_to_user: str = ""
```

Парсинг ответа LLM — переиспользовать отлаженную схему `route()`:
срез markdown-ограждений → `json.loads` → regex-fallback первого
JSON-объекта → валидация pydantic.

### 3.6 Промпты Supervisor

- `src/prompts/supervisor.py`: системный промпт планировщика.
- Карточки агентов — из существующего `SnapshotBuilder._build_orchestrator_prompt()`
  (id, description_for_router, keywords, режим, недоступные агенты) +
  новые секции: правила декомпозиции, «когда НЕ планировать» (пустой план
  для болтовни — иначе система будет дёргать агентов на «привет»),
  формат ObserveDecision.
- Модель планировщика может отличаться (сильнее) — `model` из конфига.

### 3.7 Безопасность, бюджеты, отказы

| Риск | Механика |
|---|---|
| Бесконечные re-plan | бюджет плана: max_steps (12), max_replans (2), max_total_tokens, wall-clock — поверх агентских бюджетов |
| Опасные действия | per-agent `dangerous_tools` + approval_handler (уже есть); новый флаг `needs_approval` на весь план |
| Недоступный провайдер | `friendly_llm_error` → шаг завершён ошибкой → ObserveDecision.finish с понятным сообщением |
| Провал шага | retry (loop retry существует) → эскалация другому агенту → отчёт пользователю; всё видно в дорожке шагов |
| Аудит | journal task_start/task_end уже в `handle()`; plan.created/updated — в meta задачи; tool-вызовы журналируются как сейчас |

### 3.8 WS-протокол (аддитивный, старые события не меняются)

```jsonc
{"type": "plan",     "steps": [{"id": "1", "agent": "file", "task": "..."}]}
{"type": "step_start", "step_id": "1", "agent": "file"}
{"type": "token",      "step_id": "1", "token": "..."}     // привязка токена к шагу
{"type": "step_done",  "step_id": "1", "ok": true, "summary": "..."}
{"type": "plan_done",  "success": true}
{"type": "plan_approval", "plan": {...}}                    // если needs_approval
```

UI (`src/web/app.js`): «дорожка шагов» под сообщением пользователя —
иконка агента из `ui.icon`, статус, счётчик tool-calls, раскрывающийся
стрим. Это развитие существующего `step_start`-рендера.

### 3.9 Карта внедрения (файлы, минимальный дифф)

| Файл | Изменение |
|---|---|
| `src/supervisor/models.py` | Plan / PlanStep / ObserveDecision + парсеры (сделано) |
| `src/supervisor/intents.py` | Intent Layer (§3.3, сделано в Этапе 1) |
| `src/supervisor/session.py` | ConversationSession + дамп в data/sessions (+ active_plan/step_results в Этапе 2, сделано) |
| `src/supervisor/supervisor.py` | plan/execute/observe; переиспользует `Orchestrator.run_agent()` (публичная обёртка над `_run_one`); бюджеты MAX_STEPS/MAX_REPLANS/MAX_SECONDS (сделано) |
| `src/supervisor/dag.py` | волны зависимостей `plan_waves()`, `deps_satisfied()`, эвристика режима + выключатель `SUPERVISOR_PARALLEL` (Этап 3, сделано) |
| `src/supervisor/plans.py` | `PlanRegistry`: состояние планов в памяти + дамп `data/plans/*.json`, срез `view_for_client` для REST (Этап 3, сделано) |
| `src/events.py` | событийная шина: `publish/subscribe/publish_soon/recent`, изоляция падений подписчиков (Этап 4, сделано) |
| `src/core/features.py` | `FeatureManifest` + `FeatureLoader`: сканирование `features/*/feature.yaml`, requires, api_router («module:func» или «file.py:func»), ui_tab, миграции один раз (Этап 4, сделано) |
| `features/journal`, `features/notes` | journal — вкладка + API из реестра (api_router: `api.py:create_router`, Этап 5); notes — эталонная фича с api_router/ws-событиями (сделано) |
| `scripts/new_agent.py`, `scripts/new_feature.py` | скелетонеры по карте §2.1 (сделано) |
| `src/route_analytics.py`, `scripts/route_report.py` | route-аналитика: JSONL решений/итогов + шина + отчёт «какой агент был нужен на самом деле» и подсказки тюнинга routing_hints (Этап 5, сделано) |
| `src/prompts/supervisor.py` | системные промпты план/observe/синтез (сделано) |
| `src/main.py` | WS: ветка `SUPERVISOR_ENABLED` → supervisor, иначе текущий путь через `handle()` (сделано; env `SUPERVISOR_ENABLED=0` — откат на Этап 1) |
| `src/web/app.js` + `style.css` | дорожка шагов, inline-апрув плана (сделано) |
| `loops/agent.plan.yaml` | (опц.) декларативная спека planning-шага |

`Orchestrator` не выбрасывается: `route()` — это «план из одного шага»,
supervisor его обёртывает; при выключенном флаге всё работает как сегодня.

### 3.10 Этапы

| Этап | Содержимое | Результат |
|---|---|---|
| **0 (сделано)** | `handle()` восстановлен + тесты; journal-пакет восстановлен; мягкие хуки кэшируются | чат снова работает |
| **1 (сделано)** | ConversationSession + Intent Layer + история в `route()` | «продолжай/а теперь» понимаются; дёшево, малый риск |
| **2 (сделано)** | Supervisor MVP: план/execute/observe последовательно, re-plan по контексту ответа, WS-события (`plan/step_start/token/step_done/replan/plan_done`), дорожка шагов в UI, апрув плана, флаг `SUPERVISOR_ENABLED` | программа сама распределяет действия по контексту ответа |
| **3 (сделано)** | DAG-шаги параллельно (`src/supervisor/dag.py`, волны по `depends_on`, семафор `SUPERVISOR_MAX_CONCURRENT`; планы без явных зависимостей — последовательно, как в Этапе 2), долгая память (итоги планов → `memory.log_event_async`, релевантные события → промпт планировщика), `PlanRegistry` (`src/supervisor/plans.py`) + REST `GET /api/plans[/{id}]` + hello/session_id-восстановление после переподключения WS | ускорение параллельных планов, контекст «долгих проектов», план переживает reconnect/рестарт |
| **4 (сделано)** | §2.2–2.4: событийная шина `src/events.py` (+ WS-мост `events → websocket`), `FeatureLoader` (`src/core/features.py`: манифест → API-роутер/вкладка/миграции), `features/journal` (вкладка из реестра) и эталонная `features/notes` (API+UI+события), скелетонеры `scripts/new_agent.py` / `scripts/new_feature.py`, вкладки из реестра `GET /api/features` (app.js) | новый функционал = директория с манифестом, ноль правок ядра |
| **5 (сделано)** | Чистка и обучение: журнал унифицирован на SQLite-поколении (v1.0 PG-бэкенд → `attic/journal-pg`); API `/api/journal/*` перенесён в фичу `features/journal` (роутер `src/journal/api.py` монтируется FeatureLoader'ом); каноническая интеграция `init_journal/instrument_mcp_manager/shutdown_journal` в main.py + WS-сессии журнала; легаси-классы `src/agents/*_agent.py` (11 шт.) и MCP-сервер журнала v1.0 → `attic/`; bugfix: `journal_agent_context` — асинхронный КМ (при живом журнале агент падал); route-аналитика `src/route_analytics.py` (JSONL `data/routing/`, шина `route.decision/outcome`) + хуки в Intent/route()/Supervisor + отчёт-тюнер `scripts/route_report.py` (`--out report.md`) | один бэкенд журнала под тестами/CLI/MCP/UI; новый функционал — только фичи; качество роутинга растёт на своих данных |

---

## 4. Риски и открытые вопросы

1. **Слабая модель планировщика.** Supervisor требует хорошего
   следования JSON-формату. Митигируется Intent Layer (меньше обращений
   к LLM), срезом ограждений/regex-fallback (уже есть) и возможностью
   назначить планировщику отдельную (более сильную) модель.
   Для DAG добавлена страховка: план без явных `depends_on` исполняется
   последовательно (конвейер не сломается параллельным запуском).
2. **Латентность.** План = +1 LLM-вызов. Митигация: пустой план для
   болтовни, Intent Layer для очевидных случаев, кэш маршрутов
   (query-шаблон → агенты).
3. **Пользовательский контроль.** По умолчанию предложить режим
   «показывать план перед выполнением» (`needs_approval: true`), с
   переключателем «автоматически» в настройках.
4. **Совместимость WS.** Все новые события аддитивны; старый фронтенд
   продолжит работать с `handle()`-путём.
5. **Долгие планы и отвал соединения — РЕШЕНО (Этап 3).** Состояние
   планов пишется в `data/plans/*.json`; клиент шлёт `hello`/`session_id`,
   сервер отвечает `session_restored` (история + последний план),
   REST `GET /api/plans[/{id}]` отдаёт состояние из другого соединения.
6. **Параллельные шаги и провайдер.** Волна бьётся семафором
   `SUPERVISOR_MAX_CONCURRENT` (по умолчанию 3) — провайдер не
   молотится N запросами; блокированные упавшей зависимостью шаги
   видны в дорожке как «пропущен» и отдаются наблюдателю.


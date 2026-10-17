# OAC-интеграция (OpenAgents Control → llm-agent)

Источник методологии: репозиторий [alexeyk222/Agents](https://github.com/alexeyk222/Agents) —
**OpenAgents Control (OAC)**, конфигурационный слой для AI-агентов: Markdown-промпты,
специализированные роли, команды и контекстные файлы. Цель OAC — сделать работу
AI-агента **предсказуемой**: сначала понять проект и правила, затем предложить план,
получить подтверждение и только после этого выполнять изменения.

## 1. Ключевые идеи OAC и как они реализованы здесь

| Идея OAC | Реализация в llm-agent |
|---|---|
| Цикл «анализ → план → подтверждение → выполнение → проверка» | `loops/oac_pipeline.yaml` (декларативный конвейер: llm_call → tool_calls c `approval: from_policy` → llm_call → check git_diff/todos/lint) |
| Ролевые агенты (ContextScout, CodeReviewer, TestEngineer, BuildAgent, DocWriter) | Существующие профильные агенты llm-agent — см. таблицу делегирования ниже |
| MVI (Minimal Viable Information) — минимальный достаточный контекст | Шаблон контекстного бандла `agents/_templates/oac/context-bundle-template.md`; агент подгружает только файлы, нужные для текущего шага |
| Навигация по базе знаний (`navigation.md` с приоритетами) | `agents/_templates/oac/navigation.md` — маршруты «задача → где искать контекст» |
| Пометка `[review]` — подтверждение перед действием | Approval gate llm-agent (`dangerous_tools`, `danger: external`, политики `src/policies.py`) |
| Декомпозиция задач | Супервизор (`src/supervisor/`) строит DAG-план; агент `oac_orchestrator` ведёт декомпозицию в диалоге |

## 2. Маппинг ролей OAC → агенты llm-agent

| Роль OAC | Когда | Агент llm-agent (`agents/<id>`) |
|---|---|---|
| **ContextScout** | Перед новой задачей: найти правила, стандарты, примеры | `file`, `document`, `code_analysis` |
| **CodeReviewer** | Ревью без изменения кода | `code_analysis`, `testing` |
| **TestEngineer** | Создание и запуск тестов | `testing` |
| **BuildAgent** | Финальная техническая проверка (type-check, сборка) | `cicd`, `environment` |
| **DocWriter** | Документация по результатам | `documentation` |
| **OpenCoder** | Основная разработка | профильный агент задачи (`frontend`, `db_extended`, `onec_*`, …) |

Оркестратор выбирает агента по `priority` и `routing_hints`; для OAC-сценариев
роутинг подсказывает `agents/oac_orchestrator` (см. ниже).

## 3. Как используется `loops/oac_pipeline.yaml`

Цикл объявлен декларативно (как `loops/reasoning.yaml` / `loops/verification.yaml`)
и загружается реестром петель (`src/loop/spec.py`). Триггер — `manual`:

1. **Анализ** — `llm_call`: агент собирает контекст (MVI-бандл по шаблону
   `agents/_templates/oac/context-bundle-template.md`), формулирует ограничения.
2. **План** — агент предлагает шаги; опасные вызовы инструментов проходят через
   `approval: from_policy` (approval gate — модальное окно подтверждения).
3. **Выполнение** — основной цикл LLM ↔ tools (`tool_calls` + `llm_call`).
4. **Проверка** — `check`-шаги: `git_diff` (изменения есть), `todos` (нет
   заглушек TODO/FIXME/stub), `lint` (`ruff check`).

Провал проверки → `on_failure: agent.retry`, таймаут → `agent.escalation`.

## 4. Принцип MVI и шаблон контекстного бандла

MVI — в контекст попадает **минимально достаточная** информация: навигация →
2–4 релевантных файла → план. Полный шаблон бандла:
`agents/_templates/oac/context-bundle-template.md`. Кратко:

````markdown
# Контекстный бандл: <задача>
## 1. Задача (1–2 строки)
## 2. Релевантный контекст (только затронутые файлы, с путями)
## 3. Ограничения и стандарты проекта (пункты, влияющие на задачу)
## 4. План (шаги, у каждого: что, чем, [review] если нужно подтверждение)
## 5. Критерии проверки (как поймём, что готово)
````

Загрузчик llm-agent пропускает служебные директории, начинающиеся с `_`
(`agents/_templates/` не попадает в реестр агентов) — шаблоны хранятся там.

## 5. Агент `oac_orchestrator`

`agents/oac_orchestrator/` — «дирижёр» методологии: получает сложную задачу,
проводит её по циклу анализ → план → подтверждение → выполнение → проверка и
**делегирует** шаги профильным агентам по таблице из §2. У него нет собственных
MCP-инструментов изменения данных — он работает через оркестратор и отчёты
делегированных агентов.

## 6. Файлы интеграции

| Файл | Назначение |
|---|---|
| `loops/oac_pipeline.yaml` | Декларативный OAC-конвейер (тип `reasoning` — см. примечание в файле) |
| `agents/oac_orchestrator/` | Агент-оркестратор методологии |
| `agents/_templates/oac/context-bundle-template.md` | Шаблон MVI-контекстного бандла |
| `agents/_templates/oac/navigation.md` | Навигация «задача → контекст» по проекту |
| Этот документ | Описание интеграции |

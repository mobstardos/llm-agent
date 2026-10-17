<!--
Навигация по контексту (по мотивам .opencode/context/navigation.md из
OpenAgents Control, alexeyk222/Agents). Служебные каталоги с префиксом "_"
загрузчиком агентов игнорируются — шаблоны живут здесь безопасно.
-->

# Навигация по контексту проекта

**Впервые в проекте?** → `docs/GETTING_STARTED.md` → `docs/ARCHITECTURE-V2.md`

## Структура базы знаний

```
<корень проекта>/
├── docs/                       # архитектура, возможности, FAQ, безопасность
├── agents/<id>/                # декларации агентов (agent.yaml + prompt.md)
├── mcp_servers/<id>/           # декларации MCP-серверов (server.yaml)
├── src/mcp_servers/<id>/       # реализации MCP-серверов (server.py)
├── loops/                      # декларативные циклы (reasoning, verification,
│                               # oac_pipeline)
├── config/                     # settings.yaml, models.yaml, memory.yaml…
└── agents/_templates/oac/      # шаблоны OAC (этот каталог; "_" = служебный)
```

## Быстрые маршруты

| Задача | Путь | Приоритет |
|------|------|----------|
| Понять архитектуру | `docs/ARCHITECTURE-V2.md` | ⭐⭐⭐⭐⭐ |
| Возможности системы | `docs/CAPABILITIES.md` | ⭐⭐⭐⭐ |
| Добавить агента | `agents/<пример>/agent.yaml` + `docs/CONTRIBUTING.md` | ⭐⭐⭐⭐ |
| Добавить MCP-сервер | `mcp_servers/<пример>/server.yaml` + `src/mcp_servers/<пример>/server.py` | ⭐⭐⭐⭐ |
| Правила безопасности | `docs/SECURITY.md` | ⭐⭐⭐⭐⭐ |
| OAC-методология | `docs/OAC_INTEGRATION.md` | ⭐⭐⭐ |
| Шаблон контекстного бандла | `agents/_templates/oac/context-bundle-template.md` | ⭐⭐⭐ |
| Типовые проблемы | `docs/FAQ.md` | ⭐⭐ |

## Стратегия загрузки (MVI)

1. Начинай с навигации (этот файл) — не читай проект целиком.
2. Для задачи возьми 2–4 релевантных файла и зафиксируй их в контекстном
   бандле (`agents/_templates/oac/context-bundle-template.md`).
3. Перед изменениями собери план и получи подтверждение (см.
   `loops/oac_pipeline.yaml`, шаги план/подтверждение).

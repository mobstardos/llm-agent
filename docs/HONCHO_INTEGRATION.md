# Honcho — межсессионная память агентов (интеграция в llm-agent)

Honcho ([honcho.dev](https://honcho.dev), [github.com/plastic-labs/honcho](https://github.com/plastic-labs/honcho))
— open-source «AI memory for agents» от **Plastic Labs** (лицензия
**AGPL-3.0**, Python + TypeScript), SOTA-решение на бенчмарке LongMemEval.
Honcho помнит пользователя **между диалогами**: строит представления о
пирах, выводит персонализированные выводы и отдаёт их агентам через MCP.

Доступны два варианта развёртывания:

- **Облако** — `api.honcho.dev` + MCP `mcp.honcho.dev`; ключи формата
  `hch-…` выдаются в консоли [app.honcho.dev](https://app.honcho.dev).
- **Self-hosted** — Docker-стек `api + deriver + Postgres + Redis`
  (`docker compose up` или `./honcho start`); MCP-сервер поднимается
  отдельно из каталога `mcp/` репозитория.

---

## Зачем он в llm-agent

- **Межсессионная память агентов**: встроенная память llm-agent живёт в
  PostgreSQL/SQLite и помнит факты и события, но не «понимание
  пользователя». Honcho добавляет слой персонализации: что за человек
  с тобой говорит, каков его стиль, что он предпочитает, каково его
  типичное состояние — и это переживает перезапуск системы.
- **Связь с OAC**: в README [alexeyk222/Agents](https://github.com/alexeyk222/Agents)
  (OpenAgents Control) заявлена «Honcho — интеграция с межсессионной
  памятью». Наш `oac_orchestrator` (конвейер анализ → план →
  подтверждение → выполнение → проверка) получает долговременный слой
  памяти через агента `honcho_memory`: предпочтения оператора и выводы
  прошлых сессий доступны при планировании.

---

## Архитектура

```
┌──────────────────┐   stdio (JSON-RPC)   ┌───────────────────────────┐
│ агент            │◄────────────────────►│ MCP-прокси honcho         │
│ honcho_memory    │                      │ src/mcp_servers/honcho/   │
│ (agents/…)       │                      │ (python -m …honcho.server)│
└──────────────────┘                      └────────────┬──────────────┘
                                                       │ Streamable HTTP
                                                       │ Authorization: Bearer hch-…
                                                       │ X-Honcho-Workspace-ID (опц.)
                                                       ▼
                                          ┌───────────────────────────┐
                                          │ Официальный MCP Honcho    │
                                          │ https://mcp.honcho.dev    │
                                          │ или self-hosted :3000/mcp │
                                          └────────────┬──────────────┘
                                                       │
                                          ┌────────────▼──────────────┐
                                          │ Honcho: reasoning-пайплайн│
                                          │ api + deriver + Postgres  │
                                          │ + Redis (peers, sessions, │
                                          │ conclusions, dreams)      │
                                          └───────────────────────────┘
```

Прокси — это клон проверенного паттерна `onec_qa`: ленивый `initialize`
под блокировкой → `Mcp-Session-Id` (если upstream stateful; hosted
эндпоинт может работать без сеанса — тогда заголовок не отправляется) →
`tools/call`; ответы `application/json` или SSE (`data:`-строки); 404 /
потеря сеанса → re-initialize и один ретрай. У Honcho нет `/healthz` —
ошибки диагностируются по HTTP-кодам и JSON-RPC.

Таймауты: **5 с** подключение/initialize, **30 с** обычные операции,
**120 с** `chat`/`workspace_chat` (живой reasoning — ответ приходит за
5+ секунд).

---

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Описание |
|---|---|---|---|
| `HONCHO_API_KEY` | да (для вызовов) | — | Ключ `hch-…` из [app.honcho.dev](https://app.honcho.dev); передаётся как `Authorization: Bearer` на **каждый** запрос, включая `initialize` |
| `HONCHO_MCP_URL` | нет | `https://mcp.honcho.dev` | Официальный MCP-эндпоинт; для self-hosted — например `http://127.0.0.1:3000/mcp` |
| `HONCHO_WORKSPACE_ID` | нет | — | Если задан — каждый запрос идёт с заголовком `X-Honcho-Workspace-ID`; иначе `workspace_id` передаётся в аргументах инструментов |

Переменные прописаны в `mcp_servers/honcho/server.yaml` и
`config/settings.yaml` (секция `mcp_servers.honcho`, стиль `${VAR}`).
Значения задавайте в `.env` — ключ в репозиторий не попадает.

---

## Инструменты (21)

`tools/list` прокси динамический: каталог запрашивается из upstream
(с TTL-кэшем). Если upstream недоступен — статический фолбэк-каталог
ниже (точно он же отдаёт `honcho_tools_list`, поэтому обнаружение
работает офлайн). `tools/call` — чистый passthrough: валидация
аргументов на стороне upstream.

### Recall — только чтение (режим по умолчанию)

| Инструмент | Описание | Пометки |
|---|---|---|
| `list_workspaces` | Список workspaces (workspace → peers → sessions) | read |
| `list_peers` | Пиры workspace — люди и агенты | read |
| `get_peer_card` | Биографическая карточка пира (стабильные факты) | read |
| `list_sessions` | Список сессий-«корзин» контекста | read |
| `get_session_context` | Сводка диалога сессии с точки зрения пира | read |
| `get_peer_context` | Всё, что Honcho знает о пире | read |
| `get_representation` | Текстовая сводка-представление пира | read |
| `chat` | Reasoned-ответ о пользователе из представления пира | read, **долго** (до 120 с) |
| `workspace_chat` | Как `chat`, но на уровне workspace | read, **долго** |
| `search` | Семантический поиск по памяти | read |
| `list_conclusions` | Выводы о пире (уровни explicit/deductive/inductive/contradiction, source_ids, times_derived) | read |
| `get_conclusions` | Выводы пира с деревом источников (вниз по source_ids) | read |
| `get_derived_conclusions` | Выводы, выведенные из указанного (вверх по дереву) | read |

### Memory store — запись (если пользователь попросил запоминать)

| Инструмент | Описание | Пометки |
|---|---|---|
| `create_workspace` | Создать workspace | mutating |
| `create_peer` | Создать пира (один стабильный peer_id на человека) | mutating |
| `set_peer_card` | Перезаписать peer card | mutating |
| `create_session` | Создать сессию (одна на тред/проект, переиспользовать) | mutating |
| `add_peers_to_session` | Добавить пиров в сессию (observe_me/observe_others) | mutating |
| `add_messages_to_session` | **Ядро record-цикла**: записать сообщения ОБЕИХ сторон диалога | mutating |

### Служебные

| Инструмент | Описание | Пометки |
|---|---|---|
| `schedule_dream` | Запланировать «сон» — фоновую консолидацию памяти | mutating |
| `honcho_tools_list` | Каталог с RU-описаниями, режимами и состоянием конфигурации | read, работает офлайн |

Mutating-инструменты (`create_workspace`, `create_peer`, `create_session`,
`add_peers_to_session`, `add_messages_to_session`, `set_peer_card`,
`schedule_dream`) помечены `danger: external` в `server.yaml` и
перечислены в `dangerous_tools` агента — проходят approval gate.

---

## Первый сеанс (memory store)

Сценарий «научить систему помнить»:

```
1. create_workspace            # если нужен отдельный workspace (опционально)
2. create_peer                 # пир пользователя (стабильный peer_id!)
3. create_session              # сессия-«корзина» (одна на проект/тред)
4. add_peers_to_session        # peer_ids=[…], observe_me=true
5. add_messages_to_session     # ОБЕ стороны диалога: сообщения пользователя
                               # и ответы агента, с указанием автора
6. … работа …                  # reasoning фоновый — выводы появятся сами
7. get_representation / list_conclusions / get_peer_card   # позже, recall
8. schedule_dream              # по желанию — плановая консолидация
```

## Режим Recall (по умолчанию)

Пользователь спрашивает «что ты знаешь обо мне?»:

```
get_peer_card → get_representation / get_peer_context → search
→ (если нужен осмысленный вывод) chat
```

Примеры вопросов к `chat`: «Какой у пользователя стиль общения и уровень
формальности?», «Какие темы его волнуют в этом проекте?», «Каково его
вероятное эмоциональное состояние по последним сообщениям?». Сначала
дешёвые чтения, `chat` — только если их не хватило (живой reasoning,
5+ секунд, до 120 с).

---

## Связь с OAC

- `oac_orchestrator` реализует методологию OpenAgents Control
  (см. `loops/oac_pipeline.yaml`, `docs/OAC_INTEGRATION.md`).
- В README OAC заявлена «Honcho — интеграция с межсессионной памятью».
- В llm-agent эта связка собрана так: на этапе **анализ/план** оркестратор
  может спросить `honcho_memory` (recall: preferences/style/выводы), а
  после **выполнения/проверки** — записать итоги сессии через
  `add_messages_to_session` (record). Оператору достаточно один раз
  попросить «запоминай мои предпочтения по 1С-проектам».

## Best practices (из официальных instructions Honcho)

1. Один стабильный `peer_id` на человека во всех каналах — иначе память
   разделится между дублями.
2. Группируйте сообщения по осмысленным `session_id` (одна сессия на
   тред/проект) и переиспользуйте их.
3. Reasoning асинхронный — не ждите и не поллите; выводы появятся в фоне.
4. Сначала дешёвые чтения (context / representation / search); `chat` —
   только когда нужен reasoned-ответ (секунд 5+).
5. `observe_me: false` — только для детерминированных ботов.
6. `reasoning_level` — minimal/low/medium/high/max (по умолчанию low).
7. Выводы — дерево: исправление факта — вниз по `source_ids`, удаление —
   вверх через `get_derived_conclusions`.

---

## Self-hosting

```bash
# 1. Стек Honcho (api + deriver + Postgres + Redis)
git clone https://github.com/plastic-labs/honcho
cd honcho && docker compose up        # или ./honcho start

# 2. MCP-сервер Honcho (варианты из каталога mcp/ репозитория)
cd mcp && bun install
bun --cwd mcp src/stdio.ts            # stdio-вариант
bun run http                          # HTTP → http://127.0.0.1:3000
# либо Docker: образ honcho-mcp рядом с api:8000

# 3. llm-agent
HONCHO_MCP_URL=http://127.0.0.1:3000/mcp
HONCHO_API_KEY=<ключ вашего инстанса>
```

## Безопасность

- `HONCHO_API_KEY` (`hch-…`) — секрет: только в `.env`/окружении, не в
  репозитории и не в логах (прокси логирует только факт наличия ключа).
- Все mutating-инструменты требуют подтверждения оператора (approval
  gate, `danger: external`); персональные данные записывайте в память
  только по явной просьбе пользователя; секреты (пароли/токены) в память
  не выносить.
- Облако Honcho — сторонний сервис: не отправляйте туда чувствительные
  данные без осознанного решения; для чувствительных контуров —
  self-hosted инстанс.

## Troubleshooting

| Симптом | Причина / решение |
|---|---|
| `HTTP 401 (403)` | Ключ не принят: получите `hch-…` на app.honcho.dev и задайте `HONCHO_API_KEY`; Bearer должен ходить на **каждый** запрос, включая initialize (для локального self-hosted MCP — тоже) |
| `HONCHO_API_KEY не задан` | Прокси не сможет вызывать upstream; каталог `honcho_tools_list` доступен офлайн |
| `No personalization insights found` | **Норма для новых пиров**: выводы ещё не выведены — запишите наблюдения через `add_messages_to_session`, дайте фоновому reasoning время, повторите `list_conclusions`/`get_representation` позже |
| `chat` отвечает дольше 30 с | Это ожидаемо: живой reasoning; прокси ждёт до 120 с. Проверьте `reasoning_level` — «max» существенно дольше |
| `Honcho MCP-эндпоинт недоступен` | Проверьте `HONCHO_MCP_URL` (DNS/прокси/фаервол); для self-hosted — поднят ли MCP (`bun run http` / контейнер) и сам стек (`docker compose ps`) |
| «сеанс теряется при каждом вызове» | Upstream отдаёт 404 на каждый запрос с сеансом — проверьте, что URL ведёт именно на MCP-эндпоинт (`/mcp`), а не на обычный API |
| В песочнице/офлайн `tools/list` отдаёт 21 инструмент | Это статический фолбэк-каталог — штатное поведение без сети; с доступным upstream каталог станет динамическим |

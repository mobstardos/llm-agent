# onec_qa — MCP QA: ИИ-тестирование управляемых форм 1С

Интеграция [MCP QA](https://docs.onerpa.ru/mcp-servery-1c/servery/qa)
(автор comol — тот же, что у [Конструктора MCP серверов](https://vibecoding1c.ru/#designer))
в llm-agent по образцу `onec_designer_tools`. ИИ управляет **тестовой базой
1С** через логическую модель форм: читает окна и поля, находит элементы,
вводит значения, нажимает кнопки, проверяет результат.

## Назначение

MCP QA — Docker-образ `comol/qa_mcp:latest` (версия 0.7.14, только
Linux/amd64), который сам работает **менеджером тестирования**: подключается
к уже запущенному тест-клиенту 1С и даёт агенту ~30 инструментов `qa_*`/`ui_*`.
Платформы 1С и лицензий в образе нет — тест-клиент запускается на машине
пользователя, контейнер общается с ним по сети.

MCP-сервер llm-agent (`src/mcp_servers/onec_qa/server.py`) — **stdio-прокси**:
он не говорит с 1С напрямую, а переводит MCP-вызовы агента в Streamable HTTP
(JSON-RPC 2.0 с состоянием сеанса) к контейнеру.

## Архитектура

```
┌──────────────┐   stdio MCP    ┌────────────────────┐  Streamable HTTP  ┌─────────────────────┐
│ Агент onec_qa│ ─────────────► │ MCP-прокси onec_qa │ ────────────────► │ Контейнер qa_mcp    │
│ (agents/     │  (jsonrpc)     │ (src/mcp_servers/  │  POST /mcp        │ comol/qa_mcp:latest │
│  onec_qa/)   │                │  onec_qa/server.py)│  + Mcp-Session-Id │ :8020  /healthz     │
└──────────────┘                └────────────────────┘                   └─────────┬───────────┘
                                                                                   │ TCP 1538
                                                                                   ▼
                                                                        ┌─────────────────────┐
                                                                        │ Тест-клиент 1С      │
                                                                        │ 1cv8c ENTERPRISE    │
                                                                        │ /F"<база>" /TestClient
                                                                        │ -TPort1538 … (Windows)
                                                                        └─────────────────────┘
```

- Транспорт до контейнера: `POST ONEC_QA_URL` (по умолчанию
  `http://127.0.0.1:8020/mcp`), `Accept: application/json, text/event-stream`.
  Ответ может прийти как JSON, так и **SSE-потоком** (`data:`-строки) — прокси
  парсит оба варианта.
- **Состояние сеанса**: `initialize` → заголовок `Mcp-Session-Id` → передаётся
  в каждом последующем запросе. Имя MCP-подключения контейнера: `1c-qa`.
  Инициализация ленивая (при первом вызове инструмента), session id кэшируется
  в прокси; при 404/истечении сеанса — re-initialize и **один** ретрай.
- Опциональный HTTP-токен: `ONEC_QA_HTTP_TOKEN` → `Authorization: Bearer …`
  (это **НЕ лицензия**; лицензия — `LICENSE_KEY_QA` в `config.env` контейнера).
- Таймауты: 5 с на подключение, 120 с на операцию (как
  `MCP_QA_COMMAND_TIMEOUT` контейнера по умолчанию), `ui_wait` — до 900 с.
- Контейнер держит **ОДИН сеанс**: новый `qa_start` сбрасывает предыдущий.

## Переменные окружения

| Переменная | Где | Назначение |
|---|---|---|
| `ONEC_QA_URL` | llm-agent (`config/settings.yaml`, `.env`) | MCP-эндпоинт контейнера, по умолчанию `http://127.0.0.1:8020/mcp` |
| `ONEC_QA_HTTP_TOKEN` | llm-agent | Bearer-токен, если в контейнере задан `MCP_QA_HTTP_TOKEN` (не лицензия) |
| `LICENSE_KEY_QA` | config.env контейнера | лицензия MCP QA (внутри — `LICENSE_KEY` или `LICENSE_KEY_FILE`) |
| `MCP_QA_EXECUTOR` | контейнер | `native` (по умолчанию в образе) — контейнер сам работает менеджером тестирования |
| `MCP_QA_TESTCLIENT` | контейнер | адрес тест-клиента, по умолчанию `host.docker.internal:1538` |
| `MCP_QA_TESTCLIENT_ID` | контейнер | идентификатор для веб-клиента (`?TestClient`) |
| `MCP_QA_TESTCLIENT_USER/PASSWORD/DOMAIN` | контейнер | необязательны (NTLM принимает любую учётку на 8.3.27) |
| `MCP_QA_HTTP_PORT` | контейнер | порт MCP-сервера (по умолчанию 8020) |
| `MCP_QA_COMMAND_TIMEOUT` | контейнер | таймаут команды (120 с по умолчанию) |
| `MCP_QA_CLIENT_BUS_URL` | контейнер | шина клиента (опционально) |

## Инструменты прокси (31)

| Группа | Инструменты | danger |
|---|---|---|
| Обнаружение | `qa_tools_list` — каталог с пометками (работает без контейнера) | read |
| Жизненный цикл `qa_*` | `qa_status` (executor, link, platform_version, client_hook.available, busy), `qa_start` (connection/testclient/port; user/password/client_kind игнорируются), `qa_stop`, `qa_reconnect(force)`, `qa_command_status` (channel="client", wait_seconds), `qa_doctor`, `qa_profiles` (ключ testclient), `qa_data_candidates`¹ | только `qa_start`/`qa_stop`/`qa_reconnect` — external |
| Окна и формы | `ui_active_window`, `ui_window_tree` (detail lite\|full), `ui_window_changes`, `ui_inspect`, `ui_open` (kind/metadata_name/form_name; понимает русские имена видов: Справочник, Документы…), `ui_close_form` (on_prompt), `ui_form` (command_bar, menu_choice), `ui_form_schema`¹ | `ui_open`, `ui_close_form` — external |
| Элементы и ввод | `ui_find`, `ui_select`, `ui_click`, `ui_input`, `ui_set`, `ui_get_text`, `ui_field` (dropdown_select), `ui_dialog` (action click), `ui_wait`, `ui_messages`, `ui_errors`, `ui_assert` (kind="element", checked) | `ui_select`, `ui_click`, `ui_input`, `ui_set`, `ui_field`, `ui_dialog` — external |
| Таблицы | `ui_table` (select/edit/delete/copy/input_cell/end_edit), `ui_list` | `ui_table` — external |

¹ `qa_data_candidates` и `ui_form_schema` (а также `ui_open` неосновных форм
по имени) требуют **расширение MCPQAClient** в тестовой базе; без него
`client_hook.available: false`, остальные инструменты работают.

`danger: external` = проходит approval gate llm-agent (модальное окно
подтверждения) и перечислен в `dangerous_tools` агента `onec_qa`.

## Установка

1. Запустить контейнер (в `config.env` — `LICENSE_KEY_QA=…`,
   `MCP_QA_TESTCLIENT=host.docker.internal:1538`):

   ```
   docker run -d --name qa-mcp -p 8020:8020 --env-file config.env comol/qa_mcp:latest
   ```

2. Проверить живость: `curl http://127.0.0.1:8020/healthz` — это **только**
   HTTP-живость контейнера, НЕ доступность 1С.
3. Запустить тест-клиент на машине с базой (платформа НЕ в контейнере):

   ```
   1cv8c ENTERPRISE /F"<имя тестовой базы>" /TestClient -TPort1538 /DisableStartupDialogs
   ```

   или веб-клиент `…?TestClient` (тогда задать `MCP_QA_TESTCLIENT_ID`).
4. Прописать окружение llm-agent: `ONEC_QA_URL`, при необходимости
   `ONEC_QA_HTTP_TOKEN` (значения вида `${VAR}` в `config/settings.yaml`,
   фактические — в `.env`). Прокси-сервер и агент подхватятся автоматически
   при старте (сканирование `mcp_servers/` и `agents/`).

## Первый сеанс

```
qa_status                  # executor, link, client_hook.available, busy
qa_start(connection=…)     # подключиться к тест-клиенту (qa_profiles — список)
ui_active_window           # что открыто
ui_window_tree(detail="lite")   # дерево элементов формы
… действия …               # ui_open → ui_input/ui_set/ui_field → ui_form/ui_click
qa_stop                    # отключиться, не закрывая тест-клиент
```

Действия, меняющие значение, возвращают `value_before`/`value_after`/
`verified`; `applied: false` = текст ещё в редакторе поля (нужно
подтверждение ввода).

## Расширение MCPQAClient (кратко)

Расширение `.cfe` ставится в **тестовую базу** и нужно только для трёх
вещей: `ui_form_schema`, `qa_data_candidates` и `ui_open` неосновных форм
по имени. Без него `qa_status` покажет `client_hook.available: false`, а все
остальные инструменты продолжат работать — начинать можно и без расширения.

## Безопасность

- **Только тестовая база или копия.** Инструменты изменяют данные:
  ввод значений, нажатия кнопок, Проведение документов, удаление строк
  таблиц. Всё `danger: external` проходит approval gate.
- **Один сеанс на контейнер**: параллельные агенты будут сбрасывать сеанс
  друг друга — работайте поочерёдно.
- **Барьер «исход неизвестен»**: при обрыве связи судьба последней команды
  не известна. Порядок всегда такой: `qa_command_status` →
  `qa_reconnect(force=True)` → прочитать окно (`ui_window_tree`/
  `ui_active_window`) → только потом решать, что делать. **Никогда не
  повторять действие вслепую.**
- `qa_status` не встаёт в очередь: `busy: true` — другой инструмент ещё
  работает, подождите, не шлите параллельные действия.

## Ограничения контейнера (executor_capability)

В Docker-контейнере недоступны (прокси их **не** проксирует):
`ui_screenshot`, `qa_start(hidden_desktop)`, `ui_eval`, `qa_run_script`,
`qa_setup`, `qa_install_client`. Скриншотов нет — visual-проверки
честно помечаются невыполненными; состояние UI проверяется через
`ui_get_text`, `ui_window_changes`, `ui_messages`, `ui_errors`, `ui_assert`.

Лимиты объёма обхода (за ними следит и промпт агента): `max_nodes ≤ 5000`,
`max_depth ≤ 20`, `max_rows`/`max_table_rows ≤ 1000`, `max_columns ≤ 200`,
`max_rows × max_columns ≤ 20000`, переход к строке `≤ 10000`.

## Troubleshooting

| Симптом | Причина / что делать |
|---|---|
| «контейнер MCP QA недоступен» | Контейнер не запущен — команда `docker run` выше; проверьте `curl http://127.0.0.1:8020/healthz`. Порт занят/сменён — `MCP_QA_HTTP_PORT` и `ONEC_QA_URL` должны совпадать. |
| HTTP 401/403 | В контейнере задан `MCP_QA_HTTP_TOKEN` — укажите то же значение в `ONEC_QA_HTTP_TOKEN`. |
| «Отсутствует подходящий клиент тестирования» | Тест-клиент не вошёл в базу или не совпал TestClientID: запустите `1cv8c ENTERPRISE /F"<база>" /TestClient -TPort1538 /DisableStartupDialogs`, проверьте `MCP_QA_TESTCLIENT` (host.docker.internal:1538) / `MCP_QA_TESTCLIENT_ID`, вызовите `qa_doctor`. |
| `qa_status` → `busy: true` | Предыдущий инструмент ещё работает (>5 с). Подождите и повторите статус — очередь нет. |
| `client_hook.available: false` | Нет расширения MCPQAClient в тестовой базе — нужны только `ui_form_schema`, `qa_data_candidates`, `ui_open` неосновных форм по имени. |
| «сеанс MCP QA теряется при каждом вызове» | Контейнер перезапускался / сеанс истёк. Прокси сам делает re-initialize + 1 ретрай; если не помогло — проверьте, не дёргает ли кто-то ещё контейнер (один сеанс!). |
| HTTP 404 на инструментах | Сеанс устарел — прокси переинициализируется автоматически; при повторении перезапустите контейнер. |

## Файлы интеграции

| Файл | Назначение |
|---|---|
| `src/mcp_servers/onec_qa/__init__.py` | пакет MCP-прокси |
| `src/mcp_servers/onec_qa/server.py` | stdio MCP-прокси (31 инструмент, Streamable HTTP + SSE + сеанс) |
| `mcp_servers/onec_qa/server.yaml` | декларация MCP (danger-метки, env) |
| `agents/onec_qa/agent.yaml` | декларация агента (routing, dangerous_tools) |
| `agents/onec_qa/prompt.md` | правила безопасности и порядок работы |
| `agents/onec_qa/user.md` | шаблон задачи |
| `config/settings.yaml` | секция `mcp_servers.onec_qa` (`ONEC_QA_URL`, `ONEC_QA_HTTP_TOKEN`) |

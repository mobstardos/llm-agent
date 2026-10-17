# MCP FAQ — справочник инструментов

> Полный список всех MCP-серверов и их инструментов с описаниями.
> Используй как справочник — что умеет каждый MCP, какие env vars нужны,
> какие dangerous_tools требуют подтверждения.

## Содержание

1. [qa-mcp-public (63 tools) — 1C TestClient QA](#qa-mcp-public)
2. [Выбор между дубликатами MCP](#mcp-choice-guide)

---

## qa-mcp-public

**GitHub:** https://github.com/vlikhobabin/qa-mcp-public
**License:** Apache-2.0
**Install:**
```bash
# Option 1: GitHub release wheel
# Download qa_mcp-0.1.0-py3-none-any.whl from releases page
python3 -m venv .venv
.venv/bin/python -m pip install ./qa_mcp-0.1.0-py3-none-any.whl
.venv/bin/qa-native-mcp  # starts stdio MCP server

# Option 2: uv (no .whl needed)
uv run qa-native-mcp
```

**Env vars:**

| Variable | Level | Default | Description |
|---|---|---|---|
| `QA_MCP_HOME` | soft | (fallback) | Runtime output root |
| `QA_MCP_TRANSPORT` | soft | stdio | Transport: stdio или http |
| `QA_MCP_HTTP_HOST` | soft | 127.0.0.1 | HTTP bind host (loopback по умолчанию) |
| `QA_MCP_HTTP_PORT` | soft | 8000 | HTTP port |
| `QA_MCP_BEARER_TOKEN` | soft | (none) | Bearer token для HTTP auth |
| `QA_MCP_TESTCLIENT_OWNERSHIP_ROOT` | soft | (fallback) | Ownership markers root |

**Architecture:**
```
AI agent ── MCP stdio/HTTP ── qa-mcp Python server
                                │
                                ├─ native TestClient protocol
                                ├─ scenario/reporting engine
                                └─ optional authenticated Windows bridge
                                      └─ owned TestClient + desktop primitives
```


### Lifecycle / session (5)

Управление TestClient сессией: doctor, launch, attach, status, stop


| Tool | Danger | Description |
|---|---|---|
| `launch_test_client` | destructive | Boot a native /TESTCLIENT (headless under Xvfb или through Windows host-agent). Records process ownership, requires non-consuming TPort check + 20-sec process-stability window |
| `attach_test_client` | write | Attach to already-listening TestClient TPort без process ownership. Records active attached endpoint + client_target |
| `test_client_status` | read | Is a client up; connection/listen state |
| `stop_test_client` | destructive | Tear the client down (restart Apache в finally) |

### Scenario / BDD authoring & execution (5)

Gherkin/BDD: transpile, search_for_steps, run_scenario, run_step, run_write_scenario_tool


| Tool | Danger | Description |
|---|---|---|
| `transpile` | read | Gherkin .feature → native scenario JSON; reports unmapped steps (never drops silently) |
| `search_for_steps` | read | List supported Gherkin step library (canonical phrasing + kind + example) |
| `run_scenario` | write | Run .feature / scenario JSON natively (reads, asserts, data-layer, open actions, nested scenarios) |
| `run_step` | write | Execute single step (Vanessa execute_step_from_text equivalent) |
| `run_write_scenario_tool` | write | Run write-oriented scenario (input + commit) against captured action template |

### Reporting / state / results (4)

Отчёты: get_test_results, write_test_report, get_state, infobase_info


| Tool | Danger | Description |
|---|---|---|
| `get_test_results` | read | Aggregate scenarios run this session (pass/fail/step counts) |
| `write_test_report` | write | Emit JUnit XML + Allure results (per-step timing + screenshots) для CI |
| `get_state` | read | Capture-free get_state equivalent — connection + run-session + infobase identity |
| `infobase_info` | read | Infobase/connection metadata из .ai1c profile (password-redacted) + live listening |

### Read / introspection (12)

Чтение форм/таблиц/окон/скриншотов


| Tool | Danger | Description |
|---|---|---|
| `read_form_descriptor` | read | Full element name→value descriptor + Gherkin state; open_link= opens ANY form; enumerate_live= enumerates fields live (ASCII и Cyrillic) |
| `read_active_window` | read | Bound reads expose finite window_state (observed/missing/ambiguous) + bounded marker_count |
| `read_record` | read | Read navigated record's fields |
| `read_table_cell` | read | Read form-table cell |
| `read_list_column` | read | Read one dynamic-list cell |
| `read_list_row` | read | Read row's columns (или row BY VALUE via where={col: value}) |
| `read_list_grid` | read | Read whole dynamic-list grid (next-row command; flat=True для hierarchical catalog в flat view) |
| `read_spreadsheet_cell` | read | Read spreadsheet-document cell |
| `read_user_messages` | read | Read client's user-message area (Сообщить) |
| `get_window_list` | read | OS top-level windows на client display (xdotool: id/title/geometry) |
| `get_window_list_testclient` | read | 1C-internal window/tab list (caption + frame kind) |
| `capture_screenshot` | read | OS screenshot of client display; trusted bound producers allocate and verify fresh destination + actual digest |

### Assertions / waits (5)

Проверки UI и DB (включая beyond Vanessa)


| Tool | Danger | Description |
|---|---|---|
| `assert_form_value` | read | Assert form field equals/contains value |
| `wait_for_form_value` | read | Poll form field until it reaches value (или timeout) |
| `assert_data` | read | BEYOND Vanessa — assert UI action persisted to DB via read-only OData client (entity_set/field/expected, match=equals|contains|regex) |
| `assert_data_count` | read | BEYOND Vanessa — assert NUMBER of records matching filter (op=eq|ne|gt|lt|ge|le); e.g. 'posting created exactly N register rows' |
| `role_data_matrix` | read | BEYOND Vanessa — run same data-layer read under several credentials/roles → per-role access (read/denied) + count |

### Host-side COM read / diagnostics (3)

COM-запросы к Windows host-agent


| Tool | Danger | Description |
|---|---|---|
| `query_com` | read | Execute bounded read-only 1C query through configured Windows host-agent COM worker |
| `assert_com_count` | read | Compare first numeric result of bounded COM read query with expected count |
| `com_connector_doctor` | read | Diagnose COMConnector availability, bitness, registration + actionable host repair guidance |

### Write / input (6)

Ввод данных в формы


| Tool | Danger | Description |
|---|---|---|
| `write_form_value` | write | String/number/date input + commit via protocol template (captured action) |
| `write_form_date` | write | Validate and write form date by field name/label, with optional open-link routing |
| `write_form_value_xtest` | write | Input + commit via protocol+XTEST hybrid (real OS keystrokes; needs display) |
| `write_form_values` | write | Write several fields в one session |
| `write_form_fields_by_label` | write | BEYOND Vanessa — config-agnostic arbitrary-field write by visible label «X:» (foreground → locate → xtest type) |
| `send_keys` | write | Raw OS keyboard keys (Enter/Esc/Tab/arrows/shortcuts) via XTEST/local display или Windows host-agent |

### Form field actions (12)

Действия с элементами форм: чекбоксы, таблицы, выбор


| Tool | Danger | Description |
|---|---|---|
| `switch_page` | write | Switch form tab/page |
| `toggle_checkbox` | write | Toggle checkbox |
| `set_choice` | write | Set choice/enum field |
| `set_reference_field` | write | Set reference (lookup) field |
| `set_table_cell` | write | Write form-table cell |
| `set_table_date_cell` | write | Set date в table date cell (config-agnostic; auto-localized; calendar driven by mouse) |
| `select_table_row` | write | Select / go to table row |
| `add_table_row` | write | Add row to form table |
| `delete_table_row` | destructive | Delete row from form table (destructive) |
| `move_table_row` | write | Move table row |
| `copy_table_row` | write | Copy table row |
| `select_all_table_rows` | write | Select all table rows |

### Navigation / windows (5)

Навигация: open_list, open_card, close_window, activate_window, click_command


| Tool | Danger | Description |
|---|---|---|
| `open_list` | write | Open list form by nav-link; omitted/blank capture uses released versioned navigation templates |
| `open_card` | write | Open object card (e.g. by button) |
| `close_window` | write | Close current/named window |
| `activate_window` | write | Bring window to foreground |
| `click_command` | write | Click form command/button |

### Lists / search / reports / dialogs (7)

Списки, поиск, отчёты, диалоги


| Tool | Danger | Description |
|---|---|---|
| `search_list` | read | Search/filter dynamic list |
| `set_list_view` | write | Switch dynamic list's view mode |
| `advanced_search` | read | Dynamic-list advanced filter |
| `choose_from_list` | write | Pick from selection list |
| `choose_from_menu` | write | Pick from menu |
| `answer_dialog` | write | Answer modal dialog (Да/Нет/ОК) |
| `run_report` | read | Run report |


### Non-stable research inventory (5)

Эти инструменты НЕ входят в stable standalone matrix. Research tools требуют
`QA_MCP_TOOL_PROFILE=research`.

| Tool | Status | Description |
|---|---|---|
| `open_external_processor` | **dormant** | Vanessa-component-free external .epf open (omitted from stable) |
| `echo_jsonrpc_arguments` | research-only | Inspect parsed JSON-RPC arguments и encoding damage |
| `generate_smoke_suite` | research-only | Metadata-driven smoke generation |
| `autofill_required_fields` | research-only | Metadata-driven required-field values |
| `measure_scenario` | research-only | Debug-protocol coverage + perf measurement |

### Beyond Vanessa (что НЕ умеет Vanessa Automation)

- `assert_data` (UI→DB cross-verification) — action persisted to DB via read-only OData
- `assert_data_count` — assert NUMBER of records matching filter
- `role_data_matrix` — same data-layer read under several credentials/roles
- `write_form_fields_by_label` — config-agnostic field write by visible label
- `set_table_date_cell` — config-agnostic date write (calendar driven by mouse)
- (non-stable): `generate_smoke_suite`, `autofill_required_fields`, `measure_scenario`

### CI quality gates

- **Per-step perf budget** — Gherkin step «Каждый шаг выполняется быстрее N мс» fails scenario
- **Coverage gate** — `python -m qa_mcp.debug.gates --report <measure.json> --min-lines N`

### Live regression harness

```bash
python -m qa_mcp.regression
```
Boots TestClient, exercises LIVE-verified capabilities, exits non-zero on regression.

### Safety model

- Use disposable test data for UI/mutation operations + retain recovery proof
- Runtime target resolution immutable для session; target mismatches fail before lifecycle/UI work
- HTTP + Windows bridge routes authenticated; loopback — safe default
- Cleanup stops only exact processes + artifacts owned by the operation
- Secrets/customer data/private endpoints/full infobases/proprietary 1C binaries/local captures
  do NOT belong in Git or issue reports

---

## MCP Choice Guide — выбор между дубликатами

### supabase vs postgrest

| Критерий | `supabase` | `postgrest` |
|---|---|---|
| **Что это** | Полный Supabase MCP (SQL, Auth, Storage, Realtime + PostgREST) | Только PostgREST API |
| **npm package** | `@supabase/mcp-server-supabase` (VERIFIED, OFFICIAL) | нет отдельного MCP (часть supabase) |
| **GitHub** | https://github.com/supabase/mcp | (part of supabase/mcp) |
| **Когда использовать** | ✅ ВСЕГДА когда нужен Supabase/PostgREST | ❌ никогда (используйте supabase) |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ SUPABASE** — содержит PostgREST + больше | НЕ ИСПОЛЬЗУЙТЕ — postgrest это subset |

**Вывод:** supabase это надмножество postgrest. Удалим postgrest полностью.

### github vs github_projects vs github_official

| Критерий | `github` (existing) | `github_projects` | `github_official` (Sprint 6) |
|---|---|---|---|
| **Что это** | GitHub MCP (existing в проекте) | GitHub Projects v2 kanban | Official @modelcontextprotocol/server-github |
| **npm package** | (existing) | нет (deprecated) | `@modelcontextprotocol/server-github` (VERIFIED) |
| **Когда использовать** | ✅ ВСЕГДА — это оригинальный | ❌ НИКОГДА — дубликат | ⚠ альтернатива оригинальному |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ GITHUB** — он уже работает | НЕ ИСПОЛЬЗУЙТЕ — нет смысла | Опционально — если хотите official MCP |

**Вывод:** github — основной. github_official — альтернатива если нужен official MCP. github_projects — удалить.

### playwright_official vs playwright_cli vs puppeteer

| Критерий | `playwright_official` | `playwright_cli` | `puppeteer` |
|---|---|---|---|
| **Что это** | Microsoft official Playwright MCP | Тот же, CLI-режим | Puppeteer (Chrome-only) |
| **npm package** | `@playwright/mcp` (VERIFIED) | то же | `@modelcontextprotocol/server-puppeteer` (VERIFIED) |
| **Когда использовать** | ✅ ВСЕГДА для E2E-тестов | то же, не отдельный пакет | Альтернатива если предпочитаете Puppeteer |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ PLAYWRIGHT_OFFICIAL** | НЕ ИСПОЛЬЗУЙТЕ — дубликат | Опционально — для Chrome-only сценариев |

**Вывод:** playwright_official основной. puppeteer — альтернатива для Chrome-only. playwright_cli — удалить.

### git_official vs git (existing)

| Критерий | `git` (existing) | `git_official` |
|---|---|---|
| **npm/PyPI** | (existing в проекте) | `mcp-server-git` (PyPI VERIFIED) |
| **Что это** | Git MCP (существующий) | Official MCP из modelcontextprotocol/servers |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ GITHUB** — он уже работает | Опционально |

### sqlite_official vs db_extended (existing)

| Критерий | `db_extended` (existing) | `sqlite_official` |
|---|---|---|
| **PyPI** | (existing в проекте) | `mcp-server-sqlite` (VERIFIED) |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ DB_EXTENDED** — он уже работает | Опционально — official из modelcontextprotocol/servers |

### memory_official vs memory (existing)

| Критерий | `memory` (existing src/memory/facade.py) | `memory_official` |
|---|---|---|
| **npm** | (existing, native Python) | `@modelcontextprotocol/server-memory` (VERIFIED) |
| **Рекомендация** | **ИСПОЛЬЗУЙТЕ MEMORY** — интегрирован с PostgreSQL/pgvector | Опционально — для standalone knowledge graph |

## Сводная таблица: что использовать ПО УМОЛЧАНИЮ

| Категория | Используй | Альтернатива | Не использовать |
|---|---|---|---|
| PostgREST | `supabase` | — | `postgrest` (subset) |
| GitHub | `github` (existing) | `github_official` | `github_projects` (duplicate) |
| Playwright | `playwright_official` | `puppeteer` | `playwright_cli` (duplicate) |
| Git | `git` (existing) | `git_official` | — |
| SQLite | `db_extended` (existing) | `sqlite_official` | — |
| Memory | `memory` (existing) | `memory_official` | — |
| 1C QA | `qa_mcp` (NEW) | — | — |

## Финальные рекомендации

### Обязательно включить в config/settings.yaml:
- `qa_mcp` — 1C QA automation (63 tools, ИДЕАЛЬНО для вашего 1С-проекта!)
- `elasticsearch`, `redis`, `brave_search`, `exa_search`, `tavily_search`, `headroom` — native Python MCP (deps уже стоят)
- `supabase` — если используете Supabase
- `terraform` — если есть IaC
- `argocd`, `twilio`, `wandb`, `cassandra`, `clinical_trials`, `seatunnel` — восстановлены с реальными GitHub

### Опционально (по необходимости):
- `slack`, `notion`, `jira`, `linear`, `trello`, `gmail`, `discord`, `telegram` — для team collaboration
- `grafana`, `sentry`, `datadog`, `prometheus` — для monitoring
- `cloudflare`, `aws_s3`, `aws_lambda`, `google_drive` — для cloud
- `figma`, `stripe`, `shopify`, `airtable` — для дизайна/e-commerce
- `langsmith` — для LLM observability
- `qdrant`, `weaviate`, `chroma`, `milvus`, `pinecone` — для vector DB (альтернатива pgvector)

### НЕ включать:
- `postgrest` — subset supabase
- `github_projects` — duplicate github
- `playwright_cli` — duplicate playwright_official

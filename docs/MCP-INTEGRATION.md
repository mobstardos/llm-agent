# MCP Integration Guide — llm-agent v2026-10-01

> **Sprint 6: расширение проекта 62 новыми MCP-серверами.**
> 5 нативных Python (deps уже стоят) + 57 внешних (через npx/uvx).

## 📊 Что добавлено (62 MCP-сервера)

| Категория | Кол-во | Примеры |
|---|---|---|
| **Database** | 12 | elasticsearch, redis, mongodb, supabase, qdrant, pinecone, weaviate, chroma, milvus, sqlite_official, postgrest, cassandra |
| **Communication** | 6 | slack, discord, telegram, gmail, twilio, reddit |
| **Cloud** | 5 | cloudflare, aws_s3, aws_lambda, aws_cloudwatch, google_drive |
| **DevOps** | 5 | terraform, helm, argocd, git_official, github_official |
| **Project Management** | 5 | linear, notion, jira, trello, github_projects |
| **Monitoring** | 4 | grafana, sentry, datadog, prometheus |
| **AI Observability** | 4 | langsmith, wandb, sequential_thinking, memory_official |
| **Office** | 4 | google_sheets, airtable, google_calendar, time |
| **Search** | 4 | brave_search, exa_search, tavily_search, fetch |
| **Browser** | 3 | playwright_official, puppeteer, brightdata |
| **Specialized** | 3 | clinical_trials, bioinformatics, seatunnel |
| **Design** | 2 | figma, everart |
| **Security** | 2 | onepassword, vault |
| **E-commerce** | 2 | stripe, shopify |
| **Code Quality** | 1 | sonarqube |
| **ИТОГО** | **62** | — |

## 🏗️ Архитектура интеграции

### Декларативный подход (без кода!)

Каждый MCP-сервер описан 4 файлами:

```
mcp_servers/<id>/server.yaml        ← декларация (command, args, env_vars, tools)
agents/<id>/agent.yaml              ← декларация агента (routing_hints, mode, dangerous_tools)
agents/<id>/prompt.md               ← системный промпт агента
agents/<id>/user.md                ← шаблон пользовательского ввода
```

Для **внешних** MCP (npx/uvx) — этого ДОСТАТОЧНО. Никакого кода писать не нужно.

Для **нативных** Python (deps уже в requirements.txt) — добавлен 5-й файл:
```
src/mcp_servers/<id>/server.py     ← реализация через @app.list_tools() / @app.call_tool()
```

### 5 нативных Python MCP-серверов

Эти реализованы в самом проекте (deps уже стоят в `requirements.txt`):

| MCP | LOC | Deps | Файл |
|---|---|---|---|
| `elasticsearch` | 215 | elasticsearch>=8.15.0 | `src/mcp_servers/elasticsearch/server.py` |
| `redis` | 165 | redis>=5.0.0 | `src/mcp_servers/redis/server.py` |
| `brave_search` | 140 | httpx>=0.27.2 | `src/mcp_servers/brave_search/server.py` |
| `exa_search` | 130 | httpx>=0.27.2 | `src/mcp_servers/exa_search/server.py` |
| `tavily_search` | 140 | httpx>=0.27.2 | `src/mcp_servers/tavily_search/server.py` |

Все 5 используют **mcp 1.x контракт** (см. `tests/test_mcp_1x_contract.py`):
```python
from mcp import Server
app = Server("my-mcp")

@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [Tool(name="...", description="...", inputSchema={...})]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    return [TextContent(type="text", text="...")]
```

## 🚀 Установка

### Способ 1: всё одной командой (рекомендуется)

```bash
cd /path/to/llm-agent-v2026-10-01

# Проверить окружение (node/uv/python):
bash install_mcp_servers.sh --check

# Установить все 57 внешних MCP (npm cache + uv cache):
bash install_mcp_servers.sh

# Или dry-run (показать что будет):
bash install_mcp_servers.sh --dry-run
```

### Способ 2: только нужные

Если не нужны все 57 — установите выборочно:

```bash
# Только npm-пакеты (Slack, Notion, GitHub, ...):
npx -y @modelcontextprotocol/server-slack
npx -y @modelcontextprotocol/server-notion
npx -y @modelcontextprotocol/server-github

# Только PyPI-пакеты через uvx (Qdrant, Grafana, ...):
uvx qdrant-mcp-server
uvx mcp-grafana
uvx mcp-atlassian
```

### Способ 3: нативные Python (deps уже стоят)

Нативные MCP-серверы (elasticsearch, redis, brave_search, exa_search, tavily_search)
**уже реализованы** в `src/mcp_servers/`. Ничего ставить не нужно — deps в `requirements.txt`.

## ⚙️ Конфигурация

### 1. Заполнить .env

```bash
cp .env.example.mcp-expansion .env.mcp
nano .env.mcp  # раскомментировать нужные MCP, вписать ключи
```

Пример (включаем Slack и Elasticsearch):
```env
# ─── elasticsearch (native) ──────────────────────────────────────
ELASTICSEARCH_URL=http://localhost:9200
#ELASTICSEARCH_API_KEY=...

# ─── slack (external) ─────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-...
```

### 2. Включить MCP в config/settings.yaml

См. `config/settings.yaml.sprint6-addition` для блока всех 62 MCP.
Скопируйте нужные строки в `config/settings.yaml`:

```yaml
mcp_servers:
  elasticsearch:
    command: python
    args: ["-m", "src.mcp_servers.elasticsearch.server"]
    env:
      ELASTICSEARCH_URL: "${ELASTICSEARCH_URL}"
  slack:
    command: npx
    args: ["-y", "@modelcontextprotocol/server-slack"]
    env:
      SLACK_BOT_TOKEN: "${SLACK_BOT_TOKEN}"
  # ... и т.д.
```

### 3. Перезапустить сервер

```bash
python run.py
```

### 4. Проверить активные MCP

```bash
curl http://127.0.0.1:8000/api/registry/mcp | jq '.mcp_servers[] | select(.status=="active") | .id'
```

## 🧪 Тестирование

### Нативные MCP-серверы (5 шт.)

```bash
# Контракт-тест mcp 1.x (для всех нативных):
pytest tests/test_mcp_1x_contract.py -v

# По одному:
pytest tests/test_mcp_1x_contract.py -v -k elasticsearch
pytest tests/test_mcp_1x_contract.py -v -k redis
pytest tests/test_mcp_1x_contract.py -v -k brave_search
pytest tests/test_mcp_1x_contract.py -v -k exa_search
pytest tests/test_mcp_1x_contract.py -v -k tavily_search
```

### Внешние MCP (57 шт.)

Контракт-тест не применим (мы не контролируем их код). Тестируйте через
end-to-end вызовы:

```bash
# Пример: проверка Slack MCP после настройки
curl -X POST http://127.0.0.1:8000/api/mcp/call_tool \
  -H "Content-Type: application/json" \
  -d '{"server": "slack", "tool": "slack_list_channels"}'
```

## 🛠️ Создание нового MCP-сервера

### Через scaffolding utility (рекомендуется)

```bash
python scripts/add_mcp_server.py \
    --id my_new_mcp \
    --title "My New MCP" \
    --description "Does X, Y, Z" \
    --category database \
    --command npx \
    --args '["-y", "my-package"]' \
    --env-vars 'MY_API_KEY:hard' \
    --mode read \
    --tools 'list_items:read, create_item:write, delete_item:destructive' \
    --keywords 'keyword1, keyword2' \
    --priority 10 \
    --icon 🎯 \
    --color '#3b82f6' \
    --github https://github.com/user/repo
```

Эта команда создаёт **5 файлов**:
- `mcp_servers/my_new_mcp/server.yaml`
- `agents/my_new_mcp/agent.yaml`
- `agents/my_new_mcp/prompt.md`
- `agents/my_new_mcp/user.md`
- (если `--native`) `src/mcp_servers/my_new_mcp/{__init__.py, server.py}`

### Вручную (по шаблону)

1. Скопировать `features/_template/` в `features/my_new_feature/`
2. Заполнить `feature.yaml`, `api.py`, `ui.js`
3. См. `features/_template/README.md` для деталей

## 📋 Список всех 62 MCP с env-переменными

См. `.env.example.mcp-expansion` — содержит все переменные окружения
для всех 62 MCP, с комментариями `[HARD — required]` / `[SOFT — optional]`.

## 🔍 Категории и конкретные MCP

### Databases (12)

| ID | Tools | Env | Native? |
|---|---|---|---|
| elasticsearch | 6 | ELASTICSEARCH_URL, ELASTICSEARCH_API_KEY | ✅ Python |
| redis | 7 | REDIS_URL | ✅ Python |
| mongodb | 5 | MONGO_URI | ❌ npm |
| supabase | 5 | SUPABASE_ACCESS_TOKEN | ❌ npm |
| qdrant | 4 | QDRANT_URL, QDRANT_API_KEY | ❌ uvx |
| pinecone | 3 | PINECONE_API_KEY | ❌ uvx |
| weaviate | 3 | WEAVIATE_URL, WEAVIATE_API_KEY | ❌ uvx |
| chroma | 3 | CHROMA_PATH | ❌ uvx |
| milvus | 3 | MILVUS_HOST, MILVUS_PORT | ❌ uvx |
| sqlite_official | 4 | SQLITE_PATH | ❌ uvx |
| postgrest | 3 | POSTGREST_URL | ❌ uvx |
| cassandra | 3 | CASSANDRA_HOST, CASSANDRA_PORT | ❌ uvx |

### Cloud Services (5)

| ID | Tools | Env |
|---|---|---|
| cloudflare | 7 | CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID |
| aws_s3 | 5 | AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION |
| aws_lambda | 4 | AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY |
| aws_cloudwatch | 4 | AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY |
| google_drive | 4 | GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET |

### Communication (6)

| ID | Tools | Env |
|---|---|---|
| slack | 5 | SLACK_BOT_TOKEN |
| discord | 4 | DISCORD_TOKEN |
| telegram | 4 | TELEGRAM_BOT_TOKEN |
| gmail | 5 | GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET |
| twilio | 3 | TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN |
| reddit | 3 | REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET |

### Project Management (5)

| ID | Tools | Env |
|---|---|---|
| linear | 5 | LINEAR_API_KEY |
| notion | 5 | NOTION_API_KEY |
| jira | 6 | JIRA_URL, JIRA_USERNAME, JIRA_API_TOKEN |
| trello | 4 | TRELLO_API_KEY, TRELLO_TOKEN |
| github_projects | 4 | GITHUB_TOKEN |

### Design (2)

| ID | Tools | Env |
|---|---|---|
| figma | 4 | FIGMA_ACCESS_TOKEN |
| everart | 1 | EVERART_API_KEY |

### Monitoring (4)

| ID | Tools | Env |
|---|---|---|
| grafana | 5 | GRAFANA_URL, GRAFANA_API_KEY |
| sentry | 4 | SENTRY_AUTH_TOKEN, SENTRY_ORG |
| datadog | 4 | DATADOG_API_KEY, DATADOG_APP_KEY |
| prometheus | 4 | PROMETHEUS_URL |

### AI Observability (4)

| ID | Tools | Env |
|---|---|---|
| langsmith | 3 | LANGCHAIN_API_KEY |
| wandb | 3 | WANDB_API_KEY |
| sequential_thinking | 1 | (none) |
| memory_official | 4 | (none) |

### Office (4)

| ID | Tools | Env |
|---|---|---|
| google_sheets | 4 | GSHEETS_CLIENT_ID, GSHEETS_CLIENT_SECRET |
| airtable | 4 | AIRTABLE_API_KEY |
| google_calendar | 4 | GCAL_CLIENT_ID, GCAL_CLIENT_SECRET |
| time | 3 | (none) |

### Search (4)

| ID | Tools | Env | Native? |
|---|---|---|---|
| brave_search | 3 | BRAVE_API_KEY | ✅ Python |
| exa_search | 3 | EXA_API_KEY | ✅ Python |
| tavily_search | 3 | TAVILY_API_KEY | ✅ Python |
| fetch | 1 | (none) | ❌ uvx |

### Browser (3)

| ID | Tools | Env |
|---|---|---|
| playwright_official | 5 | (none) |
| puppeteer | 4 | (none) |
| brightdata | 3 | BRIGHTDATA_API_KEY |

### DevOps (5)

| ID | Tools | Env |
|---|---|---|
| terraform | 4 | TF_TOKEN |
| helm | 5 | KUBECONFIG |
| argocd | 4 | ARGOCD_SERVER, ARGOCD_TOKEN |
| git_official | 5 | (none) |
| github_official | 5 | GITHUB_TOKEN |

### Security (2)

| ID | Tools | Env |
|---|---|---|
| onepassword | 3 | OP_ACCOUNT, OP_TOKEN |
| vault | 3 | VAULT_ADDR, VAULT_TOKEN |

### E-commerce (2)

| ID | Tools | Env |
|---|---|---|
| stripe | 4 | STRIPE_SECRET_KEY |
| shopify | 4 | SHOPIFY_ACCESS_TOKEN, SHOPIFY_SHOP |

### Code Quality (1)

| ID | Tools | Env |
|---|---|---|
| sonarqube | 4 | SONARQUBE_URL, SONARQUBE_TOKEN |

### Specialized (3)

| ID | Tools | Env |
|---|---|---|
| clinical_trials | 3 | (none) |
| bioinformatics | 3 | NCBI_API_KEY |
| seatunnel | 3 | SEATUNNEL_API_URL |

## 🎯 Приоритеты для интеграции

### С чего начать (топ-5)

1. **Elasticsearch MCP** (native Python, deps уже стоят!) — самый простой старт
2. **Redis MCP** (native Python, deps уже стоят!) — у вас уже есть Redis как cluster bus
3. **Brave/Exa/Tavily Search** (native Python, deps уже стоят!) — web-search без браузера
4. **Slack MCP** (npm) — для уведомлений о задачах/ошибках
5. **Grafana/Sentry MCP** (uvx) — для production monitoring

### Не ставить (если не нужны)

- `clinical_trials`, `bioinformatics`, `seatunnel` — специализированные, для медицины/биологии/data engineering
- `everart` — AI image generation (можно через существующий image MCP)
- `memory_official` — у вас уже есть memory facade, избыточно
- `sequential_thinking` — у вас уже есть LoopController с reasoning cycles

## ⚠️ Известные ограничения

1. **mcp 2.x НЕ поддерживается** — все MCP-серверы используют 1.x контракт.
   Тест `tests/test_mcp_1x_contract.py` проверяет это (8 кейсов).

2. **57 внешних MCP требуют Node.js + uv** — без них не запустятся npm/PyPI пакеты.
   Проверьте: `bash install_mcp_servers.sh --check`

3. **OAuth для некоторых MCP** (Google Drive, Gmail, Calendar, Sheets) —
   требуют настройки OAuth credentials в Google Cloud Console. См.
   документацию каждого MCP.

4. **Дублирование с существующими MCP**:
   - `git_official` и `github_official` дублируют ваши `git` и `github` MCP.
     Используйте либо оригинальные, либо официальные — не оба.
   - `playwright_official` и `puppeteer` дублируют ваш `browser` MCP.
   - `sqlite_official` дублирует ваш `db_extended` для SQLite.

5. **Нативные MCP-серверы** используют **lazy imports** — если
   `elasticsearch` пакет не установлен, сервер запустится, но `get_client()`
   выбросит ImportError при первом вызове. Тесты это покрывают.

## 📚 Полезные ресурсы

| Ресурс | URL |
|---|---|
| Главная подборка MCP | https://github.com/punkpeger/awesome-mcp-servers |
| Официальные reference | https://github.com/modelcontextprotocol/servers |
| MCP Registry | https://github.com/modelcontextprotocol/registry |
| MCP Servers Directory | https://mcpservers.org |
| MCP TypeScript SDK | https://www.npmjs.com/package/@modelcontextprotocol/sdk |
| MCP Python SDK | https://pypi.org/project/mcp/ |

## 🆘 Поддержка

- **Issues**: GitHub Issues репозитория
- **Документация по конкретному MCP**: см. поле `# GitHub:` в каждом `server.yaml`
- **Scaffolding новых MCP**: `python scripts/add_mcp_server.py --help`


## ⚠️ DEPRECATED MCP (10 — пакеты не существуют)

Следующие MCP помечены DEPRECATED — их пакеты не существуют на npm/PyPI.
Каталоги в `mcp_servers/<id>/` сохранены с DEPRECATED.md для будущего.
В `config/settings.yaml` и `.env` они НЕ должны включаться.

| MCP | Причина | Альтернатива |
|---|---|---|
| `terraform` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `argocd` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `postgrest` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `cassandra` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `twilio` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `wandb` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `clinical_trials` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `seatunnel` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `supabase` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n| `github_projects` | нет MCP пакета | используйте SDK напрямую или напишите обёртку |\n

Для wandb/cassandra/twilio — можно реализовать нативный Python MCP
(через `scripts/add_mcp_server.py --native`).

## 🆕 Agent Skills (отдельная категория, НЕ MCP)

Agent Skills — это markdown-файлы (SKILL.md / DESIGN.md), которые AI-агенты
читают напрямую. Не используют протокол MCP. Установка через
`npx skills add <github-url>`.

| Skill | GitHub | Что делает |
|---|---|---|
| Taste Skill | https://github.com/Leonxlnx/taste-skill | Anti-slop frontend framework |
| Awesome DESIGN.md | https://github.com/VoltAgent/awesome-design-md | 73 DESIGN.md с реальных сайтов |

Установка: `bash agent_skills/install_all.sh` (см. `agent_skills/README.md`)

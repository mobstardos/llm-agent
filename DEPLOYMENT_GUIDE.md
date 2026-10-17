# Deployment Guide — llm-agent v2026-10-01

> Production-ready deployment guide. Контейнерный способ (рекомендуется)
> + bare-metal вариант (для разработки/теста).

## 🎯 Цели deployment

- **High availability** — multi-instance через nginx sticky sessions
- **Persistence** — volumes для data/logs/shadows (переживают рестарты)
- **Security** — непривилегированный пользователь, secrets через .env
- **Observability** — Prometheus metrics, journal event sourcing, journald logging
- **Recoverability** — авто-бэкапы pg_dump (24h, keep=7), retention зеркал

## 📋 Требования

### Production (минимум)

- **OS**: Linux x86_64 (Ubuntu 22.04+ / Debian 12+ / Fedora 38+)
- **RAM**: 4 ГБ минимум (2 ГБ — приложение, 2 ГБ — PostgreSQL + Kafka)
- **Disk**: 20 ГБ (10 ГБ — данные, 10 ГБ — бэкапы + теневые копии)
- **CPU**: 2 cores (production), 4+ для multi-instance

### Опционально (для полной функциональности)

- **GPU**: 2 ГБ VRAM для Ollama (qwen2.5:1.5b-instruct, фоновое обогащение памяти)
- **Tesseract OCR**: для extraction MCP (image OCR)
- **ffmpeg**: для media MCP (audio/video transcription через Whisper)
- **ripgrep + fd**: для code_analysis MCP (быстрый поиск)

## 🐳 Способ 1: Docker Compose (рекомендуется для production)

### Шаг 1. Клонировать репозиторий

```bash
git clone https://github.com/<your-org>/llm-agent.git
cd llm-agent
git checkout v2026-10-01  # production tag
```

### Шаг 2. Создать `.env` из шаблона

```bash
cp deploy/env.example .env
nano .env
```

Заполнить критичные переменные:

```env
# ─── Сервер ────────────────────────────────────────────────────────
HOST=0.0.0.0
PORT=8000

# ─── PostgreSQL (использует сервис postgres из docker-compose) ─────
PG_APP_HOST=postgres
PG_APP_PORT=5432
PG_APP_USER=llmagent
PG_APP_PASSWORD=<CHANGE_ME_STRONG_PASSWORD>
PG_APP_DATABASE=llmagent
PG_ENABLED=true
PG_REPLICATE=1

# ─── AGE (Apache AGE graph, опционально) ──────────────────────────
AGE_ENABLED=false  # true для полной функциональности

# ─── CDC + Kafka (опционально) ─────────────────────────────────────
KAFKA_ENABLED=false  # true для CDC

# ─── AI-модели (минимум одна) ──────────────────────────────────────
# Способ 1: DeepSeek API
DEEPSEEK_API_KEY=sk-...
DEEPSEEK_BASE_URL=https://api.deepseek.com/v1

# Способ 2: OpenAI-совместимый (DeepSeek/OpenAI/Ollama/qwenproxy)
OPENAI_API_KEY=sk-...
LLM_BASE_URL=http://localhost:9999/v1  # или https://api.openai.com/v1
LLM_MODEL=deepseek-chat

# ─── Бэкапы ───────────────────────────────────────────────────────
BACKUP_ENABLED=true
BACKUP_INTERVAL_HOURS=24
BACKUP_KEEP_LAST=7

# ─── Ретенция зеркал (опционально, 0=OFF) ─────────────────────────
PG_RETENTION_DAYS=90  # удалять записи старше 90 дней
PG_RETENTION_INTERVAL_HOURS=24
```

### Шаг 3. Поднять инфраструктуру + приложение

```bash
# Сборка образа llm-agent (5-15 минут, multi-stage build)
docker compose -f docker-compose.yml -f docker-compose.patch.yml build

# Запуск всех сервисов (postgres + AGE + Kafka + llm-agent)
docker compose -f docker-compose.yml -f docker-compose.patch.yml up -d

# Проверка статуса
docker compose -f docker-compose.yml -f docker-compose.patch.yml ps
# Все сервисы должны быть "healthy" через 60-90 секунд
```

### Шаг 4. Проверка

```bash
# Healthcheck (Docker HEALTHCHECK использует этот же эндпоинт)
curl http://127.0.0.1:8000/api/db/health
# Ожидается: {"enabled": true, "healthy": true}

# Список агентов
curl http://127.0.0.1:8000/api/registry/agents | jq

# Status всех подсистем
curl http://127.0.0.1:8000/api/db/status | jq

# Веб-чат в браузере
open http://127.0.0.1:8000
```

### Шаг 5. Reverse proxy (nginx, для HTTPS)

```nginx
# /etc/nginx/sites-available/llm-agent.conf
upstream llmagent {
    # sticky sessions через ip_hash (WebSocket корректно)
    ip_hash;
    server instance-a:8001;
    server instance-b:8002;
}

server {
    listen 443 ssl http2;
    server_name llm-agent.example.com;

    ssl_certificate /etc/letsencrypt/live/llm-agent.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/llm-agent.example.com/privkey.pem;

    location / {
        proxy_pass http://llmagent;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 600s;  # WS-стриминг
    }

    # Rate limiting (защита от DDoS)
    limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
    location /api/ {
        limit_req zone=api burst=20 nodelay;
        proxy_pass http://llmagent;
    }
}
```

### Шаг 6. Observability (опционально)

```bash
# Prometheus scrape config:
# scrape_configs:
#   - job_name: 'llm-agent'
#     scrape_interval: 30s
#     static_configs:
#       - targets: ['llm-agent:8000']
#     metrics_path: /metrics

# Проверка метрик:
curl http://127.0.0.1:8000/metrics | head -20
```

## 🔧 Способ 2: Bare-metal (для разработки/теста)

### Шаг 1. Установить Python 3.11+

```bash
# Ubuntu/Debian
sudo apt install python3.11 python3.11-venv python3.11-dev

# macOS
brew install python@3.11

# Проверка
python3.11 --version  # Python 3.11.x
```

### Шаг 2. Установить системные зависимости (опционально, для отдельных MCP)

```bash
# Debian/Ubuntu
sudo apt install ripgrep fd-find ffmpeg tesseract-ocr tesseract-ocr-rus \
                 libpq-dev libffi-dev libjpeg-dev zlib1g-dev

# Fedora
sudo dnf install ripgrep fd-find ffmpeg tesseract tesseract-langpack-rus
```

### Шаг 3. Клонировать и установить

```bash
git clone https://github.com/<your-org>/llm-agent.git
cd llm-agent
git checkout v2026-10-01

# Создать venv
python3.11 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# .venv\Scripts\activate   # Windows

# Обновить pip
pip install --upgrade pip setuptools wheel

# Установить зависимости (pinned версии)
pip install -r requirements.txt
pip install -r requirements-dev.txt

# Установить пакет в editable-режиме (для импортов src.*)
pip install -e .

# Установить pre-commit hooks
pre-commit install
pre-commit install --hook-type pre-push

# Browser MCP (опционально, для веб-агента)
pip install playwright && playwright install chromium
```

### Шаг 4. PostgreSQL (опционально, но рекомендуется для production)

```bash
# Установить PostgreSQL 16 + pgvector
sudo apt install postgresql-16 postgresql-16-pgvector

# Создать пользователя и БД
sudo -u postgres psql -c "CREATE USER llmagent WITH PASSWORD 'secret';"
sudo -u postgres psql -c "CREATE DATABASE llmagent OWNER llmagent;"

# Применить схему
python scripts/init_db.py
python scripts/init_db.py --check  # проверить состояние
```

### Шаг 5. Настроить `.env`

```bash
cp deploy/env.example .env
nano .env  # заполнить (см. Способ 1, Шаг 2)
```

### Шаг 6. Запустить

```bash
# Development mode (reload=True, single process)
python run.py
# или:
python main.py --host 0.0.0.0 --port 8000

# Production mode (печатает инструкцию по gunicorn)
python main.py --prod
# gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8000 src.app:app
```

### Шаг 7. Systemd service (production bare-metal)

```bash
sudo cp deploy/llm-agent.service /etc/systemd/system/
sudo nano /etc/systemd/system/llm-agent.service  # поправить User/WorkingDirectory
sudo systemctl daemon-reload
sudo systemctl enable llm-agent
sudo systemctl start llm-agent
sudo systemctl status llm-agent
```

## 🐛 Диагностика

### Сервер не стартует

```bash
# Логи (Docker):
docker compose logs llm-agent | tail -100

# Логи (bare-metal):
journalctl -u llm-agent -f

# Частые причины:
# 1. PostgreSQL недоступен → PG_ENABLED=false или починить DSN
# 2. Зависимости не установлены → pip install -r requirements.txt
# 3. .env не создан → cp deploy/env.example .env && заполнить
# 4. Порт 8000 занят → lsof -i :8000, или PORT=8001 в .env
```

### WebSocket не работает

```bash
# Проверить через websocat (или браузер):
websocat ws://127.0.0.1:8000/ws
# {"type": "hello", "session_id": "__new__"}
# Ожидается: {"type": "session_restored", "session_id": "...", ...}

# Если 404 — роутер не зарегистрирован. Проверить src/app.py:
grep "include_router(ws_router)" src/app.py
```

### MCP-серверы падают

```bash
# Проверить контракт MCP 1.x:
pytest tests/test_mcp_1x_contract.py -v

# Если установлен mcp 2.x — DOWNGRADE:
pip install "mcp>=1.1.3,<2.0"
pip show mcp | grep Version  # должно быть 1.x
```

### Тесты падают

```bash
# Полный прогон:
pytest tests/ -v --tb=short

# С покрытием:
pytest tests/ --cov=src --cov-report=term-missing

# Один тест:
pytest tests/test_orchestrator.py::test_route_empty_runtime_returns_no_agents -v

# Известные flaky: test_initialize_with_pg_pool требует psycopg —
# pytest mark skipif(not has_psycopg)
```

## 📊 Production-readiness чеклист

- [ ] `.env` не в git (`git status .env` → "untracked" или в `.gitignore`)
- [ ] Создан отдельный PostgreSQL-пользователь с минимальными правами
- [ ] `development.json` профиль НЕ активен в production
- [ ] Включён `production.json` профиль (`shell`, `git` агенты отключены)
- [ ] `BACKUP_ENABLED=true` и `BACKUP_KEEP_LAST≥7`
- [ ] `PG_RETENTION_DAYS>0` — авто-ретенция зеркал
- [ ] Docker-контейнер запускается от непривилегированного пользователя (USER llmagent)
- [ ] Healthcheck эндпоинт `/api/db/health` отвечает 200 в течение 60 секунд
- [ ] Мониторинг логов на `ERROR`/`CRITICAL` настроен (Promtail → Loki или ELK)
- [ ] SSL/TLS через nginx (см. конфиг выше)
- [ ] Rate-limiting на `/ws` и `/api/*` эндпоинты
- [ ] CODECOV_TOKEN добавлен в GitHub secrets (для CI coverage)
- [ ] `pre-commit run --all-files` проходит без ошибок
- [ ] `pytest tests/ -v` ≥50 кейсов зелёные
- [ ] `mypy --config-file mypy.ini src/` — без критичных ошибок
- [ ] Docker build: `docker build -t llm-agent:v2026-10-01 .` — успешно
- [ ] `docker compose up -d` — все сервисы healthy
- [ ] Backup проверен: `curl -X POST http://127.0.0.1:8000/api/backup/create`
- [ ] `.github/SECURITY.md` заполнен (email + PGP fingerprint)

## 🔄 Backup & Restore

### Авто-бэкапы (PostgreSQL)

Включены по умолчанию (`BACKUP_ENABLED=true`):
- Период: 24 часа (`BACKUP_INTERVAL_HOURS=24`)
- Хранить: 7 последних (`BACKUP_KEEP_LAST=7`)
- Каталог: `data/db_backups/`
- Формат: `pg_dump --format=custom` (быстрый restore)

### Ручной backup

```bash
# Создать:
curl -X POST http://127.0.0.1:8000/api/backup/create
# {"ok": true, "backup": "llmagent_20261001_120000.dump"}

# Список:
curl http://127.0.0.1:8000/api/backup/list

# Восстановить (требует approval):
curl -X POST http://127.0.0.1:8000/api/backup/restore \
     -H "Content-Type: application/json" \
     -d '{"name": "llmagent_20261001_120000.dump"}'

# Удалить:
curl -X DELETE http://127.0.0.1:8000/api/backup/llmagent_20261001_120000.dump
```

### Journal (event sourcing) — откат изменений

```bash
# События журнала:
curl http://127.0.0.1:8000/api/journal/events?task_id=... | jq

# Поиск:
curl 'http://127.0.0.1:8000/api/journal/search?q=create_file' | jq

# Откат (dry_run сначала!):
curl -X POST http://127.0.0.1:8000/api/journal/rollback \
     -H "Content-Type: application/json" \
     -d '{"event_id": "...", "mode": "dry_run"}'

# Если dry_run OK — выполнить:
curl -X POST http://127.0.0.1:8000/api/journal/rollback \
     -H "Content-Type: application/json" \
     -d '{"event_id": "...", "mode": "execute"}'
```

## 📈 Observability

### Metrics (Prometheus)

`GET /metrics` отдаёт exposition format:

```
# HELP llm_agent_agents_total Total registered agents
# TYPE llm_agent_agents_total gauge
llm_agent_agents_total{status="active"} 12
llm_agent_agents_total{status="degraded"} 0

# HELP llm_agent_mcp_servers_total Total MCP servers
# TYPE llm_agent_mcp_servers_total gauge
llm_agent_mcp_servers_total{status="alive"} 35

# HELP llm_agent_ws_clients Active WebSocket clients
# TYPE llm_agent_ws_clients gauge
llm_agent_ws_clients 3

# HELP llm_agent_uptime_seconds Server uptime
# TYPE llm_agent_uptime_seconds counter
llm_agent_uptime_seconds 3600
```

### Logs (journald в Docker)

```bash
# Все логи:
docker compose logs llm-agent

# Только ERROR и выше:
docker compose logs llm-agent | grep -E "ERROR|CRITICAL"

# Живая лента:
docker compose logs -f llm-agent
```

### Analytics Dashboard

`http://127.0.0.1:8000/analytics` — веб-дашборд с:
- 6 materialized views (overview, daily, hourly, enrichment, agents, topics, tools, files)
- WebSocket realtime updates (auto-refresh каждые 15 минут)
- Графики активности по дням/часам

## 🚀 Multi-instance (horizontal scaling)

### Через nginx (sticky sessions)

```nginx
upstream llmagent {
    ip_hash;  # CRITICAL для WebSocket
    server llm-agent-a:8000;
    server llm-agent-b:8000;
    server llm-agent-c:8000;
}
```

### Через Redis (cluster bus)

Включён через `src/cluster/` — heartbeat + WS bridge для синхронизации
состояния между инстансами. Redis запускается отдельно:

```yaml
# docker-compose.patch.yml — добавить:
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]
```

## 🆘 Поддержка

- **Issues**: https://github.com/<your-org>/llm-agent/issues
- **Security (private)**: см. `.github/SECURITY.md` (email + PGP)
- **Docs**: `docs/{ARCHITECTURE-V2,CAPABILITIES,FAQ,GETTING_STARTED,JOURNAL,DATABASE,SECURITY}.md`
- **Interactive course**: `http://127.0.0.1:8000/guide` (10 уроков)

## 📚 Связанные документы

- `RELEASE_NOTES-v2026-10-01.md` — что изменилось в этом релизе
- `POST-MORTEM.pdf` — итоговый отчёт о трансформации
- `llm-agent-archives-analysis.pdf` — исходный технический анализ
- `docs/` — 14 markdown-документов по архитектуре

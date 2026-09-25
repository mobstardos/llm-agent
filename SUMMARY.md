# 📦 LLM Agent — итоговая документация

## Что это

Мультиагентная система для работы с кодом, базами данных и 1С через
веб-чат. 38 MCP-серверов, 36 агентов, PostgreSQL+pgvector,
Ollama (малая LLM), Apache AGE, Kafka, Kubernetes.

## Что реализовано (7 частей)

### Часть 1. PostgreSQL + pgvector
- Одна база, 8 схем: memory, vectors, graph, cache, policies, audit,
  metrics, meta
- pgvector 0.7+ с three-tier поиском (bit → halfvec → full)
- Гибридный поиск RRF (vector + BM25)
- Multi-instance (advisory locks + LISTEN/NOTIFY)
- Партиционирование по месяцам

**Файлы:**
- `db/init.sql`
- `src/db/pool.py`
- `src/db/vector_store.py`
- `src/db/memory_store.py`
- `src/db/graph_store.py`
- `src/db/hybrid_search.py`
- `src/db/multi_instance.py`

### Часть 2. Ollama + Enrichment
- Малая LLM (qwen2.5:1.5b, ~1 GB VRAM)
- Фоновое обогащение событий (importance, topic, tags)
- Батчевая обработка + advisory lock
- Fallback на эвристику

**Файлы:**
- `src/ollama/client.py`
- `src/ollama/enricher.py`
- `src/ollama/worker.py`

### Часть 3. Analytics
- 6 materialized views
- Дашборд `/analytics`
- WebSocket realtime
- Auto-refresh каждые 15 мин

**Файлы:**
- `db/analytics.sql`
- `src/db/analytics.py`
- `src/web/analytics.html`

### Часть 4. AGE + CDC + Kafka
- Apache AGE (property graph, Cypher)
- CDC через PostgreSQL NOTIFY → Kafka
- Kafka publisher с буферизацией
- 6 topics

**Файлы:**
- `db/age_schema.sql`
- `db/cdc_notify.sql`
- `src/db/age_store.py`
- `src/db/age_sync.py`
- `src/cdc/kafka_publisher.py`
- `src/cdc/notify_worker.py`
- `docker-compose.yml`

### Часть 5. Bonus
- Snowball + синонимы (100+ терминов)
- BackupManager (pg_dump с ротацией)
- PGMetrics (15+ Prometheus метрик)
- Realtime WS для enrichment

**Файлы:**
- `db/search_improvements.sql`
- `src/db/search_helpers.py`
- `src/db/backup.py`
- `src/db/pg_metrics.py`

### Часть 6. Миграции + интеграция
- Полный `AppSettings` с Pydantic
- Memory facade с PostgreSQL + fallback + dual-write
- 3 скрипта миграции
- Полный `.env.example`
- Полный `README.md`

**Файлы:**
- `src/config.py`
- `src/memory/facade.py`
- `scripts/migrate_lancedb_to_pg.py`
- `scripts/migrate_sqlite_memory.py`
- `scripts/migrate_graph_to_pg.py`
- `.env.example`
- `README.md`

### Часть 7. Web UI
- Обновлённый чат с системным индикатором
- 11 табов в настройках
- Управление агентами, MCP, capabilities, политиками
- Бэкапы, синонимы, AGE, CDC
- Real-time обновления

**Файлы:**
- `src/web/index.html`
- `src/web/app.js`
- `src/web/style.css`

## Быстрый старт


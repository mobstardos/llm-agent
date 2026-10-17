# 🐘 PostgreSQL: долговременное хранилище llm-agent

Документ описывает, как проект хранит «всё, что наработано» в PostgreSQL:
журнал операций, диалоги чата, планы Supervisor, память и векторы — и как
гарантируется, что **ничего не теряется** даже при простое базы.

---

## 1. Два уровня хранения

Проект осознанно разделяет «рабочие» и «архивные» данные:

| Уровень | Что | Где | Зачем |
|---|---|---|---|
| **Рабочий (hot)** | журнал операций (SQLite WAL + FTS5), диалоги (`data/sessions/*.jsonl`), планы (`data/plans/*.json`), episodic-память (`data/memory.sqlite`), векторы (LanceDB/pgvector) | локальные файлы | скорость, работа без БД, офлайн |
| **Долговременный (cold)** | зеркала всего перечисленного + векторная память + аналитика | PostgreSQL | ничего не теряется, SQL-анализ, бэкапы, долговечность |

Приложение **никогда не зависит** от PostgreSQL: если база недоступна,
система работает на локальных хранилищах, а репликатор догружает пропущенное
после восстановления соединения.

## 2. Что именно попадает в PostgreSQL

### Схема `journal` (db/journal.sql + db/ops.sql)

| Таблица | Содержимое |
|---|---|
| `journal.events_mirror` | **полное зеркало** каждого события журнала SQLite (1:1, JSONB-поля): tool calls, задачи, запуски агентов, откаты. Ключ `event_uid` = `event_id` из SQLite |
| `journal.actions` | аналитическая проекция tool_call-событий (категории read/write/patch/delete/move/query/external/admin, хэши, длительность, затронутые файлы) |
| `journal.snapshots / blobs / dependencies / checkpoints / replays / rollbacks` | зарезервировано схемой v1 (тени, content-addressed блобы, граф зависимостей) |

### Схема `ops` (db/ops.sql)

| Таблица | Содержимое |
|---|---|
| `ops.chat_sessions` | диалоги чата: счётчик сообщений, последний статус, meta |
| `ops.chat_messages` | каждое сообщение (роль, агент, текст, время); ключ `(session_id, seq)` |
| `ops.plans` | планы Supervisor: статус, успех, число шагов/провалов/ре-планов + полный JSONB-дамп |
| `ops.sync_state` | курсоры репликатора (ключ → JSONB) |

### Схемы памяти (db/init.sql)

`memory.sessions / messages / events / summaries` (партиционированы по месяцам),
`vectors.chunks` (pgvector: HNSW + IVFFlat + триграммы + русский tsvector),
`graph.nodes / edges`, `cache.*`, `policies.approval`, `audit.settings`
(append-only триггер), `metrics.loop_runs`, матвьюхи аналитики (`db/analytics.sql`).

## 3. Репликатор (src/db/replicator.py)

Фоновая задача в lifespan (стартует автоматически, `PG_REPLICATE=0` — выключить).
Каждые `PG_REPLICATE_INTERVAL` секунд (по умолчанию 15):

1. **Журнал**: читает из SQLite события с `id > курсора` (read-only, в отдельном
   потоке) → вставляет в `journal.events_mirror` + проекцию в `journal.actions`.
2. **Чат**: читает хвост каждого `data/sessions/<id>.jsonl` по байтовому офсету →
   `ops.chat_sessions` / `ops.chat_messages`. Частичная строка (без `\n`)
   не потребляется — ждём дозапись.
3. **Планы**: по `(mtime, size)` файла замечает изменения `data/plans/<id>.json` →
   upsert `ops.plans`.

Гарантии:

- **Курсор продвигается только после успешного коммита партии** (данные + курсор
  в одной транзакции). Обрыв связи/рестарт БД → после восстановления репликация
  продолжается с места остановки, ничего не теряется и не дублируется.
- **Идемпотентность**: `events_mirror` — по PK `event_uid`; сообщения — по
  `(session_id, seq)`; планы — upsert; проекция `actions` — предварительная
  фильтрация существующих `event_uid` (партиционированная таблица не имеет
  глобального UNIQUE).
- **Никогда не падает и не блокирует чат**: все ошибки гасятся в счётчиках;
  при недоступной БД — cooldown 60 с между попытками.
- **Ноль правок горячего пути**: репликатор только читает файлы и SQLite.

Наблюдение: `GET /api/db/status` → здоровье PG, бэкенд памяти, счётчики
(`journal_events`, `messages`, `plans`, `errors`, время цикла).

## 4. Быстрый старт

### Вариант A — Docker (Linux/Windows/Mac)

```bash
docker compose up -d postgres      # pgvector/pgvector:pg16, схема сама
# в .env:
DATABASE_URL=postgresql://llmagent:secret@localhost:5432/llmagent
```

### Вариант B — нативно на Windows

1. Установите PostgreSQL 16 (EBD installer) + pgvector
   (`CREATE EXTENSION vector;` появится в `share/` после распаковки релиза
   pgvector под вашу версию).
2. Создайте базу и примените схему:

```powershell
psql -U postgres -c "CREATE USER llmagent WITH PASSWORD 'secret';"
psql -U postgres -c "CREATE DATABASE llmagent OWNER llmagent;"
python scripts/init_db.py          # идемпотентно, можно запускать повторно
python scripts/init_db.py --check  # проверить состояние
```

### Проверка

```bash
python scripts/init_db.py --check
curl http://localhost:8000/api/db/status
```

### Автодетект при каждом запуске (Task 24-b)

`run.py` (и `lifespan` сервера) при **каждом** старте проверяет PostgreSQL —
`src/db/autodetect.py`, отчёт всегда в `data/db_autodetect.json` и
`GET /api/db/autodetect`:

| Ситуация | Статус | Что делает система |
|---|---|---|
| Текущий DSN отвечает | `already` | ничего (конфиг не трогается) |
| Порт 5432/5433 закрыт | `no_service` | работает дальше на SQLite/LanceDB |
| Служба есть, DSN не отвечает | словарь | перебор дефолтных паролей (postgres, admin, 123456, …) |
| Словарь не сработал, есть TTY | `via=prompt` | запрос пароля админа (getpass, 3 попытки) |
| Пароль так и не найден | `manual` | подсказка как прописать `PG_APP_PASSWORD`/`DATABASE_URL` |
| `DATABASE_URL` задан, но не отвечает | `manual` | **чужой конфиг автоматически не переписывается** |

После успеха (словарь или ввод) выполняется bootstrap: создаётся
собственная роль приложения `llmagent` со сгенерированным паролем и база
`llmagent` (идемпотентно, `CREATE/ALTER ROLE`, `CREATE DATABASE`), затем
схема (`src.db.init_db`) применяется **от имени админа** — так поднимаются
расширения (pgvector/pg_trgm, требующие суперпользователя). Новые
`PG_APP_*` пишутся в `.env` и в `os.environ` текущего процесса.
Явный пароль админа нигде не сохраняется и не логируется.

## 5. Переменные окружения

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `PG_ENABLED` | `true` | включает PG-бэкенд памяти (при недоступности — мягкий откат на SQLite) |
| `DATABASE_URL` | — | полный DSN; принимается **только** `postgres://`/`postgresql://` (чужие `file:`/`sqlite:` игнорируются с предупреждением) |
| `PG_APP_HOST/PORT/USER/PASSWORD/DATABASE` | `localhost/5432/llmagent/secret/llmagent` | компоненты DSN, если нет `DATABASE_URL` |
| `PG_POOL_MIN / PG_POOL_MAX` | `2 / 20` | пул соединений памяти |
| `PG_REPLICATE` | `1` | фоновая репликация журнала/сессий/планов |
| `PG_REPLICATE_INTERVAL` | `15` | период цикла репликатора, сек |
| `AGENT_MEMORY` | `1` | фоновый индексатор памяти агентов (зеркала → `memory.tasks`) |
| `AGENT_MEMORY_INTERVAL` | `120` | период индексации, сек |
| `AGENT_MEMORY_EMBEDDER` | `auto` | `auto` (bge-m3 при доступности, иначе hashing) / `model` / `hash` |
| `PG_RETENTION_DAYS` | `0` | авто-ретенция зеркал: удалять строки старше N дней (`0` = выключено) |
| `PG_RETENTION_INTERVAL_HOURS` | `24` | период цикла авто-ретенции, часы |
| `INSTANCE_ID` | `hostname-pid` | идентификатор инстанса в реестре `ops.instances` |
| `PRIMARY_VECTOR_STORE / PRIMARY_EPISODIC_STORE / PRIMARY_GRAPH_STORE` | `postgres` | куда писать: `postgres`, `sqlite`/`lancedb`, `dual` |
| `BACKUP_ENABLED / BACKUP_INTERVAL_HOURS / BACKUP_KEEP_LAST` | `true / 24 / 7` | авто-бэкапы pg_dump |
| `AGE_ENABLED`, `AGE_DSN` | `false` | граф Apache AGE (порт 5433, отдельная БД) |
| `KAFKA_ENABLED` | `false` | CDC-публикация событий в Kafka |

## 6. Перенос уже накопленных данных

```bash
python scripts/migrate_sqlite_memory.py        # episodic-память SQLite → PG
python scripts/migrate_graph_to_pg.py          # граф знаний → PG
python scripts/migrate_lancedb_to_pg.py        # векторы LanceDB → pgvector
```

Журнал и чаты переносятся репликатором автоматически при первом запуске
(он догружает всю существующую историю по курсорам).

## 7. Бэкапы и восстановление

`src/db/backup.BackupManager` (включён по умолчанию при живом PG) каждый
`BACKUP_INTERVAL_HOURS` делает сжатый `pg_dump` в `data/db_backups/`,
хранит последние `BACKUP_KEEP_LAST` копий. Восстановление:

```bash
pg_restore -U llmagent -d llmagent --clean data/db_backups/<файл>.dump
```

Дополнительно: локальные SQLite/JSONL остаются на месте — это «холодная
копия» последнего состояния даже при полном отказе БД.

## 8. Полезные запросы

```sql
-- Вся история операций одной сессии (полное зеркало)
SELECT ts, kind, server_name, tool_name, status, duration_ms, error
FROM journal.events_mirror
WHERE session_id = 'abc123'
ORDER BY ts;

-- Кто и как часто писал в файлы за неделю
SELECT agent, tool, count(*), avg(duration_ms)
FROM journal.actions
WHERE category IN ('write','patch','delete') AND created_at > now() - interval '7 days'
GROUP BY agent, tool ORDER BY count(*) DESC;

-- Последние планы и их успешность
SELECT plan_id, status, success, steps_done, steps_failed, replans
FROM ops.plans ORDER BY updated_at DESC LIMIT 20;

-- Долгота диалогов
SELECT session_id, message_count, last_seen FROM ops.chat_sessions
ORDER BY last_seen DESC LIMIT 20;
```

## 9. Фича «История» и экспорт отчётов

Просмотр зеркал без SQL — фича `features/history` (подключается через
реестр фич, вкладка «📜 История» в настройках UI):

| Эндпоинт | Что возвращает |
|---|---|
| `GET /api/ops/status` | доступность PG, счётчики зеркал, наличие FTS |
| `GET /api/ops/sessions?q=` | список диалогов (превью последнего сообщения, фильтр) |
| `GET /api/ops/sessions/{id}/messages` | сообщения сессии по порядку |
| `GET /api/ops/sessions/{id}/export` | выгрузка сессии в Markdown (attachment) |
| `GET /api/ops/plans`, `GET /api/ops/plans/{id}` | планы Supervisor (полный дамп в payload) |
| `GET /api/ops/events?kind=&tool=&session_id=&trace_id=` | поток событий журнала |
| `GET /api/ops/search?q=` | полнотекстовый поиск: переписки + события |

Поиск использует FTS-колонки `tsv` (генерируемые, GIN-индексы — см.
конец `db/ops.sql`): русская морфология, ранжирование `ts_rank`.
Если колонок нет (старая схема) — мягкий откат на `ILIKE`
(режим виден в ответе: `mode: fts | ilike`). Фича только читает:
недоступный PG → HTTP 503, чат и репликатор не затрагиваются.

Экспорт отчётов из терминала (PG-зеркала, при их отсутствии —
локальные файлы): `scripts/export_report.py`

```bash
python scripts/export_report.py --sessions-list                # список сессий
python scripts/export_report.py --session <id> --out report.md # сессия → Markdown
python scripts/export_report.py --plan <id>                    # план → Markdown
python scripts/export_report.py --events --hours 24 --format csv --out ev.csv
```

Тесты: `scripts/test_history_feature.py` (юнит + интеграция на живом PG).

## 10. Память агентов, кластер и эксплуатация (Task 14)

Три фичи поверх зеркал, подключённые через реестр (вкладки в настройках
UI), — горячий путь чата не затрагивается, недоступный PG → 503.

### 🧠 Память агентов (pgvector) — `features/memory`

Семантический индекс прошлых задач в `memory.tasks` (db/init.sql).
Наполняется фоновым индексатором (main.py блок 7.5) из зеркал: диалоги
чата (`kind=chat`), запросы планов (`kind=plan`), ошибки инструментов
(`kind=event`). Поиск — трёхуровневый с честным признаком режима:
`vector` (pgvector, halfvec + HNSW) → `fts` (русская морфология) →
`ilike`. Без pgvector (или при `AGENT_MEMORY_EMBEDDER=hash`) качество
ранжирования ниже, но система работает.

| Эндпоинт | Что делает |
|---|---|
| `GET /api/memory/stats` | счётчики, режим (pgvector/FTS), embedder |
| `POST /api/memory/search` | `{"query", "top_k", "kind", "session_id"}` — поиск по смыслу |
| `GET /api/memory/recent` | последние записи |
| `POST /api/memory/index/run` | ручной прогон индексации зеркал |
| `DELETE /api/memory?confirm=true` | очистка (целиком или `kind=`) |

Embedder выбирается `AGENT_MEMORY_EMBEDDER`: `model` — bge-m3
(sentence-transformers, качественно, тяжело), `hash` — детерминированный
feature-hashing на stdlib (мгновенно, для офлайна/CI), `auto` — модель
при доступности, иначе hashing.

### 🌐 Кластер (мультиинстанс-аналитика) — `features/cluster`

Реестр инстансов `ops.instances` (регистрация при старте, heartbeat
каждые 30с, JSONB-метрики раз в минуту — агенты/uptime). Аналитика
агрегирует реестр и нагрузку по зеркалам за окно.

| Эндпоинт | Что возвращает |
|---|---|
| `GET /api/cluster/instances` | активные / отсутствующие инстансы с метриками |
| `GET /api/cluster/analytics?hours=24` | события/tool-calls/сессии/планы за окно, почасовая нагрузка, топ инструментов и агентов |
| `POST /api/cluster/cleanup?ttl=21600` | вычистить записи без heartbeat дольше ttl |

Запуск нескольких нод на одной базе: у каждой свой `INSTANCE_ID`,
конкуренцию за фоновые задачи разруливают advisory-локи
(`src/db/multi_instance.py`).

### 🛠 Эксплуатация (бэкапы и ретенция) — `features/ops`

| Эндпоинт | Что делает |
|---|---|
| `GET /api/ops-admin/overview` | PG + бэкапы + режим ретенции (работает и без PG: `pg.available=false`) |
| `GET /api/ops-admin/backups` | список дампов в `data/db_backups` |
| `POST /api/ops-admin/backups?confirm=true` | создать бэкап сейчас (pg_dump; ротация `BACKUP_KEEP_LAST`) |
| `GET /api/ops-admin/retention?days=N` | сколько строк в каждой таблице удаляемо |
| `POST /api/ops-admin/retention/run` | `{"days", "dry_run": true}` — отчёт; удаление только с `"dry_run": false, "confirm": true` |

Ретенция (`src/db/retention.py`) удаляет пакетами по PK из
`journal.events_mirror`, `journal.actions`, `ops.chat_messages`,
`ops.plans`, `memory.tasks`. Курсоры репликатора (`ops.sync_state`) —
монотонные позиции потока, поэтому удаление уже спроецированных строк
безопасно: догоняющая репликация продолжается без потерь. Авто-режим
выключен по умолчанию: включается `PG_RETENTION_DAYS>0`.

Тесты: `scripts/test_ops_pg.py` (юнит + интеграция на живом PG,
включая FTS-деградацию и 503).

## 11. Возможности развития (роадмап)

- **pg_cron**: партиции будущих месяцев (`meta.create_next_partitions()`),
  ночные summary — силами самой БД.
- **BI-дашборд**: Metabase/Grafana поверх `metrics.mv_*` и `ops.plans`
  (стоимость токенов, успешность агентов, топ инструментов).
- **CDC → Kafka** (каркас уже есть: `src/cdc/`, `db/cdc_notify.sql`):
  реактивные интеграции и аудит в реальном времени.
- **Координация кластера через LISTEN/NOTIFY**: реактивная передача
  задач между нодами (реестр и аналитика уже готовы — §10).
- **Архив блобов в S3**: вынос теней/снапшотов журнала в объектное хранилище
  (`src/storage/s3_backend.py` уже существует).

## 12. Troubleshooting

| Симптом | Причина / решение |
|---|---|
| `connection refused` в логах репликатора | PG не запущен — система работает на SQLite; проверьте `DATABASE_URL`/контейнер |
| `role "llmagent" does not exist` | создайте роль (см. §4) или задайте свой `PG_APP_USER`; init_db пропустит GRANT как предупреждение |
| `extension "vector" is not available` | поставьте pgvector; без него векторная память живёт в LanceDB, остальное работает |
| `DATABASE_URL не похож на PostgreSQL DSN` | в `DATABASE_URL` попал `file:`/`sqlite:` — переменная проигнорирована, DSN собран из `PG_APP_*` |
| Репликатор «отстаёт» | проверьте `/api/db/status` (`last_error`, `last_cycle_seconds`); увеличьте `PG_REPLICATE_INTERVAL` при больших партиях |
| Данные в PG «старее» локальных | БД была недоступна — дождитесь цикла репликатора; он догрузит всё по курсорам автоматически |
| `/api/memory/stats` показывает `pgvector: false` | в БД нет pgvector или он старый (без `halfvec`) — память работает в FTS-режиме; обновите pgvector и переустановите схему (`python scripts/init_db.py`) |
| Ретенция ничего не удаляет | `PG_RETENTION_DAYS=0` (по умолчанию выключено) или `days=0` в ручном прогоне — укажите глубину в днях |

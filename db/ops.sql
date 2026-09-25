-- ═══════════════════════════════════════════════════════════════════════
-- LLM Agent — схема ops: долговременные зеркала локальных данных.
--
-- Локальные хранилища (SQLite-журнал, data/sessions/*.jsonl,
-- data/plans/*.json) остаются источником правды для работающего
-- приложения. Репликатор (src/db/replicator.py) фоново переносит их
-- в PostgreSQL — центральное долговременное хранилище, из которого
-- ничего не теряется (курсоры в ops.sync_state, идемпотентные вставки).
--
-- Идемпотентно: можно запускать многократно.
-- ═══════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS ops;

-- ─── ops.chat_sessions ────────────────────────────────────────────────
-- Зеркало диалогов чата (data/sessions/<id>.jsonl).
CREATE TABLE IF NOT EXISTS ops.chat_sessions (
    session_id   TEXT PRIMARY KEY,
    first_seen   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen    TIMESTAMPTZ NOT NULL DEFAULT now(),
    message_count INTEGER NOT NULL DEFAULT 0,
    last_role    TEXT NOT NULL DEFAULT '',
    meta         JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_ops_chatsessions_last
    ON ops.chat_sessions (last_seen DESC);

-- ─── ops.chat_messages ────────────────────────────────────────────────
-- Сообщения диалогов. seq = порядковый номер сообщения в сессии
-- (среди строк {"role": ...} исходного jsonl).
CREATE TABLE IF NOT EXISTS ops.chat_messages (
    session_id TEXT NOT NULL,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL,
    agent      TEXT NOT NULL DEFAULT '',
    content    TEXT NOT NULL DEFAULT '',
    ts         TIMESTAMPTZ,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_ops_chatmessages_ts
    ON ops.chat_messages (session_id, ts);

-- ─── ops.plans ────────────────────────────────────────────────────────
-- Зеркало планов Supervisor (data/plans/<plan_id>.json).
CREATE TABLE IF NOT EXISTS ops.plans (
    plan_id      TEXT PRIMARY KEY,
    session_id   TEXT NOT NULL DEFAULT '',
    query        TEXT NOT NULL DEFAULT '',
    intent       TEXT NOT NULL DEFAULT '',
    mode         TEXT NOT NULL DEFAULT 'sequential',  -- sequential | dag
    status       TEXT NOT NULL DEFAULT 'running',
    needs_approval BOOLEAN NOT NULL DEFAULT false,
    success      BOOLEAN,
    replans      INTEGER NOT NULL DEFAULT 0,
    steps_total  INTEGER NOT NULL DEFAULT 0,
    steps_done   INTEGER NOT NULL DEFAULT 0,
    steps_failed INTEGER NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ,
    updated_at   TIMESTAMPTZ,
    payload      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- полный дамп плана
    ingested_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ops_plans_session
    ON ops.plans (session_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_plans_status
    ON ops.plans (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ops_plans_payload
    ON ops.plans USING GIN (payload);

-- ─── journal.events_mirror ────────────────────────────────────────────
-- ПОЛНОЕ зеркало потока событий журнала (SQLite data/journal/journal.sqlite,
-- таблица events). Каждое поле сохраняется 1:1 → ничего не теряется.
-- event_uid = event_id из SQLite (UNIQUE) — идемпотентная догрузка.
CREATE TABLE IF NOT EXISTS journal.events_mirror (
    event_uid   TEXT PRIMARY KEY,
    seq         BIGINT NOT NULL,
    ts          TIMESTAMPTZ NOT NULL,
    iso_time    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT '',
    action      TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'ok',
    session_id  TEXT NOT NULL DEFAULT '',
    task_id     TEXT NOT NULL DEFAULT '',
    trace_id    TEXT NOT NULL DEFAULT '',
    agent_id    TEXT NOT NULL DEFAULT '',
    loop_id     TEXT NOT NULL DEFAULT '',
    iteration   INTEGER NOT NULL DEFAULT -1,
    server_name TEXT NOT NULL DEFAULT '',
    tool_name   TEXT NOT NULL DEFAULT '',
    targets     JSONB NOT NULL DEFAULT '[]'::jsonb,
    args        JSONB,
    args_digest TEXT NOT NULL DEFAULT '',
    before_hash TEXT NOT NULL DEFAULT '',
    after_hash  TEXT NOT NULL DEFAULT '',
    shadow_dir  TEXT NOT NULL DEFAULT '',
    reversible  INTEGER NOT NULL DEFAULT 0,
    inverse     JSONB NOT NULL DEFAULT '{}'::jsonb,
    parent_id   TEXT NOT NULL DEFAULT '',
    depends_on  JSONB NOT NULL DEFAULT '[]'::jsonb,
    duration_ms REAL NOT NULL DEFAULT 0,
    bytes_before BIGINT NOT NULL DEFAULT -1,
    bytes_after  BIGINT NOT NULL DEFAULT -1,
    error       TEXT NOT NULL DEFAULT '',
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    payload_path TEXT NOT NULL DEFAULT '',
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jmirror_ts
    ON journal.events_mirror (ts DESC);
CREATE INDEX IF NOT EXISTS idx_jmirror_kind
    ON journal.events_mirror (kind, ts DESC);
CREATE INDEX IF NOT EXISTS idx_jmirror_session
    ON journal.events_mirror (session_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_jmirror_trace
    ON journal.events_mirror (trace_id);
CREATE INDEX IF NOT EXISTS idx_jmirror_task
    ON journal.events_mirror (task_id);
CREATE INDEX IF NOT EXISTS idx_jmirror_tool
    ON journal.events_mirror (server_name, tool_name);
CREATE INDEX IF NOT EXISTS idx_jmirror_args
    ON journal.events_mirror USING GIN (args);
CREATE INDEX IF NOT EXISTS idx_jmirror_meta
    ON journal.events_mirror USING GIN (meta);

-- ─── journal.actions.event_uid ────────────────────────────────────────
-- Аналитическая проекция tool_call-событий в journal.actions (db/journal.sql):
-- ссылка на исходное событие для трассировки.
-- ВАЖНО: journal.actions партиционирована по created_at, поэтому UNIQUE-индекс
-- по одному event_uid невозможен (PG требует включать ключ партиционирования).
-- Идемпотентность проекции обеспечивает репликатор: перед вставкой он
-- отбрасывает event_uid, уже присутствующие в journal.actions.
ALTER TABLE journal.actions ADD COLUMN IF NOT EXISTS event_uid TEXT;
CREATE INDEX IF NOT EXISTS idx_jactions_event_uid
    ON journal.actions (event_uid)
    WHERE event_uid IS NOT NULL AND event_uid <> '';

-- ─── ops.sync_state ───────────────────────────────────────────────────
-- Курсоры репликатора: journal.last_id, session:<id> → {offset, seq},
-- plan:<id> → {mtime, size}. Курсор продвигается ТОЛЬКО после успешного
-- коммита → после сбоя/простоя репликация продолжается без потерь.
CREATE TABLE IF NOT EXISTS ops.sync_state (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Полнотекстовый поиск (FTS) ───────────────────────────────────────
-- Сгенерированные tsvector-колонки + GIN-индексы. Форма to_tsvector
-- (config, text) иммутабельна → разрешена в GENERATED STORED (PG12+).
-- Фича «История» (features/history) использует их в /api/ops/search;
-- при отсутствии колонок поиск мягко падает в ILIKE.
ALTER TABLE ops.chat_messages ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('russian', content)) STORED;
CREATE INDEX IF NOT EXISTS idx_ops_chatmessages_tsv
    ON ops.chat_messages USING GIN (tsv);

ALTER TABLE journal.events_mirror ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('russian',
        coalesce(error, '')   || ' ' ||
        coalesce(tool_name, '') || ' ' ||
        coalesce(server_name, '') || ' ' ||
        coalesce(kind, ''))) STORED;
CREATE INDEX IF NOT EXISTS idx_jmirror_tsv
    ON journal.events_mirror USING GIN (tsv);

-- ─── ops.instances: реестр инстансов (мультиинстанс-аналитика) ───────
-- Каждый инстанс регистрирует себя (InstanceRegistry, src/db/multi_instance.py),
-- шлёт heartbeat и раз в минуту метрики (JSONB-снапшот состояния).
-- «Аналитика» агрегирует реестр + нагрузку по зеркалам за окно (features/cluster).
CREATE TABLE IF NOT EXISTS ops.instances (
    instance_id    TEXT PRIMARY KEY,
    hostname       TEXT NOT NULL DEFAULT '',
    pid            INTEGER NOT NULL DEFAULT 0,
    role           TEXT NOT NULL DEFAULT 'agent',
    version        TEXT NOT NULL DEFAULT '',
    started_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_heartbeat TIMESTAMPTZ NOT NULL DEFAULT now(),
    metrics        JSONB NOT NULL DEFAULT '{}'::jsonb,
    meta           JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ops_instances_hb
    ON ops.instances (last_heartbeat DESC);

-- ─── Права ────────────────────────────────────────────────────────────
GRANT USAGE ON SCHEMA ops TO llmagent;
GRANT ALL ON ALL TABLES IN SCHEMA ops TO llmagent;
GRANT ALL ON ALL SEQUENCES IN SCHEMA ops TO llmagent;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA ops TO llmagent;

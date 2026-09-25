-- ═══════════════════════════════════════════════════════════════════════
-- LLM Agent — инициализация PostgreSQL
-- Одна база, 8 схем, pgvector 0.7+
-- ═══════════════════════════════════════════════════════════════════════

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

CREATE SCHEMA IF NOT EXISTS memory;
CREATE SCHEMA IF NOT EXISTS vectors;
CREATE SCHEMA IF NOT EXISTS graph;
CREATE SCHEMA IF NOT EXISTS cache;
CREATE SCHEMA IF NOT EXISTS policies;
CREATE SCHEMA IF NOT EXISTS audit;
CREATE SCHEMA IF NOT EXISTS metrics;
CREATE SCHEMA IF NOT EXISTS meta;

-- ─── meta ───────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS meta.schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    description TEXT
);

INSERT INTO meta.schema_version (version, description)
VALUES (1, 'initial schema')
ON CONFLICT (version) DO NOTHING;

CREATE TABLE IF NOT EXISTS meta.config (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── memory.sessions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title TEXT,
    summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    message_count INTEGER NOT NULL DEFAULT 0,
    tokens_used BIGINT NOT NULL DEFAULT 0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_sessions_started
    ON memory.sessions (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON memory.sessions (started_at DESC) WHERE ended_at IS NULL;

-- ─── memory.messages ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.messages (
    id BIGSERIAL,
    session_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    role TEXT NOT NULL,
    content TEXT,
    tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls JSONB,
    tool_call_id TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

DO $$
DECLARE
    current_month TEXT := to_char(now(), 'YYYY_MM');
    next_month TEXT := to_char(now() + interval '1 month', 'YYYY_MM');
    start_curr DATE := date_trunc('month', now())::date;
    start_next DATE := (date_trunc('month', now()) + interval '1 month')::date;
    start_next2 DATE := (date_trunc('month', now()) + interval '2 month')::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS memory.messages_%s '
        'PARTITION OF memory.messages FOR VALUES FROM (%L) TO (%L)',
        current_month, start_curr, start_next
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS memory.messages_%s '
        'PARTITION OF memory.messages FOR VALUES FROM (%L) TO (%L)',
        next_month, start_next, start_next2
    );
END $$;

CREATE INDEX IF NOT EXISTS idx_messages_session
    ON memory.messages (session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_created
    ON memory.messages USING BRIN (created_at);

-- ─── memory.events ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.events (
    id BIGSERIAL,
    session_id UUID,
    trace_id TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    type TEXT NOT NULL,
    agent TEXT,
    summary TEXT,
    details JSONB,
    success BOOLEAN NOT NULL DEFAULT true,
    importance REAL,
    topic TEXT,
    tags TEXT[],
    enriched_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

DO $$
DECLARE
    current_month TEXT := to_char(now(), 'YYYY_MM');
    next_month TEXT := to_char(now() + interval '1 month', 'YYYY_MM');
    start_curr DATE := date_trunc('month', now())::date;
    start_next DATE := (date_trunc('month', now()) + interval '1 month')::date;
    start_next2 DATE := (date_trunc('month', now()) + interval '2 month')::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS memory.events_%s '
        'PARTITION OF memory.events FOR VALUES FROM (%L) TO (%L)',
        current_month, start_curr, start_next
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS memory.events_%s '
        'PARTITION OF memory.events FOR VALUES FROM (%L) TO (%L)',
        next_month, start_next, start_next2
    );
END $$;

CREATE INDEX IF NOT EXISTS idx_events_session
    ON memory.events (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON memory.events (type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_unenriched
    ON memory.events (created_at) WHERE enriched_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_importance
    ON memory.events (importance DESC) WHERE importance IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_tags
    ON memory.events USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_events_summary_trgm
    ON memory.events USING GIN (summary gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_events_created_brin
    ON memory.events USING BRIN (created_at);

-- ─── memory.summaries ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS memory.summaries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    covers_from TIMESTAMPTZ NOT NULL,
    covers_to TIMESTAMPTZ NOT NULL,
    importance REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_summaries_scope
    ON memory.summaries (scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_session
    ON memory.summaries (session_id);
CREATE INDEX IF NOT EXISTS idx_summaries_content_trgm
    ON memory.summaries USING GIN (content gin_trgm_ops);

-- ─── vectors.chunks ─────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS vectors.chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file TEXT NOT NULL,
    content TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    language TEXT,
    symbols TEXT[],
    hash TEXT NOT NULL,
    embedding vector(1024),
    embedding_half halfvec(1024),
    embedding_bin bit(1024),
    importance REAL,
    topic TEXT,
    tags TEXT[],
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chunks_half_hnsw
    ON vectors.chunks USING hnsw (embedding_half halfvec_cosine_ops)
    WITH (m = 16, ef_construction = 64);

CREATE INDEX IF NOT EXISTS idx_chunks_full_ivf
    ON vectors.chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);

CREATE INDEX IF NOT EXISTS idx_chunks_file ON vectors.chunks (file);
CREATE INDEX IF NOT EXISTS idx_chunks_hash ON vectors.chunks (hash);
CREATE INDEX IF NOT EXISTS idx_chunks_language ON vectors.chunks (language);
CREATE INDEX IF NOT EXISTS idx_chunks_importance
    ON vectors.chunks (importance DESC) WHERE importance IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_chunks_tags
    ON vectors.chunks USING GIN (tags);
CREATE INDEX IF NOT EXISTS idx_chunks_symbols
    ON vectors.chunks USING GIN (symbols);
CREATE INDEX IF NOT EXISTS idx_chunks_content_trgm
    ON vectors.chunks USING GIN (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_chunks_content_tsv
    ON vectors.chunks USING GIN (to_tsvector('russian', content));

-- ─── graph ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS graph.nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    file TEXT,
    line INTEGER,
    importance REAL,
    role TEXT,
    complexity TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON graph.nodes (kind);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON graph.nodes (file);
CREATE INDEX IF NOT EXISTS idx_nodes_name_trgm
    ON graph.nodes USING GIN (name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_nodes_role ON graph.nodes (role);

CREATE TABLE IF NOT EXISTS graph.edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON graph.edges (src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON graph.edges (dst, kind);

-- ─── cache ──────────────────────────────────────────────────────────
CREATE UNLOGGED TABLE IF NOT EXISTS cache.llm_responses (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_cache_expires
    ON cache.llm_responses (expires_at) WHERE expires_at IS NOT NULL;

CREATE UNLOGGED TABLE IF NOT EXISTS cache.extraction (
    file_hash TEXT NOT NULL,
    extractor_id TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    use_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (file_hash, extractor_id, extractor_version)
);

-- ─── policies ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS policies.approval (
    id BIGSERIAL PRIMARY KEY,
    tool TEXT NOT NULL,
    scope TEXT NOT NULL,
    path_pattern TEXT,
    decision TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    use_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_policies_tool ON policies.approval (tool);

-- ─── audit ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit.settings (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    before_data JSONB,
    after_data JSONB,
    details JSONB
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit.settings (ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit.settings (target);

CREATE OR REPLACE FUNCTION audit.forbid_modify() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'Audit table is append-only';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_no_update ON audit.settings;
CREATE TRIGGER trg_audit_no_update
    BEFORE UPDATE OR DELETE ON audit.settings
    FOR EACH ROW EXECUTE FUNCTION audit.forbid_modify();

-- ─── metrics ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS metrics.loop_runs (
    id BIGSERIAL,
    trace_id TEXT NOT NULL,
    loop_id TEXT NOT NULL,
    parent_loop_id TEXT,
    agent_id TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    iterations INTEGER,
    exit_reason TEXT,
    success BOOLEAN,
    tokens_input INTEGER,
    tokens_output INTEGER,
    tool_calls_count INTEGER,
    duration_ms REAL,
    session_id UUID,
    scenario_id TEXT,
    error TEXT,
    PRIMARY KEY (id, started_at)
) PARTITION BY RANGE (started_at);

DO $$
DECLARE
    current_month TEXT := to_char(now(), 'YYYY_MM');
    next_month TEXT := to_char(now() + interval '1 month', 'YYYY_MM');
    start_curr DATE := date_trunc('month', now())::date;
    start_next DATE := (date_trunc('month', now()) + interval '1 month')::date;
    start_next2 DATE := (date_trunc('month', now()) + interval '2 month')::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS metrics.loop_runs_%s '
        'PARTITION OF metrics.loop_runs FOR VALUES FROM (%L) TO (%L)',
        current_month, start_curr, start_next
    );
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS metrics.loop_runs_%s '
        'PARTITION OF metrics.loop_runs FOR VALUES FROM (%L) TO (%L)',
        next_month, start_next, start_next2
    );
END $$;

CREATE INDEX IF NOT EXISTS idx_loop_trace ON metrics.loop_runs (trace_id);
CREATE INDEX IF NOT EXISTS idx_loop_loop_id ON metrics.loop_runs (loop_id);
CREATE INDEX IF NOT EXISTS idx_loop_started_brin
    ON metrics.loop_runs USING BRIN (started_at);

-- ─── NOTIFY trigger ─────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION memory.notify_new_event() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('new_events', NEW.id::text);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_new_event ON memory.events;
CREATE TRIGGER trg_new_event
    AFTER INSERT ON memory.events
    FOR EACH ROW EXECUTE FUNCTION memory.notify_new_event();

-- ─── Автосоздание партиций ──────────────────────────────────────────
CREATE OR REPLACE FUNCTION meta.create_next_partitions()
RETURNS void AS $$
DECLARE
    tables TEXT[] := ARRAY['memory.messages', 'memory.events', 'metrics.loop_runs'];
    t TEXT;
    next_month TEXT;
    start_next DATE;
    start_next2 DATE;
BEGIN
    FOR t IN SELECT unnest(tables) LOOP
        FOR i IN 1..2 LOOP
            next_month := to_char(now() + (i || ' month')::interval, 'YYYY_MM');
            start_next := (date_trunc('month', now()) + (i || ' month')::interval)::date;
            start_next2 := (date_trunc('month', now()) + ((i+1) || ' month')::interval)::date;
            BEGIN
                EXECUTE format(
                    'CREATE TABLE IF NOT EXISTS %s_%s '
                    'PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                    t, next_month, t, start_next, start_next2
                );
            EXCEPTION WHEN duplicate_table THEN NULL;
            END;
        END LOOP;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- ─── Память агентов: семантический индекс прошлых задач ─────────────
-- Наполняет AgentMemoryIndexer (src/db/agent_memory.py) из зеркал
-- ops.chat_messages / ops.plans / journal.events_mirror (Task 14).
-- Базовая таблица БЕЗ векторных колонок — создаётся на любом PG;
-- векторные колонки добавляются ниже и мягко пропускаются без pgvector
-- (тогда поиск идёт по FTS-колонке tsv).
CREATE TABLE IF NOT EXISTS memory.tasks (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL DEFAULT 'note',
    -- chat | plan | event | note — источник записи
    source_key    TEXT NOT NULL DEFAULT '',
    -- event:<uid> | msg:<session>:<seq> | plan:<id> | manual:<hash>
    text          TEXT NOT NULL,
    content_hash  TEXT NOT NULL DEFAULT '',
    session_id    TEXT NOT NULL DEFAULT '',
    plan_id       TEXT NOT NULL DEFAULT '',
    tsv           tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED,
    meta          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, source_key)
);

CREATE INDEX IF NOT EXISTS idx_tasks_kind_created
    ON memory.tasks (kind, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tasks_session
    ON memory.tasks (session_id) WHERE session_id <> '';
CREATE INDEX IF NOT EXISTS idx_tasks_tsv
    ON memory.tasks USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_tasks_meta
    ON memory.tasks USING GIN (meta);

-- Векторные колонки (требуют расширение pgvector; без него эти два
-- statements мягко пропускаются init_db, остальное работает):
ALTER TABLE memory.tasks ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE memory.tasks ADD COLUMN IF NOT EXISTS embedding_half halfvec(1024);

-- HNSW-индекс по halfvec-колонке. Обёрнут в DO-блок: создаётся только
-- если колонка embedding_half реально существует (новый pgvector);
-- старый pgvector (без halfvec) или его отсутствие — молча пропускают
-- индекс, остальная схема и FTS-поиск продолжают работать.
DO $mem_hnsw$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'memory' AND table_name = 'tasks'
          AND column_name = 'embedding_half'
    ) THEN
        BEGIN
            CREATE INDEX IF NOT EXISTS idx_tasks_half_hnsw
                ON memory.tasks
                USING hnsw (embedding_half halfvec_cosine_ops);
        EXCEPTION WHEN OTHERS THEN
            -- очень старый pgvector: колонка есть, hnsw-доступа нет
            NULL;
        END;
    END IF;
END
$mem_hnsw$;

-- ─── Права ──────────────────────────────────────────────────────────
GRANT USAGE ON SCHEMA memory, vectors, graph, cache, policies, audit, metrics, meta TO llmagent;
GRANT ALL ON ALL TABLES IN SCHEMA memory, vectors, graph, cache, policies, audit, metrics, meta TO llmagent;
GRANT ALL ON ALL SEQUENCES IN SCHEMA memory, vectors, graph, cache, policies, audit, metrics, meta TO llmagent;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA memory, vectors, graph, cache, policies, audit, metrics, meta TO llmagent;

-- ═══════════════════════════════════════════════════════════════════════
-- Journal: event-sourcing, snapshots, dependencies, rollback
-- ═══════════════════════════════════════════════════════════════════════

CREATE SCHEMA IF NOT EXISTS journal;

-- ─── Actions (события) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.actions (
    id UUID DEFAULT gen_random_uuid(),   -- PK составной: (id, created_at)
    trace_id TEXT,
    session_id UUID,
    parent_id UUID,                      -- parent action (цепочка)
    agent TEXT NOT NULL,
    tool TEXT NOT NULL,
    args JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_summary TEXT,
    result_hash TEXT,                    -- sha256(результата)
    success BOOLEAN NOT NULL DEFAULT true,
    error TEXT,
    duration_ms REAL,

    -- Классификация
    category TEXT NOT NULL DEFAULT 'read',
        -- read | write | patch | delete | move | query | external | admin
    importance REAL,
    reversible BOOLEAN NOT NULL DEFAULT false,

    -- Для отката
    inverse_op JSONB,                    -- инструкция отката
    affects_files TEXT[],                -- какие файлы затронуты
    affects_rows JSONB,                  -- {table: {before: N, after: M}}

    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- PK обязан включать ключ партиционирования (created_at)
    PRIMARY KEY (id, created_at)
) PARTITION BY RANGE (created_at);

-- Партиции на текущий и следующий месяц
DO $$
DECLARE
    current_month TEXT := to_char(now(), 'YYYY_MM');
    next_month TEXT := to_char(now() + interval '1 month', 'YYYY_MM');
    start_curr DATE := date_trunc('month', now())::date;
    start_next DATE := (date_trunc('month', now()) + interval '1 month')::date;
    start_next2 DATE := (date_trunc('month', now()) + interval '2 month')::date;
BEGIN
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS journal.actions_%s '
        'PARTITION OF journal.actions FOR VALUES FROM (%L) TO (%L)',
        current_month, start_curr, start_next);
    EXECUTE format(
        'CREATE TABLE IF NOT EXISTS journal.actions_%s '
        'PARTITION OF journal.actions FOR VALUES FROM (%L) TO (%L)',
        next_month, start_next, start_next2);
END $$;

CREATE INDEX IF NOT EXISTS idx_jactions_session
    ON journal.actions (session_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jactions_trace
    ON journal.actions (trace_id);
CREATE INDEX IF NOT EXISTS idx_jactions_agent
    ON journal.actions (agent, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jactions_tool
    ON journal.actions (tool, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jactions_category
    ON journal.actions (category, created_at DESC)
    WHERE category != 'read';
CREATE INDEX IF NOT EXISTS idx_jactions_parent
    ON journal.actions (parent_id)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jactions_files
    ON journal.actions USING GIN (affects_files);
CREATE INDEX IF NOT EXISTS idx_jactions_args
    ON journal.actions USING GIN (args);
CREATE INDEX IF NOT EXISTS idx_jactions_created_brin
    ON journal.actions USING BRIN (created_at);

-- ─── Snapshots (before/after содержимое) ──────────────────────────────
CREATE TABLE IF NOT EXISTS journal.snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    action_id UUID NOT NULL,
    file_path TEXT NOT NULL,
    phase TEXT NOT NULL,                 -- 'before' | 'after'
    hash TEXT NOT NULL,                  -- sha256 содержимого
    size_bytes BIGINT,
    mode INTEGER,                        -- unix permissions
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jsnap_action
    ON journal.snapshots (action_id, phase);
CREATE INDEX IF NOT EXISTS idx_jsnap_file
    ON journal.snapshots (file_path, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jsnap_hash
    ON journal.snapshots (hash);

-- ─── Blobs (content-addressed storage) ────────────────────────────────
CREATE UNLOGGED TABLE IF NOT EXISTS journal.blobs (
    hash TEXT PRIMARY KEY,
    compression TEXT NOT NULL DEFAULT 'zstd',
    content BYTEA NOT NULL,
    size_raw BIGINT NOT NULL,
    size_compressed BIGINT NOT NULL,
    ref_count INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jblobs_refs
    ON journal.blobs (ref_count);
CREATE INDEX IF NOT EXISTS idx_jblobs_used
    ON journal.blobs (last_used_at);

-- ─── Dependencies (граф) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.dependencies (
    src UUID NOT NULL,
    dst UUID NOT NULL,
    kind TEXT NOT NULL DEFAULT 'caused_by',
        -- caused_by | depends_on | conflicts_with | supersedes
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (src, dst, kind)
);

CREATE INDEX IF NOT EXISTS idx_jdeps_src ON journal.dependencies (src, kind);
CREATE INDEX IF NOT EXISTS idx_jdeps_dst ON journal.dependencies (dst, kind);

-- ─── Checkpoints (точки отката) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.checkpoints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label TEXT,
    action_id UUID,                      -- последнее действие на момент чекпоинта
    files_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
        -- {file_path: hash}
    reason TEXT,                         -- manual | auto | before_rollback | before_replay
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jcheckpoints_created
    ON journal.checkpoints (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jcheckpoints_label
    ON journal.checkpoints (label);

-- ─── Retention (метрики диска) ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.retention_stats (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    disk_total_gb REAL,
    disk_free_gb REAL,
    disk_used_percent REAL,
    blob_count INTEGER,
    blob_size_gb REAL,
    actions_count BIGINT,
    oldest_action_at TIMESTAMPTZ
);

-- ─── Replays (попытки воспроизведения) ────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.replays (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_action UUID,
    to_action UUID,
    checkpoint_id UUID,                  -- стартовый чекпоинт
    status TEXT NOT NULL DEFAULT 'running',
        -- running | success | failed | cancelled
    actions_replayed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    log JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- ─── Rollback logs ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS journal.rollbacks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_action UUID NOT NULL,
    checkpoint_id UUID,                  -- чекпоинт, к которому откатываемся
    actions_reverted INTEGER DEFAULT 0,
    files_restored INTEGER DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
        -- running | success | failed
    dry_run BOOLEAN NOT NULL DEFAULT false,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at TIMESTAMPTZ,
    log JSONB NOT NULL DEFAULT '[]'::jsonb
);

-- ─── Триггер автосоздания партиций ────────────────────────────────────
CREATE OR REPLACE FUNCTION journal.create_next_partitions()
RETURNS void AS $$
DECLARE
    next_month TEXT;
    start_next DATE;
    start_next2 DATE;
BEGIN
    FOR i IN 1..2 LOOP
        next_month := to_char(now() + (i || ' month')::interval, 'YYYY_MM');
        start_next := (date_trunc('month', now()) + (i || ' month')::interval)::date;
        start_next2 := (date_trunc('month', now()) + ((i+1) || ' month')::interval)::date;
        BEGIN
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS journal.actions_%s '
                'PARTITION OF journal.actions '
                'FOR VALUES FROM (%L) TO (%L)',
                next_month, start_next, start_next2);
        EXCEPTION WHEN duplicate_table THEN NULL;
        END;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- ─── Права ────────────────────────────────────────────────────────────
GRANT USAGE ON SCHEMA journal TO llmagent;
GRANT ALL ON ALL TABLES IN SCHEMA journal TO llmagent;
GRANT ALL ON ALL SEQUENCES IN SCHEMA journal TO llmagent;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA journal TO llmagent;

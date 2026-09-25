-- ═══════════════════════════════════════════════════════════════════════
-- Аналитические materialized views
-- ═══════════════════════════════════════════════════════════════════════

-- ─── Daily activity ─────────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_daily_activity CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_daily_activity AS
SELECT
    date_trunc('day', created_at)::date AS day,
    COUNT(*) AS events_count,
    COUNT(DISTINCT session_id) AS sessions_count,
    COUNT(*) FILTER (WHERE type = 'error') AS errors_count,
    COUNT(*) FILTER (WHERE type = 'decision') AS decisions_count,
    COUNT(*) FILTER (WHERE type = 'fix') AS fixes_count,
    COUNT(*) FILTER (WHERE type = 'tool_call') AS tool_calls_count,
    AVG(importance) AS avg_importance,
    COUNT(*) FILTER (WHERE importance >= 0.7) AS important_count,
    COUNT(*) FILTER (WHERE enriched_at IS NOT NULL) AS enriched_count
FROM memory.events
WHERE created_at > now() - interval '180 days'
GROUP BY 1
ORDER BY 1 DESC;

CREATE UNIQUE INDEX idx_mv_daily_activity_day
    ON metrics.mv_daily_activity (day);

-- ─── Top files touched ──────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_top_files CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_top_files AS
SELECT
    file,
    COUNT(*) AS chunk_count,
    AVG(importance) AS avg_importance,
    COUNT(DISTINCT language) AS languages_count,
    MAX(updated_at) AS last_updated,
    MIN(updated_at) AS first_seen
FROM vectors.chunks
GROUP BY file
ORDER BY chunk_count DESC
LIMIT 200;

CREATE UNIQUE INDEX idx_mv_top_files_file
    ON metrics.mv_top_files (file);

-- ─── Agent performance ──────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_agent_performance CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_agent_performance AS
SELECT
    agent_id AS agent,
    COUNT(*) AS runs,
    COUNT(*) FILTER (WHERE success) AS successes,
    COUNT(*) FILTER (WHERE NOT success) AS failures,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE success)
        / NULLIF(COUNT(*), 0),
        2
    ) AS success_rate,
    ROUND(AVG(duration_ms)::numeric, 0) AS avg_duration_ms,
    ROUND(
        PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY duration_ms)::numeric,
        0
    ) AS p95_duration_ms,
    ROUND(AVG(iterations)::numeric, 1) AS avg_iterations,
    SUM(tokens_input + COALESCE(tokens_output, 0)) AS total_tokens,
    MAX(started_at) AS last_run,
    COUNT(DISTINCT exit_reason) AS exit_reasons
FROM metrics.loop_runs
WHERE started_at > now() - interval '30 days'
GROUP BY agent_id
ORDER BY runs DESC;

CREATE UNIQUE INDEX idx_mv_agent_perf_agent
    ON metrics.mv_agent_performance (agent);

-- ─── Topic distribution ─────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_topics CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_topics AS
SELECT
    e.topic,
    COUNT(*) AS events_count,
    AVG(e.importance) AS avg_importance,
    COUNT(DISTINCT e.session_id) AS sessions_count,
    MAX(e.created_at) AS last_seen,
    ARRAY(
        SELECT DISTINCT t
        FROM memory.events e2
        CROSS JOIN UNNEST(e2.tags) AS t
        WHERE e2.topic = e.topic
          AND e2.created_at > now() - interval '30 days'
          AND e2.tags IS NOT NULL
        LIMIT 20
    ) AS sample_tags
FROM memory.events e
WHERE e.topic IS NOT NULL
  AND e.created_at > now() - interval '30 days'
GROUP BY e.topic
ORDER BY events_count DESC
LIMIT 50;

CREATE UNIQUE INDEX idx_mv_topics_topic
    ON metrics.mv_topics (topic);

-- ─── Tool usage ─────────────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_tool_usage CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_tool_usage AS
SELECT
    details->>'tool' AS tool,
    COUNT(*) AS calls,
    COUNT(*) FILTER (WHERE success) AS successes,
    COUNT(*) FILTER (WHERE NOT success) AS failures,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE success)
        / NULLIF(COUNT(*), 0),
        2
    ) AS success_rate,
    ROUND(AVG((details->>'duration_ms')::numeric), 0) AS avg_duration_ms,
    MAX(created_at) AS last_call
FROM memory.events
WHERE type = 'tool_call'
  AND details->>'tool' IS NOT NULL
  AND created_at > now() - interval '30 days'
GROUP BY 1
HAVING COUNT(*) >= 3
ORDER BY calls DESC
LIMIT 100;

CREATE UNIQUE INDEX idx_mv_tool_usage_tool
    ON metrics.mv_tool_usage (tool);

-- ─── Enrichment stats ───────────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS metrics.mv_enrichment_stats CASCADE;

CREATE MATERIALIZED VIEW metrics.mv_enrichment_stats AS
SELECT
    date_trunc('day', created_at)::date AS day,
    COUNT(*) AS total_events,
    COUNT(*) FILTER (WHERE enriched_at IS NOT NULL) AS enriched,
    COUNT(*) FILTER (WHERE enriched_at IS NULL) AS pending,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE enriched_at IS NOT NULL)
        / NULLIF(COUNT(*), 0),
        2
    ) AS enrichment_rate,
    AVG(importance) FILTER (WHERE importance IS NOT NULL) AS avg_importance
FROM memory.events
WHERE created_at > now() - interval '30 days'
GROUP BY 1
ORDER BY 1 DESC;

CREATE UNIQUE INDEX idx_mv_enrichment_stats_day
    ON metrics.mv_enrichment_stats (day);

-- ═══════════════════════════════════════════════════════════════════════
-- Refresh function
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION metrics.refresh_all()
RETURNS void AS $$
BEGIN
    -- CONCURRENTLY работает только при наличии unique index
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_daily_activity;
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_top_files;
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_agent_performance;
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_topics;
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_tool_usage;
    REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.mv_enrichment_stats;
END;
$$ LANGUAGE plpgsql;

-- ═══════════════════════════════════════════════════════════════════════
-- Retention для MV: чистка старых данных
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION metrics.cleanup_old_mv(days_to_keep INTEGER DEFAULT 180)
RETURNS void AS $$
BEGIN
    -- MV обновляются полностью, поэтому ретеншен работает через
    -- фильтр в самих MV (WHERE created_at > now() - interval)
    -- Здесь только дополнительная очистка бэкапов и логов
    PERFORM pg_notify('mv_cleanup',
        json_build_object('days', days_to_keep)::text);
END;
$$ LANGUAGE plpgsql;

-- Права
GRANT ALL ON ALL TABLES IN SCHEMA metrics TO llmagent;
GRANT ALL ON ALL FUNCTIONS IN SCHEMA metrics TO llmagent;

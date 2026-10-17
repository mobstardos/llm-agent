-- ═══════════════════════════════════════════════════════════════════════
-- CDC: PostgreSQL NOTIFY триггеры
-- ═══════════════════════════════════════════════════════════════════════

-- ─── Session created ────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION memory.notify_session_created()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'session_created',
        json_build_object(
            'id', NEW.id,
            'title', NEW.title,
            'started_at', NEW.started_at
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_session_created ON memory.sessions;
CREATE TRIGGER trg_session_created
    AFTER INSERT ON memory.sessions
    FOR EACH ROW EXECUTE FUNCTION memory.notify_session_created();

-- ─── Session ended ──────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION memory.notify_session_ended()
RETURNS trigger AS $$
BEGIN
    IF OLD.ended_at IS NULL AND NEW.ended_at IS NOT NULL THEN
        PERFORM pg_notify(
            'session_ended',
            json_build_object(
                'id', NEW.id,
                'ended_at', NEW.ended_at,
                'message_count', NEW.message_count,
                'tokens_used', NEW.tokens_used
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_session_ended ON memory.sessions;
CREATE TRIGGER trg_session_ended
    AFTER UPDATE ON memory.sessions
    FOR EACH ROW EXECUTE FUNCTION memory.notify_session_ended();

-- ─── Tool called ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION memory.notify_tool_call()
RETURNS trigger AS $$
BEGIN
    IF NEW.type = 'tool_call' THEN
        PERFORM pg_notify(
            'tool_called',
            json_build_object(
                'id', NEW.id,
                'agent', NEW.agent,
                'summary', NEW.summary,
                'success', NEW.success,
                'created_at', NEW.created_at
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tool_called ON memory.events;
CREATE TRIGGER trg_tool_called
    AFTER INSERT ON memory.events
    FOR EACH ROW EXECUTE FUNCTION memory.notify_tool_call();

-- ─── Important event ────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION memory.notify_important_event()
RETURNS trigger AS $$
BEGIN
    IF NEW.importance IS NOT NULL AND NEW.importance >= 0.7 THEN
        PERFORM pg_notify(
            'important_event',
            json_build_object(
                'id', NEW.id,
                'agent', NEW.agent,
                'summary', NEW.summary,
                'importance', NEW.importance,
                'topic', NEW.topic,
                'tags', NEW.tags
            )::text
        );
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_important_event ON memory.events;
CREATE TRIGGER trg_important_event
    AFTER UPDATE ON memory.events
    FOR EACH ROW
    WHEN (OLD.importance IS DISTINCT FROM NEW.importance)
    EXECUTE FUNCTION memory.notify_important_event();

-- ─── Approval decision ──────────────────────────────────────────────
CREATE OR REPLACE FUNCTION policies.notify_approval()
RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify(
        'approval_decision',
        json_build_object(
            'id', NEW.id,
            'tool', NEW.tool,
            'scope', NEW.scope,
            'decision', NEW.decision,
            'created_at', NEW.created_at
        )::text
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_approval_decision ON policies.approval;
CREATE TRIGGER trg_approval_decision
    AFTER INSERT ON policies.approval
    FOR EACH ROW EXECUTE FUNCTION policies.notify_approval();

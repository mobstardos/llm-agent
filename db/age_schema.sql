-- ═══════════════════════════════════════════════════════════════════════
-- Apache AGE — property graph schema
-- Применять к отдельной AGE-базе (см. docker run с apache/age)
-- ═══════════════════════════════════════════════════════════════════════

-- Проверяем наличие расширения
CREATE EXTENSION IF NOT EXISTS age;

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- Создать граф (идемпотентно)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'llm_graph'
    ) THEN
        PERFORM ag_catalog.create_graph('llm_graph');
    END IF;
END $$;

-- ─── Vertex labels ──────────────────────────────────────────────────
DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'File');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'Function');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'Class');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'Concept');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'Session');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_vlabel('llm_graph', 'Event');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── Edge labels ────────────────────────────────────────────────────
DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'IMPORTS');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'DEFINES');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'CALLS');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'RELATES_TO');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'MENTIONS');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'SOLVES');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'SOLVED_BY');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'DEPENDS_ON');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    PERFORM ag_catalog.create_elabel('llm_graph', 'MODIFIES');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ─── Индексы для быстрого поиска ────────────────────────────────────
DO $$
BEGIN
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_file_path '
        'ON llm_graph."File" ((properties->>''path''))'
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'File index: %', SQLERRM;
END $$;

DO $$
BEGIN
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_function_id '
        'ON llm_graph."Function" ((properties->>''id''))'
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Function index: %', SQLERRM;
END $$;

DO $$
BEGIN
    EXECUTE format(
        'CREATE INDEX IF NOT EXISTS idx_concept_name '
        'ON llm_graph."Concept" ((properties->>''name''))'
    );
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'Concept index: %', SQLERRM;
END $$;

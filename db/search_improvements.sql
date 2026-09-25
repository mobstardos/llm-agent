-- ═══════════════════════════════════════════════════════════════════════
-- Улучшения поиска: синонимы + Snowball + функция meta.search_events
-- ═══════════════════════════════════════════════════════════════════════

-- ─── Словарь синонимов ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS meta.synonyms (
    term TEXT PRIMARY KEY,
    syns TEXT[] NOT NULL,
    category TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO meta.synonyms (term, syns, category) VALUES
    -- Программирование
    ('файл', ARRAY['file', 'документ', 'исходник', 'модуль'], 'file'),
    ('папка', ARRAY['директория', 'folder', 'directory', 'каталог'], 'file'),
    ('ошибка', ARRAY['error', 'exception', 'падение', 'сбой', 'баг'], 'error'),
    ('баг', ARRAY['bug', 'ошибка', 'дефект', 'проблема'], 'error'),
    ('тест', ARRAY['test', 'проверка', 'spec', 'specification'], 'test'),
    ('тесты', ARRAY['tests', 'тестирование', 'проверки'], 'test'),
    ('сборка', ARRAY['build', 'компиляция', 'компилирование'], 'build'),
    ('запуск', ARRAY['run', 'execute', 'start', 'выполнение'], 'action'),
    ('удалить', ARRAY['delete', 'remove', 'drop', 'убрать'], 'action'),
    ('создать', ARRAY['create', 'new', 'add', 'сделать', 'добавить'], 'action'),
    ('изменить', ARRAY['modify', 'change', 'update', 'edit', 'править'], 'action'),
    ('найти', ARRAY['find', 'search', 'locate', 'искать', 'поиск'], 'action'),
    ('прочитать', ARRAY['read', 'open', 'прочесть', 'load'], 'action'),
    ('записать', ARRAY['write', 'save', 'сохранить', 'запись'], 'action'),

    -- Базы данных
    ('база', ARRAY['database', 'db', 'бд'], 'db'),
    ('запрос', ARRAY['query', 'sql', 'statement'], 'db'),
    ('таблица', ARRAY['table', 'relation'], 'db'),
    ('схема', ARRAY['schema', 'structure', 'структура'], 'db'),
    ('миграция', ARRAY['migration', 'alter', 'изменение схемы'], 'db'),
    ('индекс', ARRAY['index', 'idx'], 'db'),
    ('транзакция', ARRAY['transaction', 'tx'], 'db'),

    -- Разработка
    ('решение', ARRAY['decision', 'choice', 'выбор'], 'dev'),
    ('проблема', ARRAY['problem', 'issue', 'task', 'задача'], 'dev'),
    ('функция', ARRAY['function', 'method', 'method', 'fn'], 'dev'),
    ('класс', ARRAY['class', 'type', 'тип'], 'dev'),
    ('модуль', ARRAY['module', 'package', 'пакет'], 'dev'),
    ('код', ARRAY['code', 'source', 'исходник'], 'dev'),

    -- Ошибки
    ('исправить', ARRAY['fix', 'repair', 'починить', 'решить'], 'fix'),
    ('исправление', ARRAY['fix', 'patch', 'правка'], 'fix'),
    ('падение', ARRAY['crash', 'failure', 'fail'], 'error'),
    ('зависание', ARRAY['hang', 'freeze', 'timeout'], 'error'),
    ('утечка', ARRAY['leak', 'memory leak'], 'error'),

    -- 1С
    ('справочник', ARRAY['catalog', 'reference'], '1c'),
    ('документ', ARRAY['document'], '1c'),
    ('регистр', ARRAY['register'], '1c'),
    ('конфигурация', ARRAY['config', 'configuration'], '1c'),
    ('расширение', ARRAY['extension', 'cfe'], '1c'),
    ('обработка', ARRAY['processing', 'epf'], '1c'),
    ('отчет', ARRAY['report'], '1c'),
    ('запрос1с', ARRAY['запрос 1с', '1c query'], '1c'),

    -- Инфраструктура
    ('докер', ARRAY['docker', 'контейнер', 'container'], 'infra'),
    ('кластер', ARRAY['cluster', 'k8s', 'kubernetes'], 'infra'),
    ('поды', ARRAY['pods', 'pod'], 'infra'),
    ('деплой', ARRAY['deploy', 'deployment', 'развертывание'], 'infra'),
    ('логи', ARRAY['logs', 'журнал', 'логирование'], 'infra'),
    ('метрики', ARRAY['metrics', 'статистика'], 'infra'),

    -- AI
    ('модель', ARRAY['model', 'llm'], 'ai'),
    ('промпт', ARRAY['prompt', 'инструкция'], 'ai'),
    ('агент', ARRAY['agent', 'bot'], 'ai'),
    ('токен', ARRAY['token', 'токены'], 'ai')
ON CONFLICT (term) DO UPDATE SET
    syns = EXCLUDED.syns,
    category = EXCLUDED.category,
    updated_at = now();

-- Индекс по категориям
CREATE INDEX IF NOT EXISTS idx_synonyms_category
    ON meta.synonyms (category);

-- ═══════════════════════════════════════════════════════════════════════
-- Функция расширения запроса синонимами
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION meta.expand_query_terms(query_text TEXT)
RETURNS TEXT[] AS $$
DECLARE
    words TEXT[];
    word TEXT;
    syns TEXT[];
    expanded TEXT[] := ARRAY[]::TEXT[];
BEGIN
    -- Разбить на слова, привести к нижнему регистру
    words := regexp_split_to_array(
        lower(regexp_replace(query_text, '[^\w\s]', ' ', 'g')),
        '\s+'
    );

    FOREACH word IN ARRAY words LOOP
        IF length(word) < 2 THEN
            CONTINUE;
        END IF;

        -- Само слово
        expanded := array_append(expanded, word);

        -- Синонимы
        SELECT syns INTO syns FROM meta.synonyms WHERE term = word;
        IF syns IS NOT NULL THEN
            expanded := expanded || syns;
        END IF;
    END LOOP;

    -- Уникальные
    RETURN ARRAY(SELECT DISTINCT unnest(expanded));
END;
$$ LANGUAGE plpgsql STABLE;

-- ═══════════════════════════════════════════════════════════════════════
-- Основная функция поиска событий с синонимами
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION meta.search_events(
    query_text TEXT,
    limit_count INTEGER DEFAULT 50,
    min_importance REAL DEFAULT 0.0,
    days_back INTEGER DEFAULT 30
)
RETURNS TABLE (
    event_id BIGINT,
    summary TEXT,
    topic TEXT,
    tags TEXT[],
    importance REAL,
    created_at TIMESTAMPTZ,
    rank REAL
) AS $$
DECLARE
    terms TEXT[];
    tsquery_text TEXT;
BEGIN
    -- Расширить запрос синонимами
    terms := meta.expand_query_terms(query_text);

    IF array_length(terms, 1) IS NULL OR array_length(terms, 1) = 0 THEN
        RETURN;
    END IF;

    -- Составить tsquery: word1 | word2 | word3:*
    tsquery_text := array_to_string(terms, ' | ');

    RETURN QUERY
    SELECT
        e.id AS event_id,
        e.summary,
        e.topic,
        e.tags,
        e.importance,
        e.created_at,
        ts_rank_cd(
            to_tsvector('russian',
                coalesce(e.summary, '') || ' ' ||
                coalesce(e.topic, '') || ' ' ||
                coalesce(array_to_string(e.tags, ' '), '')
            ),
            to_tsquery('russian', tsquery_text)
        ) AS rank
    FROM memory.events e
    WHERE
        e.created_at > now() - (days_back || ' days')::interval
        AND (min_importance = 0 OR e.importance >= min_importance)
        AND to_tsvector('russian',
                coalesce(e.summary, '') || ' ' ||
                coalesce(e.topic, '') || ' ' ||
                coalesce(array_to_string(e.tags, ' '), '')
            ) @@ to_tsquery('russian', tsquery_text)
    ORDER BY rank DESC, e.importance DESC NULLS LAST
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- ═══════════════════════════════════════════════════════════════════════
-- Fuzzy search по чанкам (pg_trgm)
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION vectors.search_chunks_fuzzy(
    query_text TEXT,
    limit_count INTEGER DEFAULT 20,
    similarity_threshold REAL DEFAULT 0.2
)
RETURNS TABLE (
    id UUID,
    file TEXT,
    content TEXT,
    similarity REAL,
    start_line INTEGER,
    end_line INTEGER
) AS $$
BEGIN
    RETURN QUERY
    SELECT
        c.id,
        c.file,
        c.content,
        similarity(c.content, query_text) AS similarity,
        c.start_line,
        c.end_line
    FROM vectors.chunks c
    WHERE
        c.content % query_text
        AND similarity(c.content, query_text) > similarity_threshold
    ORDER BY similarity DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- ═══════════════════════════════════════════════════════════════════════
-- Гибридный поиск: BM25 + синонимы
-- ═══════════════════════════════════════════════════════════════════════
CREATE OR REPLACE FUNCTION meta.hybrid_search_events(
    query_text TEXT,
    limit_count INTEGER DEFAULT 50
)
RETURNS TABLE (
    event_id BIGINT,
    summary TEXT,
    importance REAL,
    bm25_rank REAL,
    recency_boost REAL,
    combined_score REAL
) AS $$
DECLARE
    terms TEXT[];
    tsquery_text TEXT;
BEGIN
    terms := meta.expand_query_terms(query_text);

    IF array_length(terms, 1) IS NULL THEN
        RETURN;
    END IF;

    tsquery_text := array_to_string(terms, ' | ');

    RETURN QUERY
    SELECT
        e.id,
        e.summary,
        e.importance,
        ts_rank_cd(
            to_tsvector('russian',
                coalesce(e.summary, '') || ' ' ||
                coalesce(e.topic, '') || ' ' ||
                coalesce(array_to_string(e.tags, ' '), '')
            ),
            to_tsquery('russian', tsquery_text)
        ) AS bm25_rank,
        -- Чем свежее, тем лучше
        exp(-extract(epoch from (now() - e.created_at)) / (86400 * 7)) AS recency_boost,
        (
            ts_rank_cd(
                to_tsvector('russian',
                    coalesce(e.summary, '') || ' ' ||
                    coalesce(e.topic, '') || ' ' ||
                    coalesce(array_to_string(e.tags, ' '), '')
                ),
                to_tsquery('russian', tsquery_text)
            ) * 0.6 +
            exp(-extract(epoch from (now() - e.created_at)) / (86400 * 7)) * 0.2 +
            coalesce(e.importance, 0.5) * 0.2
        ) AS combined_score
    FROM memory.events e
    WHERE
        e.created_at > now() - interval '30 days'
        AND to_tsvector('russian',
                coalesce(e.summary, '') || ' ' ||
                coalesce(e.topic, '') || ' ' ||
                coalesce(array_to_string(e.tags, ' '), '')
            ) @@ to_tsquery('russian', tsquery_text)
    ORDER BY combined_score DESC
    LIMIT limit_count;
END;
$$ LANGUAGE plpgsql STABLE;

-- Права
GRANT ALL ON meta.synonyms TO llmagent;
GRANT EXECUTE ON FUNCTION meta.expand_query_terms TO llmagent;
GRANT EXECUTE ON FUNCTION meta.search_events TO llmagent;
GRANT EXECUTE ON FUNCTION meta.hybrid_search_events TO llmagent;
GRANT EXECUTE ON FUNCTION vectors.search_chunks_fuzzy TO llmagent;

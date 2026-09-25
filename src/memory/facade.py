"""Фасад памяти — единая точка входа.

Поддерживает три стратегии хранения:
- PostgreSQL + pgvector (основная, Часть 1)
- LanceDB (fallback, dual-write)
- SQLite (для episodic, fallback)

Стратегия выбирается через .env: PRIMARY_VECTOR_STORE и др.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from src.config import get_settings
from src.memory.config import MemoryConfig, load_memory_config
from src.memory.episodic import EpisodicMemory
from src.memory.graph import KnowledgeGraph
from src.memory.procedural import ProceduralMemory
from src.memory.retriever import Retriever
from src.memory.semantic import ProjectProfile
from src.memory.summarizer import Summarizer
from src.memory.user import UserProfile
from src.memory.vector import VectorMemory
from src.memory.working import WorkingMemory

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class Memory:
    """Единый фасад памяти.

    Инициализация lazy: PostgreSQL подключается в `await memory.initialize()`.
    LanceDB и SQLite инициализируются сразу (если включены).
    """

    def __init__(
        self,
        llm_client: Any | None = None,
        config: MemoryConfig | None = None,
        base_dir: Path | None = None,
    ):
        self.cfg = config or load_memory_config()
        self.base_dir = base_dir or BASE_DIR
        self.enabled = self.cfg.enabled

        # LLM для summary
        self._llm = llm_client
        self.summarizer = Summarizer(llm_client) if llm_client else None

        # Настройки
        self.settings = get_settings()
        self.use_postgres = self.settings.use_postgres
        self.use_lancedb = self.settings.use_lancedb
        self.use_sqlite_memory = self.settings.use_sqlite_memory
        self.use_sqlite_graph = self.settings.use_sqlite_graph
        self.dual_write = self.settings.storage_strategy.dual_write

        # ─── Working ────────────────────────────────────
        self.working = WorkingMemory(self.cfg.working)

        # ─── PostgreSQL (async) ─────────────────────────
        self.pg_pool = None
        self.pg_vector_store = None
        self.pg_memory_store = None
        self.pg_graph_store = None
        self.pg_hybrid = None
        self.pg_age = None
        self.pg_search = None

        if self.use_postgres:
            self._init_postgres()

        # ─── Episodic (SQLite) ──────────────────────────
        self.episodic: EpisodicMemory | None = None
        if self.use_sqlite_memory:
            self.episodic = EpisodicMemory(
                self.cfg.episodic,
                db_path=str(self.base_dir / self.cfg.episodic.db_path),
                summarizer=self.summarizer,
            )

        # ─── Semantic ───────────────────────────────────
        self.profile = ProjectProfile(self.cfg.semantic)

        # ─── Vector (LanceDB) ───────────────────────────
        self.vector: VectorMemory | None = None
        self.retriever: Retriever | None = None
        if self.use_lancedb and self.cfg.vector.enabled:
            try:
                self.vector = VectorMemory(self.cfg.vector, self.base_dir)
                self.retriever = Retriever(self.vector)
            except Exception as e:
                logger.warning("LanceDB недоступна: %s", e)

        # ─── Procedural ─────────────────────────────────
        self.procedural = ProceduralMemory(
            self.cfg.procedural, self.base_dir,
        )

        # ─── User profile ───────────────────────────────
        self.user = UserProfile(self.cfg.user, self.base_dir)

        # ─── Graph (SQLite) ─────────────────────────────
        self.graph: KnowledgeGraph | None = None
        if self.use_sqlite_graph and self.cfg.graph.enabled:
            try:
                self.graph = KnowledgeGraph(self.cfg.graph, self.base_dir)
            except Exception as e:
                logger.warning("SQLite graph недоступен: %s", e)

    # ═══════════════════════════════════════════════════════
    # Init PostgreSQL
    # ═══════════════════════════════════════════════════════
    def _init_postgres(self) -> None:
        try:
            from src.db.pool import DatabasePool
            from src.db.vector_store import VectorStore
            from src.db.memory_store import MemoryStore
            from src.db.graph_store import GraphStore
            from src.db.hybrid_search import HybridSearch
            from src.db.search_helpers import SearchHelpers

            self.pg_pool = DatabasePool(
                self.settings.postgres.dsn(),
                min_size=self.settings.postgres.pool_min,
                max_size=self.settings.postgres.pool_max,
            )
            self.pg_vector_store = VectorStore(self.pg_pool)
            self.pg_memory_store = MemoryStore(self.pg_pool)
            self.pg_graph_store = GraphStore(self.pg_pool)
            self.pg_hybrid = HybridSearch(self.pg_pool)
            self.pg_search = SearchHelpers(self.pg_pool)

            # AGE (опционально)
            if self.settings.age.enabled:
                try:
                    from src.db.age_store import AgeStore
                    self.pg_age = AgeStore(
                        self.settings.age.dsn,
                        graph_name=self.settings.age.graph,
                    )
                except Exception as e:
                    logger.warning("AGE init failed: %s", e)
                    self.pg_age = None

        except Exception as e:
            logger.exception("PostgreSQL init failed: %s", e)
            self.use_postgres = False

    async def initialize(self) -> None:
        """Асинхронная инициализация (pool.start + AGE.start).

        PostgreSQL опционален: если база недоступна — мягко переходим
        на SQLite + LanceDB, не блокируя запуск сервера.
        """
        if self.pg_pool is not None and self.pg_pool._pool is None:
            try:
                await asyncio.wait_for(self.pg_pool.start(), timeout=8.0)
                logger.info("PostgreSQL pool initialized")
            except Exception as e:
                # Ленивый импорт: psycopg не обязателен для фасада
                from src.db.pool import _fmt_exc, dsn_view
                logger.warning(
                    "PostgreSQL недоступен (%s) — работаю на SQLite + LanceDB "
                    "(DSN: %s; включить — .env DATABASE_URL / PG_APP_*, "
                    "шум в логе убрать — PG_REPLICATE=0)",
                    _fmt_exc(e)[:300], dsn_view(self.settings.postgres.dsn()),
                )
                self.use_postgres = False
                self.pg_pool = None
                self.pg_vector_store = None
                self.pg_memory_store = None
                self.pg_graph_store = None
                self.pg_hybrid = None
                self.pg_search = None
                self.pg_age = None

        if self.pg_age is not None:
            ok = await self.pg_age.start()
            if not ok:
                logger.warning("AGE not available, disabling")
                self.pg_age = None

    async def close(self) -> None:
        """Закрыть все соединения."""
        if self.pg_pool is not None:
            try:
                await self.pg_pool.stop()
            except Exception as e:
                logger.warning("Pool close: %s", e)

        if self.pg_age is not None:
            try:
                await self.pg_age.stop()
            except Exception as e:
                logger.warning("AGE close: %s", e)

    # ═══════════════════════════════════════════════════════
    # Working memory
    # ═══════════════════════════════════════════════════════
    def build_context(
        self,
        system_prompt: str,
        query: str,
        project_root: str = "",
    ) -> dict[str, Any]:
        """Собрать контекст для LLM."""
        if not self.enabled:
            return {
                "messages": [{"role": "system", "content": system_prompt}],
                "stats": {},
            }

        project_profile = self.profile.render_for_prompt()
        user_profile = self.user.render_for_prompt()

        recent = []
        if self.episodic:
            try:
                recent = self.episodic.get_recent_messages(limit=100)
            except Exception as e:
                logger.debug("Recent messages: %s", e)

        rolling = ""
        if self.episodic:
            try:
                rolling = self.episodic.rolling_summary()
            except Exception as e:
                logger.debug("Rolling summary: %s", e)

        recalled: list[str] = []
        if self.retriever and query:
            try:
                chunks = self.retriever.retrieve(query, top_k=5)
                if chunks:
                    text = self.retriever.render_context(
                        chunks, max_chars=8000,
                    )
                    if text:
                        recalled.append(text)
            except Exception as e:
                logger.debug("Retriever: %s", e)

        return self.working.build_context(
            system_prompt=system_prompt,
            project_profile=project_profile,
            user_profile=user_profile,
            recent_messages=recent,
            rolling_summary=rolling,
            recalled_chunks=recalled,
        )

    # ═══════════════════════════════════════════════════════
    # Episodic
    # ═══════════════════════════════════════════════════════
    def start_session(self, title: str | None = None) -> str:
        """Начать новую сессию (sync API для совместимости)."""
        if self.episodic:
            return self.episodic.start_session(title).id
        return ""

    async def start_session_async(self, title: str | None = None) -> str:
        """Начать сессию с записью в PostgreSQL."""
        sid = ""
        if self.pg_memory_store:
            sid = await self.pg_memory_store.create_session(title)

        if self.episodic:
            # Дублируем в SQLite (dual-write)
            try:
                old_sid = self.episodic.start_session(title).id
                if not sid:
                    sid = old_sid
            except Exception:
                pass

        return sid

    def record_user_message(self, content: str, tokens: int = 0) -> None:
        if self.episodic:
            self.episodic.add_message("user", content, tokens=tokens)

    def record_assistant_message(
        self,
        content: str,
        tool_calls: list | None = None,
        tokens: int = 0,
    ) -> None:
        if self.episodic:
            self.episodic.add_message(
                "assistant", content or "",
                tool_calls=tool_calls, tokens=tokens,
            )

    def record_tool_result(self, tool_call_id: str, content: str) -> None:
        if self.episodic:
            self.episodic.add_message(
                "tool", content, tool_call_id=tool_call_id,
            )

    def log_event(self, type_: str, summary: str, **kwargs) -> None:
        if self.episodic:
            self.episodic.log_event(type_, summary, **kwargs)

    async def log_event_async(
        self,
        type_: str,
        summary: str,
        session_id: str | None = None,
        trace_id: str | None = None,
        agent: str | None = None,
        details: dict | None = None,
        success: bool = True,
    ) -> int:
        """Записать событие в PostgreSQL (и SQLite при dual-write)."""
        event_id = 0
        if self.pg_memory_store:
            try:
                event_id = await self.pg_memory_store.add_event(
                    type_=type_, summary=summary,
                    session_id=session_id or None,
                    trace_id=trace_id, agent=agent,
                    details=details, success=success,
                )
            except Exception as e:
                logger.warning("PG add_event: %s", e)

        if self.episodic and (self.dual_write or not self.pg_memory_store):
            try:
                self.episodic.log_event(
                    type_, summary, agent=agent,
                    trace_id=trace_id, details=details,
                    success=success,
                )
            except Exception as e:
                logger.debug("SQLite log_event: %s", e)

        return event_id

    async def end_session(self, model: str | None = None) -> str:
        if not self.enabled:
            return ""
        if self.episodic:
            return await self.episodic.end_session(model=model)
        return ""

    # ═══════════════════════════════════════════════════════
    # Semantic
    # ═══════════════════════════════════════════════════════
    def refresh_profile(self, project_root: str) -> dict:
        if not self.enabled:
            return {}
        return self.profile.generate_and_save(project_root)

    # ═══════════════════════════════════════════════════════
    # Vector / Index
    # ═══════════════════════════════════════════════════════
    def index_project(self, project_root: str) -> dict:
        """Sync — использует LanceDB (для совместимости)."""
        if not self.enabled or not self.vector:
            return {"enabled": False, "reason": "LanceDB not enabled"}
        return self.vector.index_project(project_root)

    async def index_project_async(self, project_root: str) -> dict:
        """Async — использует PostgreSQL или LanceDB или оба."""
        results: dict = {
            "postgres": None,
            "lancedb": None,
            "files_scanned": 0,
            "chunks_indexed": 0,
        }

        if not self.enabled:
            return results

        # PostgreSQL
        if self.pg_vector_store is not None:
            try:
                pg_result = await self._index_to_postgres(project_root)
                results["postgres"] = pg_result
                results["files_scanned"] = pg_result.get("files", 0)
                results["chunks_indexed"] = pg_result.get("chunks", 0)
            except Exception as e:
                logger.exception("Index to postgres failed: %s", e)
                results["postgres"] = {"error": str(e)}

        # LanceDB (если dual или primary)
        if self.vector and (self.dual_write or not self.use_postgres):
            try:
                loop = asyncio.get_event_loop()
                lb_result = await loop.run_in_executor(
                    None, self.vector.index_project, project_root,
                )
                results["lancedb"] = lb_result
                if not self.pg_vector_store:
                    results["files_scanned"] = lb_result.get("scanned", 0)
                    results["chunks_indexed"] = lb_result.get("chunks", 0)
            except Exception as e:
                logger.exception("Index to LanceDB failed: %s", e)
                results["lancedb"] = {"error": str(e)}

        return results

    async def _index_to_postgres(self, project_root: str) -> dict:
        """Индексация проекта в PostgreSQL."""
        from fnmatch import fnmatch
        from pathlib import Path

        from src.memory.chunker import Chunker
        from src.memory.embedder import Embedder

        chunker = Chunker(self.cfg.vector.chunking)
        embedder = Embedder(self.cfg.vector.embedder)
        root = Path(project_root)

        file_patterns = self.cfg.vector.indexing.file_patterns
        ignore_patterns = self.cfg.vector.indexing.ignore_patterns
        max_size_kb = self.cfg.vector.indexing.max_file_size_kb

        stats = {"files": 0, "chunks": 0, "skipped": 0, "errors": 0}

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")

            # Фильтры
            if not any(fnmatch(rel, p) for p in file_patterns):
                continue
            if any(fnmatch(rel, p) for p in ignore_patterns):
                stats["skipped"] += 1
                continue

            try:
                size_kb = path.stat().st_size / 1024
                if size_kb > max_size_kb:
                    stats["skipped"] += 1
                    continue
                content = path.read_text(
                    encoding="utf-8", errors="replace",
                )
            except Exception:
                stats["errors"] += 1
                continue

            chunks = chunker.chunk_file(path, content)
            if not chunks:
                continue

            try:
                texts = [c.text for c in chunks]
                vectors = embedder.embed(texts)
                payload = []
                for c, v in zip(chunks, vectors):
                    payload.append({
                        "content": c.text,
                        "embedding": v,
                        "start_line": c.start_line,
                        "end_line": c.end_line,
                        "language": path.suffix.lstrip(".").lower(),
                        "symbols": c.symbols,
                    })

                n = await self.pg_vector_store.replace_file_chunks(
                    rel, payload,
                )
                stats["files"] += 1
                stats["chunks"] += n
            except Exception as e:
                logger.debug("Index file %s: %s", rel, e)
                stats["errors"] += 1

        logger.info(
            "Postgres index: %d files, %d chunks (skipped %d, errors %d)",
            stats["files"], stats["chunks"],
            stats["skipped"], stats["errors"],
        )
        return stats

    # ═══════════════════════════════════════════════════════
    # Graph
    # ═══════════════════════════════════════════════════════
    def rebuild_graph(self, project_root: str) -> dict:
        if not self.enabled or not self.graph:
            return {"enabled": False}
        return self.graph.index_project(project_root)

    async def rebuild_graph_async(self, project_root: str) -> dict:
        """Построить граф в PostgreSQL (и SQLite при dual-write)."""
        results: dict = {"postgres": None, "sqlite": None}

        if self.pg_graph_store:
            try:
                results["postgres"] = await self._rebuild_graph_pg(
                    project_root,
                )
            except Exception as e:
                logger.exception("Graph PG: %s", e)
                results["postgres"] = {"error": str(e)}

        if self.graph and (self.dual_write or not self.use_sqlite_graph):
            try:
                loop = asyncio.get_event_loop()
                results["sqlite"] = await loop.run_in_executor(
                    None, self.graph.index_project, project_root,
                )
            except Exception as e:
                results["sqlite"] = {"error": str(e)}

        return results

    async def _rebuild_graph_pg(self, project_root: str) -> dict:
        """Build graph in PostgreSQL using graph_parsers."""
        from pathlib import Path

        from src.memory.graph_parsers import ParserRegistry

        root = Path(project_root)
        registry = ParserRegistry()
        supported = set(registry.supported_extensions())

        ignore = {".git", ".venv", "venv", "__pycache__", "node_modules",
                  ".idea", ".vscode", "dist", "build", "target", "data"}

        stats = {"files": 0, "nodes": 0, "edges": 0, "errors": 0}

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in ignore for p in path.parts):
                continue
            if path.suffix.lower() not in supported:
                continue

            parser = registry.get_for(path)
            if parser is None:
                continue

            try:
                source = path.read_text(
                    encoding="utf-8", errors="replace",
                )
                rel = str(path.relative_to(root)).replace("\\", "/")
                nodes, edges = parser.parse(path, source)

                for n in nodes:
                    await self.pg_graph_store.upsert_node(
                        id_=n.id, kind=n.kind, name=n.name,
                        file=n.file, line=n.line,
                    )
                for e in edges:
                    await self.pg_graph_store.upsert_edge(
                        src=e.src, dst=e.dst, kind=e.kind,
                    )
                stats["files"] += 1
                stats["nodes"] += len(nodes)
                stats["edges"] += len(edges)
            except Exception as e:
                logger.debug("Graph parse %s: %s", path, e)
                stats["errors"] += 1

        logger.info(
            "Graph: %d files, %d nodes, %d edges",
            stats["files"], stats["nodes"], stats["edges"],
        )
        return stats

    # ═══════════════════════════════════════════════════════
    # Search
    # ═══════════════════════════════════════════════════════
    async def search_vectors_async(
        self, query: str, top_k: int = 10,
    ) -> list[dict]:
        """Гибридный поиск по чанкам (PostgreSQL)."""
        if not self.pg_hybrid:
            return []

        from src.memory.embedder import Embedder
        embedder = Embedder(self.cfg.vector.embedder)
        embedding = embedder.embed_one(query)

        try:
            results = await self.pg_hybrid.search(
                query, embedding, top_k=top_k,
            )
            return [
                {
                    "file": r.file,
                    "content": r.content,
                    "score": r.combined_score,
                    "start_line": r.start_line,
                    "end_line": r.end_line,
                    "language": r.language,
                    "symbols": r.symbols,
                }
                for r in results
            ]
        except Exception as e:
            logger.exception("Search vectors: %s", e)
            return []

    async def search_events_async(
        self,
        query: str,
        limit: int = 50,
        min_importance: float = 0.0,
        days_back: int = 30,
    ) -> list[dict]:
        """Поиск событий с синонимами."""
        if not self.pg_search:
            return []
        try:
            return await self.pg_search.search_events(
                query, limit=limit,
                min_importance=min_importance,
                days_back=days_back,
            )
        except Exception as e:
            logger.warning("Search events: %s", e)
            return []

    # ═══════════════════════════════════════════════════════
    # Procedural
    # ═══════════════════════════════════════════════════════
    def find_procedures(self, problem: str) -> list:
        if not self.enabled:
            return []
        return self.procedural.find(problem)

    # ═══════════════════════════════════════════════════════
    # User
    # ═══════════════════════════════════════════════════════
    def record_approval(self, tool: str, path: str | None = None) -> None:
        if self.enabled:
            self.user.record_approval(tool, path)

    def record_rejection(self, tool: str, path: str | None = None) -> None:
        if self.enabled:
            self.user.record_rejection(tool, path)

    # ═══════════════════════════════════════════════════════
    # Cleanup
    # ═══════════════════════════════════════════════════════
    def cleanup(self) -> dict:
        result: dict = {}
        if self.episodic:
            try:
                result["sqlite"] = self.episodic.cleanup()
            except Exception as e:
                result["sqlite"] = {"error": str(e)}
        return result

    async def cleanup_async(self) -> dict:
        """Cleanup SQLite + PostgreSQL (retention)."""
        result = self.cleanup()

        # PostgreSQL retention: удаляем события старше N дней
        if self.pg_pool:
            try:
                async with self.pg_pool.connection() as conn:
                    async with conn.cursor() as cur:
                        # Удалить события старше 90 дней (кроме важных)
                        await cur.execute("""
                            DELETE FROM memory.events
                            WHERE created_at < now() - interval '90 days'
                              AND (importance IS NULL OR importance < 0.7)
                        """)
                        result["pg_events_deleted"] = cur.rowcount or 0

                        # Удалить сообщения старше 90 дней
                        await cur.execute("""
                            DELETE FROM memory.messages
                            WHERE created_at < now() - interval '90 days'
                        """)
                        result["pg_messages_deleted"] = cur.rowcount or 0
            except Exception as e:
                result["pg_error"] = str(e)

        return result

    # ═══════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════
    def stats(self) -> dict:
        """Общая статистика."""
        if not self.enabled:
            return {"enabled": False}

        result: dict[str, Any] = {
            "enabled": True,
            "backend": {
                "postgres": self.use_postgres,
                "lancedb": self.use_lancedb,
                "sqlite_memory": self.use_sqlite_memory,
                "sqlite_graph": self.use_sqlite_graph,
                "dual_write": self.dual_write,
            },
        }

        # Episodic (SQLite)
        if self.episodic:
            try:
                result["episodic_sqlite"] = {
                    "session_id": self.episodic.current_session_id(),
                    "messages": self.episodic.store.count_messages(
                        self.episodic.current_session_id() or "",
                    ),
                }
            except Exception:
                pass

        # Vector
        if self.vector:
            try:
                result["vector_lancedb"] = self.vector.stats()
            except Exception:
                pass

        # Graph (SQLite)
        if self.graph:
            try:
                result["graph_sqlite"] = self.graph.stats()
            except Exception:
                pass

        # Procedural
        try:
            result["procedural"] = self.procedural.stats()
        except Exception:
            pass

        # User
        try:
            result["user"] = self.user.stats()
        except Exception:
            pass

        return result

    async def stats_async(self) -> dict:
        """Расширенная статистика с PostgreSQL."""
        result = self.stats()

        # PostgreSQL
        if self.pg_pool:
            try:
                rows = await self.pg_pool.execute("""
                    SELECT
                        (SELECT COUNT(*) FROM memory.sessions)
                            AS sessions,
                        (SELECT COUNT(*) FROM memory.messages)
                            AS messages,
                        (SELECT COUNT(*) FROM memory.events)
                            AS events,
                        (SELECT COUNT(*) FROM vectors.chunks)
                            AS chunks,
                        (SELECT COUNT(*) FROM graph.nodes)
                            AS nodes,
                        (SELECT COUNT(*) FROM graph.edges)
                            AS edges
                """)
                result["postgres"] = rows[0] if rows else {}
            except Exception as e:
                result["postgres"] = {"error": str(e)}

        # AGE
        if self.pg_age:
            try:
                result["age"] = await self.pg_age.stats()
            except Exception:
                pass

        return result

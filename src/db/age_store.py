"""Apache AGE — property graph.

Требует AGE extension. Работает через Cypher-запросы.
Отдельный pool, потому что AGE требует поиск в ag_catalog.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


# Простая защита от инъекций в Cypher
FORBIDDEN_KEYWORDS = re.compile(
    r"\b(detach\s+delete|drop\s+graph|drop\s+label)\b",
    re.IGNORECASE,
)


class AgeStore:
    """Обёртка над Apache AGE.

    Использует отдельный connection pool (AGE-база, порт 5433).
    Все запросы — Cypher, передаются через cypher() функцию.
    """

    def __init__(
        self,
        dsn: str,
        graph_name: str = "llm_graph",
        min_size: int = 1,
        max_size: int = 5,
    ):
        self.dsn = dsn
        self.graph = graph_name
        self.min_size = min_size
        self.max_size = max_size
        self._pool: "DatabasePool | None" = None  # Windows-safe пул (мост)
        self._available: bool | None = None

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════
    async def start(self) -> bool:
        """Открыть pool и проверить AGE."""
        if self._pool is not None:
            return self._available or False

        try:
            from src.db.pool import DatabasePool, _fmt_exc
            # DatabasePool вместо сырого AsyncConnectionPool: TCP-пробник
            # (fail-fast без спама psycopg), selector-луп-мост (Proactor-
            # несовместимость Windows), гарантированное закрытие при неудаче.
            # autocommit=True — как раньше для AGE.
            pool = DatabasePool(
                self.dsn, min_size=self.min_size, max_size=self.max_size,
                autocommit=True, connect_timeout=10.0,
            )
            await pool.start()
        except Exception as e:
            try:
                from src.db.pool import _fmt_exc
                reason = _fmt_exc(e)
            except Exception:
                reason = str(e).strip() or e.__class__.__name__
            logger.warning("AGE pool start failed: %s", reason)
            self._available = False
            self._pool = None
            return False
        self._pool = pool

        # Проверить extension
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT EXISTS (SELECT 1 FROM pg_extension "
                        "WHERE extname = 'age') AS has_age"
                    )
                    row = await cur.fetchone()
                    self._available = bool(row and row.get("has_age"))
        except Exception as e:
            logger.warning("AGE extension check failed: %s", e)
            self._available = False

        if self._available:
            logger.info(
                "Apache AGE ready: %s (graph=%s)",
                self.dsn.split("@")[-1], self.graph,
            )
        return self._available

    async def stop(self) -> None:
        if self._pool:
            await self._pool.stop()
            self._pool = None
            logger.info("AGE pool stopped")

    async def is_available(self) -> bool:
        if self._available is not None:
            return self._available
        return await self.start()

    # ═══════════════════════════════════════════════════════
    # Cypher
    # ═══════════════════════════════════════════════════════
    async def _cypher(
        self,
        query: str,
        params: dict | None = None,
        columns: list[str] | None = None,
    ) -> list[dict]:
        """Выполнить Cypher-запрос.

        Возвращает список dict'ов, распарсенных из agtype.
        """
        if not await self.is_available():
            return []

        # Защита от опасных запросов
        if FORBIDDEN_KEYWORDS.search(query):
            logger.warning("Cypher: forbidden keyword in query")
            return []

        # Колонки для AS-секции (по умолчанию result agtype)
        cols = columns or ["result"]
        as_clause = ", ".join(f"{c} agtype" for c in cols)

        # Cypher в AGE требует префикс ag_catalog
        sql = f"""
            SET search_path = ag_catalog, "$user", public;
            SELECT * FROM cypher('{self.graph}', $$
                {query}
            $$) AS ({as_clause});
        """

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Иногда SET + SELECT вместе не работают — используем отдельные
                    await cur.execute(
                        "SET search_path = ag_catalog, \"$user\", public"
                    )
                    await cur.execute(
                        f"SELECT * FROM cypher('{self.graph}', $cy$ {query} $cy$) "
                        f"AS ({as_clause})"
                    )
                    rows = await cur.fetchall()
            return [self._parse_row(r) for r in rows]
        except Exception as e:
            logger.warning("Cypher failed: %s\nQuery: %s", e, query[:200])
            return []

    @staticmethod
    def _parse_row(row: dict) -> dict:
        """Парсит agtype в обычные Python-типы."""
        import json
        result = {}
        for k, v in row.items():
            if v is None:
                result[k] = None
                continue
            if isinstance(v, (dict, list, int, float, bool)):
                result[k] = v
                continue
            if isinstance(v, str):
                # agtype строки имеют суффикс ::type, например "123"::integer
                cleaned = v
                # Пробуем JSON
                if cleaned.startswith("{") or cleaned.startswith("["):
                    try:
                        result[k] = json.loads(cleaned)
                        continue
                    except json.JSONDecodeError:
                        pass
                # Пробуем число
                try:
                    if "." in cleaned:
                        result[k] = float(cleaned)
                    else:
                        result[k] = int(cleaned)
                    continue
                except ValueError:
                    pass
                # Иначе — строка
                result[k] = cleaned
                continue
            result[k] = str(v)
        return result

    # ═══════════════════════════════════════════════════════
    # Mutations — создать узлы
    # ═══════════════════════════════════════════════════════
    @staticmethod
    def _escape(value: str) -> str:
        """Экранирование для Cypher-литералов (простое)."""
        return value.replace("\\", "\\\\").replace("'", "\\'")

    async def create_file_node(
        self,
        path: str,
        importance: float | None = None,
        role: str | None = None,
    ) -> None:
        props = [f"path: '{self._escape(path)}'"]
        if importance is not None:
            props.append(f"importance: {importance}")
        if role:
            props.append(f"role: '{self._escape(role)}'")
        props_str = ", ".join(props)

        await self._cypher(
            f"MERGE (f:File {{ {props_str} }}) RETURN f.path"
        )

    async def create_function_node(
        self,
        id_: str,
        name: str,
        file: str,
        importance: float | None = None,
        role: str | None = None,
    ) -> None:
        props = [
            f"id: '{self._escape(id_)}'",
            f"name: '{self._escape(name)}'",
            f"file: '{self._escape(file)}'",
        ]
        if importance is not None:
            props.append(f"importance: {importance}")
        if role:
            props.append(f"role: '{self._escape(role)}'")
        props_str = ", ".join(props)

        await self._cypher(
            f"MERGE (fn:Function {{ {props_str} }}) RETURN fn.id"
        )

    async def create_class_node(
        self,
        id_: str,
        name: str,
        file: str,
        importance: float | None = None,
    ) -> None:
        props = [
            f"id: '{self._escape(id_)}'",
            f"name: '{self._escape(name)}'",
            f"file: '{self._escape(file)}'",
        ]
        if importance is not None:
            props.append(f"importance: {importance}")
        props_str = ", ".join(props)

        await self._cypher(
            f"MERGE (c:Class {{ {props_str} }}) RETURN c.id"
        )

    async def create_concept_node(
        self,
        name: str,
        importance: float | None = None,
    ) -> None:
        props = [f"name: '{self._escape(name)}'"]
        if importance is not None:
            props.append(f"importance: {importance}")
        props_str = ", ".join(props)

        await self._cypher(
            f"MERGE (cn:Concept {{ {props_str} }}) RETURN cn.name"
        )

    async def create_session_node(
        self,
        id_: str,
        title: str | None = None,
    ) -> None:
        props = [f"id: '{self._escape(id_)}'"]
        if title:
            props.append(f"title: '{self._escape(title)}'")
        props_str = ", ".join(props)

        await self._cypher(
            f"MERGE (s:Session {{ {props_str} }}) RETURN s.id"
        )

    # ═══════════════════════════════════════════════════════
    # Mutations — создать рёбра
    # ═══════════════════════════════════════════════════════
    async def create_edge(
        self,
        src_prop: tuple[str, str],
        dst_prop: tuple[str, str],
        kind: str,
        weight: float = 1.0,
    ) -> None:
        """Создать ребро между двумя узлами.

        src_prop / dst_prop: (prop_name, prop_value) — например
            ("path", "src/main.py") или ("id", "func::hello")
        """
        src_key, src_val = src_prop
        dst_key, dst_val = dst_prop
        kind_upper = kind.upper().replace("-", "_")

        await self._cypher(f"""
            MATCH (a {{ {src_key}: '{self._escape(src_val)}' }})
            MATCH (b {{ {dst_key}: '{self._escape(dst_val)}' }})
            MERGE (a)-[r:{kind_upper}]->(b)
            SET r.weight = {weight}
            RETURN r
        """)

    # ═══════════════════════════════════════════════════════
    # Queries
    # ═══════════════════════════════════════════════════════
    async def who_uses(
        self, node_id: str, max_depth: int = 3,
    ) -> list[dict]:
        """Кто использует (CALLS) указанный узел."""
        return await self._cypher(
            f"""
            MATCH path = (caller)-[:CALLS*1..{max_depth}]->(target {{id: '{self._escape(node_id)}'}})
            RETURN DISTINCT caller.id AS id,
                   caller.name AS name,
                   caller.file AS file,
                   length(path) AS depth
            ORDER BY depth
            LIMIT 100
            """,
            columns=["id", "name", "file", "depth"],
        )

    async def impact_of_change(
        self, node_id: str, max_depth: int = 5,
    ) -> list[dict]:
        """Что затронет изменение узла (impact analysis)."""
        return await self._cypher(
            f"""
            MATCH path = (start {{id: '{self._escape(node_id)}'}})-[*1..{max_depth}]->(affected)
            RETURN DISTINCT affected.id AS id,
                   affected.name AS name,
                   affected.file AS file,
                   affected.importance AS importance,
                   length(path) AS depth
            ORDER BY affected.importance DESC, depth
            LIMIT 200
            """,
            columns=["id", "name", "file", "importance", "depth"],
        )

    async def shortest_path(
        self,
        from_id: str,
        to_id: str,
        max_depth: int = 10,
    ) -> dict | None:
        """Кратчайший путь между двумя узлами."""
        results = await self._cypher(
            f"""
            MATCH (a {{id: '{self._escape(from_id)}'}}),
                  (b {{id: '{self._escape(to_id)}'}}),
                  path = shortestPath((a)-[*..{max_depth}]-(b))
            RETURN [n IN nodes(path) | n.id] AS nodes,
                   [r IN relationships(path) | type(r)] AS rels,
                   length(path) AS length
            """,
            columns=["nodes", "rels", "length"],
        )
        return results[0] if results else None

    async def related_concepts(
        self, node_id: str, max_depth: int = 2,
    ) -> list[dict]:
        """Связанные концепции."""
        return await self._cypher(
            f"""
            MATCH (n {{id: '{self._escape(node_id)}'}})
                  -[:RELATES_TO*1..{max_depth}]-(other:Concept)
            RETURN DISTINCT other.name AS concept,
                   other.importance AS importance
            ORDER BY importance DESC
            LIMIT 50
            """,
            columns=["concept", "importance"],
        )

    async def search_concepts(
        self, query: str, limit: int = 20,
    ) -> list[dict]:
        """Поиск концепций по имени."""
        safe_q = self._escape(query)
        return await self._cypher(
            f"""
            MATCH (c:Concept)
            WHERE c.name CONTAINS '{safe_q}'
            RETURN c.name AS name, c.importance AS importance
            LIMIT {limit}
            """,
            columns=["name", "importance"],
        )

    # ═══════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════
    async def stats(self) -> dict:
        if not await self.is_available():
            return {"enabled": False, "reason": "AGE not available"}

        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    # Nodes count
                    await cur.execute(
                        "SELECT count(*) AS c FROM llm_graph._ag_label_vertex"
                    )
                    nodes = (await cur.fetchone())["c"]
                    # Edges count
                    await cur.execute(
                        "SELECT count(*) AS c FROM llm_graph._ag_label_edge"
                    )
                    edges = (await cur.fetchone())["c"]

            return {
                "enabled": True,
                "graph": self.graph,
                "nodes": nodes,
                "edges": edges,
            }
        except Exception as e:
            logger.debug("AGE stats: %s", e)
            return {
                "enabled": True,
                "graph": self.graph,
                "nodes": 0,
                "edges": 0,
                "error": str(e),
            }

    async def health(self) -> bool:
        try:
            async with self._pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT 1")
                    await cur.fetchone()
            return True
        except Exception:
            return False

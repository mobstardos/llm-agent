"""Knowledge graph на SQLite — с поддержкой Python / JS / TS."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from src.memory.config import GraphSettings
from src.memory.graph_parsers import ParserRegistry

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    file TEXT,
    line INTEGER DEFAULT 0,
    metadata_json TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_kind ON nodes(kind);
CREATE INDEX IF NOT EXISTS idx_nodes_file ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name ON nodes(name);

CREATE TABLE IF NOT EXISTS edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    kind TEXT NOT NULL,
    metadata_json TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (src, dst, kind)
);
CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src, kind);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst, kind);
"""


class KnowledgeGraph:
    def __init__(self, cfg: GraphSettings, base_dir: Path):
        self.cfg = cfg
        self.path = base_dir / cfg.db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.registry = ParserRegistry()
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    # Index
    # ═══════════════════════════════════════════════════════
    def index_project(self, root: str | Path) -> dict:
        import time
        root = Path(root)
        stats = {"files": 0, "nodes": 0, "edges": 0, "skipped": 0}

        with self._conn() as conn:
            conn.execute("DELETE FROM edges")
            conn.execute("DELETE FROM nodes")

        ignore_dirs = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            ".idea", ".vscode", "dist", "build", "data",
        }
        supported = set(self.registry.supported_extensions())

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(p in ignore_dirs for p in path.parts):
                continue
            if path.suffix.lower() not in supported:
                continue

            parser = self.registry.get_for(path)
            if parser is None:
                stats["skipped"] += 1
                continue

            try:
                source = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                stats["skipped"] += 1
                continue

            try:
                rel = str(path.relative_to(root)).replace("\\", "/")
                nodes, edges = parser.parse(path, source)
                self._save(rel, nodes, edges, stats)
                stats["files"] += 1
            except Exception as e:
                logger.debug("Parse %s: %s", path, e)
                stats["skipped"] += 1

        return stats

    def _save(self, rel: str, nodes, edges, stats: dict) -> None:
        import time
        now = time.time()
        with self._conn() as conn:
            for n in nodes:
                conn.execute(
                    "INSERT OR REPLACE INTO nodes "
                    "(id, kind, name, file, line, metadata_json, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (n.id, n.kind, n.name, n.file, n.line, now),
                )
                stats["nodes"] += 1
            for e in edges:
                conn.execute(
                    "INSERT OR REPLACE INTO edges "
                    "(src, dst, kind, metadata_json, updated_at) "
                    "VALUES (?, ?, ?, NULL, ?)",
                    (e.src, e.dst, e.kind, now),
                )
                stats["edges"] += 1

    # ═══════════════════════════════════════════════════════
    # Query
    # ═══════════════════════════════════════════════════════
    def who_uses(self, name: str, kind: str = "function") -> list[dict]:
        like = f"%::{name}"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT src FROM edges WHERE dst LIKE ? OR dst = ?",
                (like, name),
            ).fetchall()
        return [{"src": r["src"]} for r in rows]

    def depends_on(self, file: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT dst, kind FROM edges WHERE src = ?",
                (f"file:{file}",),
            ).fetchall()
        return [{"dst": r["dst"], "kind": r["kind"]} for r in rows]

    def who_imports(self, module: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT src FROM edges WHERE dst = ? AND kind = 'imports'",
                (f"module:{module}",),
            ).fetchall()
        return [{"src": r["src"]} for r in rows]

    def search_nodes(self, query: str, limit: int = 20) -> list[dict]:
        like = f"%{query}%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, kind, name, file, line FROM nodes "
                "WHERE name LIKE ? ORDER BY name LIMIT ?",
                (like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict:
        with self._conn() as conn:
            n = conn.execute("SELECT COUNT(*) AS c FROM nodes").fetchone()["c"]
            e = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
            by_kind = dict(conn.execute(
                "SELECT kind, COUNT(*) FROM nodes GROUP BY kind"
            ).fetchall())
        return {
            "nodes": n, "edges": e,
            "by_kind": by_kind,
            "parsers": self.registry.supported_extensions(),
        }

"""Векторная память на LanceDB."""
from __future__ import annotations

import logging
from pathlib import Path

from src.memory.base import RetrievedChunk
from src.memory.chunker import Chunker
from src.memory.config import VectorSettings
from src.memory.embedder import Embedder

logger = logging.getLogger(__name__)


class VectorMemory:
    def __init__(self, cfg: VectorSettings, base_dir: Path):
        self.cfg = cfg
        self.base_dir = base_dir
        self.store_path = base_dir / cfg.store_path
        self.embedder = Embedder(cfg.embedder)
        self.chunker = Chunker(cfg.chunking)
        self._db = None
        self._table = None

    def _lazy_init(self):
        if self._table is not None:
            return
        import lancedb
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = lancedb.connect(str(self.store_path))

        try:
            names = self._db.table_names()
        except Exception:
            names = []

        if self.cfg.table_name in names:
            self._table = self._db.open_table(self.cfg.table_name)
        else:
            import pyarrow as pa
            dim = self.embedder.dim
            schema = pa.schema([
                pa.field("id", pa.string()),
                pa.field("text", pa.string()),
                pa.field("vector", pa.list_(pa.float32(), dim)),
                pa.field("file", pa.string()),
                pa.field("start_line", pa.int32()),
                pa.field("end_line", pa.int32()),
                pa.field("symbols", pa.string()),
                pa.field("language", pa.string()),
                pa.field("hash", pa.string()),
            ])
            self._table = self._db.create_table(
                self.cfg.table_name, schema=schema,
            )
        logger.info("Vector store готов: %s", self.store_path)

    def _hash(self, text: str) -> str:
        try:
            import xxhash
            return xxhash.xxh64(text.encode("utf-8")).hexdigest()
        except ImportError:
            import hashlib
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    def _matches(self, path: Path, patterns: list[str]) -> bool:
        from fnmatch import fnmatch
        rel = str(path).replace("\\", "/")
        return any(fnmatch(rel, p) for p in patterns)

    def index_project(self, root: str | Path) -> dict[str, int]:
        self._lazy_init()
        root = Path(root)
        stats = {"scanned": 0, "indexed": 0, "skipped": 0, "chunks": 0}

        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            rel_str = str(rel).replace("\\", "/")

            if not self._matches(rel, self.cfg.indexing.file_patterns):
                continue
            if self._matches(rel, self.cfg.indexing.ignore_patterns):
                stats["skipped"] += 1
                continue

            size_kb = path.stat().st_size / 1024
            if size_kb > self.cfg.indexing.max_file_size_kb:
                stats["skipped"] += 1
                continue

            stats["scanned"] += 1
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                stats["skipped"] += 1
                continue

            n = self._index_file(rel_str, path, content)
            if n > 0:
                stats["indexed"] += 1
                stats["chunks"] += n
        return stats

    def _index_file(self, rel_str: str, path: Path, content: str) -> int:
        self._lazy_init()
        text_hash = self._hash(content)

        try:
            self._table.delete(f"file = '{rel_str}'")
        except Exception:
            pass

        chunks = self.chunker.chunk_file(path, content)
        if not chunks:
            return 0

        lang = path.suffix.lstrip(".").lower()
        texts = [c.text for c in chunks]
        vectors = self.embedder.embed(texts)

        records = []
        for c, v in zip(chunks, vectors):
            records.append({
                "id": f"{rel_str}::{c.start_line}",
                "text": c.text,
                "vector": v,
                "file": rel_str,
                "start_line": c.start_line,
                "end_line": c.end_line,
                "symbols": ",".join(c.symbols),
                "language": lang,
                "hash": text_hash,
            })
        try:
            self._table.add(records)
        except Exception as e:
            logger.exception("Ошибка добавления %s: %s", rel_str, e)
            return 0
        return len(records)

    def search(
        self, query: str, top_k: int | None = None,
        filters: dict | None = None,
    ) -> list[RetrievedChunk]:
        self._lazy_init()
        k = top_k or self.cfg.retrieval.top_k
        vec = self.embedder.embed_one(query)

        try:
            q = self._table.search(vec).limit(k * 3)
            if filters:
                where = []
                for field, val in filters.items():
                    if isinstance(val, str):
                        where.append(f"{field} = '{val}'")
                    else:
                        where.append(f"{field} = {val}")
                if where:
                    q = q.where(" AND ".join(where))
            rows = q.to_list()
        except Exception as e:
            logger.exception("Ошибка поиска: %s", e)
            return []

        results: list[RetrievedChunk] = []
        seen: dict[str, int] = {}

        for row in rows:
            score = 1.0 - float(row.get("_distance", 0.0))
            if score < self.cfg.retrieval.min_score:
                continue
            file_ = row.get("file", "")
            if self.cfg.retrieval.dedup_by_file:
                count = seen.get(file_, 0)
                if count >= self.cfg.retrieval.max_per_file:
                    continue
                seen[file_] = count + 1

            meta = {
                "file": file_,
                "start_line": row.get("start_line"),
                "end_line": row.get("end_line"),
                "symbols": row.get("symbols", ""),
                "language": row.get("language", ""),
            }
            results.append(RetrievedChunk(
                text=row.get("text", ""), score=score, metadata=meta,
            ))
            if len(results) >= k:
                break
        return results

    def stats(self) -> dict:
        try:
            self._lazy_init()
            return {
                "enabled": True,
                "chunks": self._table.count_rows(),
                "store_path": str(self.store_path),
            }
        except Exception as e:
            return {"enabled": False, "error": str(e)}

    def clear(self) -> int:
        try:
            self._lazy_init()
            count = self._table.count_rows()
            self._db.drop_table(self.cfg.table_name)
            self._table = None
            return count
        except Exception as e:
            logger.exception("Ошибка clear: %s", e)
            return 0

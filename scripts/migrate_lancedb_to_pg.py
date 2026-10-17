"""Миграция векторов из LanceDB в PostgreSQL + pgvector.

Запуск:
    python scripts/migrate_lancedb_to_pg.py

Опции:
    --batch-size N       (по умолчанию 100)
    --keep-lancedb       не удалять LanceDB после миграции
    --dry-run            только посчитать, не писать
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--keep-lancedb", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    from src.config import get_settings
    from src.db.pool import get_pool
    from src.db.vector_store import VectorStore

    s = get_settings()

    # Найти LanceDB
    lancedb_path = Path(s.project_root) / "data" / "vector.lance"
    if not lancedb_path.exists():
        lancedb_path = BASE_DIR / "data" / "vector.lance"

    if not lancedb_path.exists():
        logger.error("LanceDB не найдена: %s", lancedb_path)
        return 1

    logger.info("LanceDB: %s", lancedb_path)

    try:
        import lancedb
    except ImportError:
        logger.error("lancedb не установлен: pip install lancedb")
        return 1

    db = lancedb.connect(str(lancedb_path))
    names = db.table_names()
    if "chunks" not in names:
        logger.error("Таблица 'chunks' не найдена. Есть: %s", names)
        return 1

    table = db.open_table("chunks")
    total = table.count_rows()
    logger.info("Всего чанков: %d", total)

    if args.dry_run:
        logger.info("DRY-RUN: пропускаю запись в PostgreSQL")
        return 0

    pool = await get_pool()
    store = VectorStore(pool)

    # Прогресс-бар
    offset = 0
    migrated = 0
    failed = 0

    while offset < total:
        try:
            # LanceDB: используем to_arrow().slice() или search
            data = table.search().limit(args.batch_size).offset(offset).to_list()
        except Exception as e:
            logger.warning("Search failed, fallback to pandas: %s", e)
            try:
                import pandas as pd
                df = table.to_pandas().iloc[
                    offset:offset + args.batch_size
                ]
                data = df.to_dict("records")
            except Exception as e2:
                logger.error("Fallback failed: %s", e2)
                break

        if not data:
            break

        for row in data:
            try:
                # Поля из LanceDB chunks: id, text, vector, file, start_line,
                # end_line, symbols, language, hash
                vector = row.get("vector")
                if vector is None:
                    failed += 1
                    continue

                # Конвертируем в list[float]
                if hasattr(vector, "tolist"):
                    vector = vector.tolist()
                elif isinstance(vector, bytes):
                    failed += 1
                    continue

                if len(vector) != 1024:
                    logger.debug(
                        "Skip chunk with dim %d (need 1024)", len(vector),
                    )
                    failed += 1
                    continue

                symbols_raw = row.get("symbols") or ""
                if isinstance(symbols_raw, str):
                    symbols = [
                        s.strip() for s in symbols_raw.split(",") if s.strip()
                    ]
                elif isinstance(symbols_raw, list):
                    symbols = symbols_raw
                else:
                    symbols = []

                await store.upsert_chunk(
                    file=row.get("file", ""),
                    content=row.get("text", ""),
                    embedding=vector,
                    start_line=row.get("start_line"),
                    end_line=row.get("end_line"),
                    language=row.get("language"),
                    symbols=symbols,
                )
                migrated += 1
            except Exception as e:
                failed += 1
                logger.debug("Chunk failed: %s", e)

        offset += len(data)
        pct = round(100 * offset / total, 1)
        logger.info(
            "Прогресс: %d/%d (%.1f%%) migrated=%d failed=%d",
            offset, total, pct, migrated, failed,
        )

    logger.info("═" * 60)
    logger.info("✓ Миграция завершена")
    logger.info("  Migrated: %d", migrated)
    logger.info("  Failed:   %d", failed)

    # Stats
    stats = await store.stats()
    logger.info("PostgreSQL vectors: %s", stats)
    logger.info("═" * 60)

    if not args.keep_lancedb and migrated > 0:
        logger.info(
            "LanceDB сохранён. Удалите вручную, если уверены: %s",
            lancedb_path,
        )

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

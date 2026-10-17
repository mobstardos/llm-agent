"""Ретенция долговременных зеркал PostgreSQL (Task 14, эксплуатация).

Зеркала (journal.events_mirror, journal.actions, ops.chat_messages,
ops.plans, memory.tasks) копятся бесконечно. PGRetention удаляет строки
старше N дней — пакетами, с dry-run и отчётом по каждой таблице.

Почему это безопасно для репликатора (db/ops.sql, src/db/replicator.py):
курсор — монотонная позиция потока (last id / offset+seq / mtime+size),
а не ссылка на строку. Удаление УЖЕ спроецированных старых строк не
влияет на курсор: новые данные продолжают дотягиваться без потерь.

Включение: env PG_RETENTION_DAYS > 0 (по умолчанию 0 — ВЫКЛЮЧЕНО,
ничего не удаляется само). Ручной прогон — фича features/ops
(POST /api/ops/retention/run) или direct вызов run().

Правило эскалации НЕ применяется (в отличие от SQLite-ретенции
src/journal/retention.py, которая сжимает тени при нехватке диска):
здесь одна политика «старше N дней», управляемая явно.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class _Table:
    name: str              # полное имя таблицы
    ts_col: str            # колонка времени
    ts_expr: str = ""      # выражение времени (если нужно coalesce)
    pk_cols: tuple = ()    # PK для пакетного удаления

    def __post_init__(self) -> None:
        if not self.ts_expr:
            self.ts_expr = self.ts_col

    @property
    def pk(self) -> tuple:
        return self.pk_cols or ("ctid",)


TABLES: list[_Table] = [
    _Table("journal.events_mirror", "ts", pk_cols=("event_uid",)),
    _Table("journal.actions", "created_at",
           pk_cols=("id", "created_at")),   # PK партиционированной
    _Table("ops.chat_messages", "ts", "coalesce(ts, ingested_at)",
           pk_cols=("session_id", "seq")),
    _Table("ops.plans", "updated_at", "coalesce(updated_at, ingested_at)",
           pk_cols=("plan_id",)),
    _Table("memory.tasks", "updated_at", "coalesce(updated_at, created_at)",
           pk_cols=("id",)),
]


@dataclass
class _Report:
    table: str
    total: int = 0
    deletable: int = 0
    oldest: str = ""
    deleted: int = 0
    error: str = ""
    rows: list = field(default_factory=list)


class PGRetention:
    """Управление ретенцией зеркал. Все методы мягкие (без исключений)."""

    def __init__(self, pool, default_days: int = 0, batch: int = 5000):
        self.pool = pool
        self.default_days = max(0, int(default_days))
        self.batch = max(100, int(batch))
        self.last_report: list[dict] = []

    # ─── Статус ────────────────────────────────────────────────────
    async def status(self, days: int | None = None) -> dict:
        """Счётчики: всего строк, старше cutoff, самая старая строка."""
        days = self.default_days if days is None else max(0, int(days))
        out: dict = {
            "default_days": self.default_days,
            "days": days,
            "enabled": self.default_days > 0,
            "tables": [],
        }
        for t in TABLES:
            rep = _Report(table=t.name)
            try:
                async with self.pool.connection() as conn, \
                        conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT count(*)::int AS c FROM {t.name}")
                    rep.total = (await cur.fetchone())["c"]
                    await cur.execute(
                        f"SELECT min({t.ts_expr})::text AS oldest "
                        f"FROM {t.name}")
                    rep.oldest = (await cur.fetchone())["oldest"] or ""
                    if days > 0:
                        await cur.execute(
                            f"SELECT count(*)::int AS c FROM {t.name} "
                            f"WHERE {t.ts_expr} < now() - "
                            f"(%s || ' days')::interval", (days,))
                        rep.deletable = (await cur.fetchone())["c"]
            except Exception as e:
                rep.error = str(e)[:200]
            out["tables"].append({
                "table": rep.table, "total": rep.total,
                "deletable": rep.deletable, "oldest": rep.oldest,
                "error": rep.error,
            })
        return out

    # ─── Прогон ────────────────────────────────────────────────────
    async def run(self, days: int | None = None, dry_run: bool = True,
                  batch: int | None = None) -> dict:
        """Удалить строки старше days. dry_run=True — только посчитать.

        Возвращает отчёт по таблицам. Никогда не бросает исключений.
        """
        days = self.default_days if days is None else max(0, int(days))
        batch = self.batch if batch is None else max(100, int(batch))
        t0 = time.time()
        if days <= 0:
            return {
                "ok": False, "days": 0, "dry_run": dry_run,
                "reason": "days=0 — ретенция выключена "
                          "(PG_RETENTION_DAYS / параметр days)",
                "tables": [], "deleted_total": 0,
                "seconds": 0.0,
            }

        out: dict = {
            "ok": True, "days": days, "dry_run": dry_run,
            "tables": [], "deleted_total": 0,
            "seconds": 0.0,
        }
        for t in TABLES:
            rep = _Report(table=t.name)
            try:
                async with self.pool.connection() as conn, \
                        conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT count(*)::int AS c FROM {t.name} "
                        f"WHERE {t.ts_expr} < now() - "
                        f"(%s || ' days')::interval", (days,))
                    rep.deletable = (await cur.fetchone())["c"]
                    if dry_run or rep.deletable == 0:
                        rep.deleted = 0
                    else:
                        # Пакетное удаление по PK (короткие блокировки,
                        # корректно для NULL-дат и партиций)
                        pk_sel = ", ".join(t.pk)
                        while True:
                            await cur.execute(
                                f"DELETE FROM {t.name} WHERE ({pk_sel}) IN ("
                                f"  SELECT {pk_sel} FROM {t.name} sel"
                                f"  WHERE {t.ts_expr} < now() - "
                                f"        (%s || ' days')::interval"
                                f"  LIMIT %s)", (days, batch))
                            n = cur.rowcount or 0
                            rep.deleted += n
                            if n < batch:
                                break
            except Exception as e:
                rep.error = str(e)[:200]
            out["tables"].append({
                "table": rep.table, "deletable": rep.deletable,
                "deleted": rep.deleted, "error": rep.error,
            })
            out["deleted_total"] += rep.deleted
        out["seconds"] = round(time.time() - t0, 2)
        self.last_report = out["tables"]
        if not dry_run:
            logger.info("PG retention: удалено %s строк (>%d дней)",
                        out["deleted_total"], days)
        return out


async def retention_loop(pool, days: int, interval_hours: float = 24.0,
                         stop_event=None) -> None:
    """Фоновый цикл ретенции (main.py блок 7.6). Никогда не падает."""
    import asyncio
    r = PGRetention(pool, default_days=days)
    # Первый прогон — через 10 минут после старта (даём БД прогреться)
    initial = 600.0
    while True:
        try:
            if stop_event:
                await asyncio.wait_for(stop_event.wait(), timeout=initial)
                return
            await asyncio.sleep(initial)
        except asyncio.TimeoutError:
            pass
        initial = max(1.0, float(interval_hours) * 3600.0)
        try:
            await r.run()
        except Exception as e:                     # pragma: no cover
            logger.warning("retention_loop: %s", e)

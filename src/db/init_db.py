"""Инициализация и проверка схемы PostgreSQL (CLI + библиотека).

Применяет SQL-файлы из db/ в правильном порядке, идемпотентно:
    python -m src.db.init_db                 # применить всё
    python -m src.db.init_db --check         # только проверить состояние
    python -m src.db.init_db --with-age      # + схема Apache AGE (отдельная БД)
    python -m src.db.init_db --file db/ops.sql
    python -m src.db.init_db --dsn postgresql://...

DSN берётся из settings (env: DATABASE_URL → только postgres:// — иначе
PG_APP_HOST/PORT/USER/PASSWORD/DATABASE, как в src/config.py).

Особенности:
- Разбор SQL: DO $$...$$ блоки, $tag$-строки, 'кавычки', -- и /* */-комментарии.
- Расширения: preflight CREATE EXTENSION; statements, зависящие от
  отсутствующих расширений (pgvector), мягко пропускаются с пометкой
  (pg_trgm/btree_gin/uuid-ossp — в комплекте PostgreSQL; vector и AGE — нет).
- Каждая ошибка собирается в отчёт; exit-код 0 только при полном успехе
  (или при пропусках из-за расширений — они считаются предупреждениями).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DB_DIR = BASE_DIR / "db"

# Порядок применения (age_schema.sql — отдельная БД AGE, только --with-age)
DEFAULT_FILES = [
    "init.sql",               # 8 схем: memory/vectors/graph/cache/policies/audit/metrics/meta
    "journal.sql",            # схема journal (actions/snapshots/blobs/…)
    "ops.sql",                # схема ops (зеркала журнала, чатов, планов, курсоры)
    "analytics.sql",          # матвьюхи аналитики
    "search_improvements.sql",
    "cdc_notify.sql",
]

# Расширения, без которых некоторые statements не выполняются.
EXTENSION_HINTS = {
    "vector": ("vector(", "halfvec(", "bit(1024)", "hnsw", "ivfflat",
               "extension \"vector\""),
    "pg_trgm": ("gin_trgm_ops", "extension \"pg_trgm\""),
    "btree_gin": ("btree_gin",),
    "uuid-ossp": ("uuid_generate_v4", "extension \"uuid-ossp\""),
}


# ═══════════════════════════════════════════════════════════════════════
# Разбор SQL на statements
# ═══════════════════════════════════════════════════════════════════════
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")


def split_statements(sql: str) -> list[str]:
    """Режет SQL на statements по ';' вне кавычек/комментариев/$$-блоков.

    Понимает: 'строки' (с ''-экранированием), "идентификаторы",
    -- строчные и /* блочные */ комментарии, $tag$...$tag$ (DO $$ ... $$).
    """
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    in_line = in_block = in_squote = in_dquote = False
    dollar: str | None = None

    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if in_line:
            buf.append(ch)
            if ch == "\n":
                in_line = False
            i += 1
            continue
        if in_block:
            buf.append(ch)
            if ch == "*" and nxt == "/":
                buf.append(nxt)
                i += 2
                in_block = False
                continue
            i += 1
            continue
        if in_squote:
            buf.append(ch)
            if ch == "'":
                if nxt == "'":          # '' внутри строки
                    buf.append(nxt)
                    i += 2
                    continue
                in_squote = False
            i += 1
            continue
        if in_dquote:
            buf.append(ch)
            if ch == '"':
                in_dquote = False
            i += 1
            continue
        if dollar is not None:
            if sql.startswith(dollar, i):
                buf.append(dollar)
                i += len(dollar)
                dollar = None
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line = True
            buf.append(ch)
            i += 1
            continue
        if ch == "/" and nxt == "*":
            in_block = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_squote = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_dquote = True
            buf.append(ch)
            i += 1
            continue
        if ch == "$":
            m = _DOLLAR_TAG.match(sql, i)
            if m:
                dollar = m.group(0)
                buf.append(dollar)
                i += len(dollar)
                continue
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                stmts.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        stmts.append(tail)
    return stmts


def _statement_wants(stmt: str, ext: str) -> bool:
    """Зависит ли statement от расширения ext (по маркерам в тексте)."""
    low = stmt.lower()
    return any(marker in low for marker in EXTENSION_HINTS.get(ext, ()))


# ═══════════════════════════════════════════════════════════════════════
# Применение
# ═══════════════════════════════════════════════════════════════════════
def resolve_dsn(explicit: str = "") -> str:
    if explicit:
        return explicit
    from src.config import get_settings
    return get_settings().postgres.dsn()


def _preflight_extensions(conn) -> dict[str, bool]:
    """Пытается создать расширения; возвращает карту доступности."""
    available: dict[str, bool] = {}
    for ext in ("pg_trgm", "btree_gin", "uuid-ossp", "vector"):
        try:
            with conn.cursor() as cur:
                cur.execute(f'CREATE EXTENSION IF NOT EXISTS "{ext}"')
            available[ext] = True
        except Exception as e:
            conn.rollback()
            available[ext] = False
            logger.info("Расширение %s недоступно: %s", ext, str(e)[:120])
    return available


def _code_of(stmt: str) -> str:
    """Statement без ведущих комментариев (для проверки GRANT и т.п.)."""
    lines = [l for l in stmt.splitlines() if not l.strip().startswith("--")]
    return "\n".join(lines).strip()


def apply_sql_file(
    conn, path: Path, *, extensions: dict[str, bool] | None = None,
    missing_objects: set[str] | None = None,
) -> dict:
    """Применяет один SQL-файл statement за statement'ом.

    missing_objects — общая для всего запуска коллекция имён объектов,
    которые НЕ были созданы из-за отсутствия расширений. Ошибки вида
    «relation X does not exist» по этим объектам классифицируются как
    каскадные предупреждения, а не как ошибки.
    """
    extensions = extensions or {}
    missing_objects = missing_objects if missing_objects is not None else set()
    report = {"file": path.name, "executed": 0, "skipped": [], "errors": []}
    sql = path.read_text(encoding="utf-8")
    create_re = re.compile(
        r"CREATE\s+(?:UNLOGGED\s+)?(?:TABLE|MATERIALIZED\s+VIEW)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.IGNORECASE,
    )
    rel_re = re.compile(r'relation "([^"]+)" does not exist')
    # Ограничения версий расширений (например, старый pgvector без halfvec)
    ext_limit_re = re.compile(
        r'(?:type "(?:halfvec|vector|sparsevec)" does not exist|'
        r'access method "(?:hnsw|ivfflat)" does not exist)'
    )

    def _is_cascade(rel: str) -> bool:
        rel = rel.lower()
        for obj in missing_objects:
            o = obj.lower()
            if rel == o or rel.endswith(o) or o.endswith(rel):
                return True
        return False

    for stmt in split_statements(sql):
        head = " ".join(stmt.split())[:80]
        code = _code_of(stmt)
        upper_code = code.upper()
        # CREATE EXTENSION уже обработаны в preflight — пропускаем в файлах
        if upper_code.startswith("CREATE EXTENSION"):
            report["skipped"].append((head, "обработан в preflight"))
            continue
        # Пропуск statements, зависящих от недоступных расширений
        missing = [
            ext for ext, ok in extensions.items()
            if not ok and _statement_wants(stmt, ext)
        ]
        if missing:
            report["skipped"].append(
                (head, "нет расширения: " + ", ".join(missing)))
            for m in create_re.finditer(stmt):
                missing_objects.add(m.group(1))
            continue
        try:
            with conn.cursor() as cur:
                cur.execute(stmt)
            conn.commit()
            report["executed"] += 1
        except Exception as e:
            conn.rollback()
            msg = str(e)
            # GRANT при отсутствии роли — предупреждение, не ошибка
            if 'role "' in msg and upper_code.startswith("GRANT"):
                report["skipped"].append((head, f"роль отсутствует: {msg[:120]}"))
                continue
            # Ограничение версии расширения (старый pgvector без halfvec и т.п.)
            if ext_limit_re.search(msg):
                for m in create_re.finditer(stmt):
                    missing_objects.add(m.group(1))
                report["skipped"].append(
                    (head, f"ограничение версии расширения: {msg[:120]}"))
                continue
            # Каскад от объекта, пропущенного из-за расширений
            m = rel_re.search(msg)
            if m and _is_cascade(m.group(1)):
                # сам объект тоже не создан — запоминаем для его индексов
                for cm in create_re.finditer(stmt):
                    missing_objects.add(cm.group(1))
                report["skipped"].append(
                    (head, f"каскад: {m.group(1)} не создан (расширения)"))
                continue
            report["errors"].append((head, msg.split("\n")[0][:200]))
    return report


def init_schema(
    dsn: str, *, files: Iterable[Path] | None = None, with_age: bool = False,
) -> tuple[list[dict], bool]:
    """Применяет файлы схемы. Возвращает (отчёты, ok)."""
    import psycopg

    paths = list(files) if files is not None else [
        DB_DIR / name for name in DEFAULT_FILES
    ]
    if with_age:
        paths.append(DB_DIR / "age_schema.sql")

    reports: list[dict] = []
    ok = True
    missing_objects: set[str] = set()
    with psycopg.connect(dsn, autocommit=False) as conn:
        extensions = _preflight_extensions(conn)
        conn.commit()
        for p in paths:
            if not p.exists():
                reports.append({"file": p.name, "executed": 0,
                                "skipped": [], "errors": [("файл", "не найден")]})
                ok = False
                continue
            rep = apply_sql_file(
                conn, p, extensions=extensions, missing_objects=missing_objects,
            )
            reports.append(rep)
            if rep["errors"]:
                ok = False
    return reports, ok


# ═══════════════════════════════════════════════════════════════════════
# Проверка (--check)
# ═══════════════════════════════════════════════════════════════════════
EXPECTED_SCHEMAS = ("memory", "vectors", "graph", "cache", "policies",
                    "audit", "metrics", "meta", "journal", "ops")


def check_schema(dsn: str) -> dict:
    """Состояние БД: схемы, таблицы, расширения. Не создаёт ничего."""
    import psycopg

    info: dict = {"schemas": {}, "extensions": {}, "ok": True}
    with psycopg.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT nspname FROM pg_namespace "
                "WHERE nspname NOT LIKE 'pg_%' AND nspname <> 'information_schema'"
            )
            present = {r[0] for r in cur.fetchall()}
            for sch in EXPECTED_SCHEMAS:
                if sch in present:
                    cur.execute(
                        "SELECT count(*) FROM pg_tables WHERE schemaname = %s",
                        (sch,),
                    )
                    info["schemas"][sch] = cur.fetchone()[0]
                else:
                    info["schemas"][sch] = None
                    info["ok"] = False
            for ext in ("pg_trgm", "btree_gin", "uuid-ossp", "vector"):
                cur.execute(
                    "SELECT count(*) FROM pg_extension WHERE extname = %s", (ext,)
                )
                info["extensions"][ext] = bool(cur.fetchone()[0])
    # ops/journal — обязательны; остальные схемы могли быть созданы ранее
    for required in ("ops", "journal"):
        if info["schemas"].get(required) in (None, 0):
            info["ok"] = False
    return info


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="init_db",
        description="Инициализация схемы PostgreSQL для llm-agent",
    )
    ap.add_argument("--dsn", default="", help="PostgreSQL DSN (по умолчанию — из settings)")
    ap.add_argument("--check", action="store_true", help="только проверить состояние")
    ap.add_argument("--with-age", action="store_true", help="применить и age_schema.sql")
    ap.add_argument("--file", action="append", default=[],
                    help="применить только указанный файл (можно несколько раз)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s %(message)s")
    dsn = resolve_dsn(args.dsn)
    safe = re.sub(r"(?<=://)[^:@/]+:[^@/]+", "***:***", dsn)
    print(f"DSN: {safe}")

    try:
        if args.check:
            info = check_schema(dsn)
            print("Схемы:")
            for sch, tables in info["schemas"].items():
                state = "—" if tables is None else f"{tables} таблиц"
                print(f"  {sch:<10} {state}")
            exts = ", ".join(
                f"{k}={'✓' if v else '✗'}" for k, v in info["extensions"].items()
            )
            print(f"Расширения: {exts}")
            print("OK" if info["ok"] else "НЕ ПОЛНО (запустите без --check)")
            return 0 if info["ok"] else 1

        files = ([DB_DIR / f for f in args.file] if args.file else None)
        reports, ok = init_schema(dsn, files=files, with_age=args.with_age)
        for rep in reports:
            print(f"\n=== {rep['file']}: выполнено {rep['executed']}")
            for head, why in rep["skipped"]:
                print(f"  SKIP  {head}  ({why})")
            for head, err in rep["errors"]:
                print(f"  ERROR {head}\n        {err}")
        skipped = sum(len(r["skipped"]) for r in reports)
        errors = sum(len(r["errors"]) for r in reports)
        print(f"\nИтого: выполнено {sum(r['executed'] for r in reports)}, "
              f"пропущено {skipped} (расширения), ошибок {errors}")
        return 0 if ok else 1
    except Exception as e:
        print(f"Подключение не удалось: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

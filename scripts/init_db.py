#!/usr/bin/env python
"""CLI-обёртка: инициализация схемы PostgreSQL.

    python scripts/init_db.py               # применить всю схему
    python scripts/init_db.py --check       # проверить состояние БД
    python scripts/init_db.py --file db/ops.sql
    python scripts/init_db.py --with-age    # + AGE (отдельная БД)

DSN: DATABASE_URL (только postgres://) или PG_APP_HOST/PORT/USER/PASSWORD/DATABASE.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db.init_db import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())

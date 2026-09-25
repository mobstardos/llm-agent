"""Пакет journal — «чёрный ящик» системы: журнал действий, правок,
зависимостей с воспроизведением (replay) и откатом (rollback).

Быстрый старт:
    from src.journal.integration import init_journal, instrument_mcp_manager
    journal = init_journal(BASE_DIR, project_root)
    instrument_mcp_manager(mcp)
"""
from .config import JournalConfig          # noqa: F401
from .schema import Event, EventKind, EventStatus  # noqa: F401
from .store import JournalStore            # noqa: F401
from .shadows import ShadowStore           # noqa: F401
from .recorder import JournalRecorder      # noqa: F401
from .dependencies import DependencyGraph  # noqa: F401
from .replay import ReplayEngine           # noqa: F401
from .rollback import RollbackEngine       # noqa: F401
from .retention import RetentionManager    # noqa: F401

__version__ = "1.0.0"

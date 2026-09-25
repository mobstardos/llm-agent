"""Интеграционный слой: связывает журнал с существующими компонентами.

Всё построено на «мягких» хуках: компоненты не обязаны знать о журнале —
если journal не инициализирован, вызовы проходят сквозь без затрат.

Точки подключения (см. INTEGRATION.md для точных патчей):
  1. instrument_mcp_manager(mcp)   — перехват ВСЕХ tool calls
  2. Orchestrator.handle()         — контекст задачи + события task_start/end
  3. instrument_main(app, state)   — lifespan, API-роутер, scheduler, atexit
  4. ws_session_start(ws_id)       — сессии пользователей
"""
from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from typing import Any

logger = logging.getLogger(__name__)

# Глобальная ссылка на рекордер (инициализируется в lifespan)
RECORDER: Any = None


def set_recorder(recorder) -> None:
    global RECORDER
    RECORDER = recorder


def get_recorder():
    return RECORDER


# ═════════════════════════════════════════════════════════════
# 1. MCPManager — перехват всех tool calls
# ═════════════════════════════════════════════════════════════
def instrument_mcp_manager(mcp_manager) -> None:
    """Оборачивает MCPManager.call_tool журналированием.

    Меняет только метод объекта (не класс) — безопасно и обратимо:
    при повторном вызове ничего не задваивается (флаг _journal_patched).
    """
    if getattr(mcp_manager, "_journal_patched", False):
        return

    original_call = mcp_manager.call_tool

    async def journaled_call_tool(qualified_name: str, arguments: dict,
                                  approval_handler=None, **kwargs):
        rec = get_recorder()
        pending = None
        denied = False
        if rec is not None:
            try:
                server, _, tool = qualified_name.partition("__")
                # approval gate отрабатывает ВНУТРИ оригинального вызова;
                # здесь мы лишь фиксируем перехват заранее (deny увидим в статусе)
                pending = await rec.before_tool_call(
                    server, tool, arguments or {},
                    session_id=jr_ctx_session.get(),
                    task_id=jr_ctx_task.get(),
                    trace_id=jr_ctx_trace.get(),
                    agent_id=jr_ctx_agent.get(),
                    loop_id=jr_ctx_loop.get(),
                    iteration=jr_ctx_iter.get(),
                )
            except Exception:
                logger.exception("journal before_tool_call")
                pending = None
        try:
            result = await original_call(qualified_name, arguments,
                                         approval_handler=approval_handler,
                                         **kwargs)
        except Exception as e:
            if rec is not None and pending is not None:
                try:
                    await rec.after_tool_call(
                        pending, error=str(e),
                        session_id=jr_ctx_session.get(),
                        task_id=jr_ctx_task.get(),
                        trace_id=jr_ctx_trace.get(),
                        agent_id=jr_ctx_agent.get(),
                    )
                except Exception:
                    logger.exception("journal after_tool_call")
            raise
        if rec is not None and pending is not None:
            try:
                denied = "⛔" in str(result)[:5]
                await rec.after_tool_call(
                    pending, result=result,
                    status="denied" if denied else "",
                    session_id=jr_ctx_session.get(),
                    task_id=jr_ctx_task.get(),
                    trace_id=jr_ctx_trace.get(),
                    agent_id=jr_ctx_agent.get(),
                )
            except Exception:
                logger.exception("journal after_tool_call")
        return result

    mcp_manager.call_tool = journaled_call_tool
    mcp_manager._journal_patched = True
    logger.info("Journal: MCPManager.call_tool instrumented")


# Contextvars-зеркала (импортируются из recorder для скорости)
from .recorder import (  # noqa: E402
    _cv_session as jr_ctx_session,
    _cv_task as jr_ctx_task,
    _cv_trace as jr_ctx_trace,
    _cv_agent as jr_ctx_agent,
    _cv_loop as jr_ctx_loop,
    _cv_iter as jr_ctx_iter,
)


# ═════════════════════════════════════════════════════════════
# 2. Помощники для Orchestrator.handle
# ═════════════════════════════════════════════════════════════
async def journal_task_start(
    query: str, agents: list[str], model: str, session_id: str = "",
    task_id: str = "", trace_id: str = "",
) -> dict:
    """Создаёт task_start; возвращает {task_id, trace_id} для handle()."""
    rec = get_recorder()
    task_id = task_id or uuid.uuid4().hex[:16]
    trace_id = trace_id or task_id
    if rec is None:
        return {"task_id": task_id, "trace_id": trace_id}
    try:
        rec.start_task(
            task_id, trace_id=trace_id,
            session_id=session_id or jr_ctx_session.get(),
            query=query, agents=agents, model=model,
        )
    except Exception:
        logger.exception("journal task_start")
    return {"task_id": task_id, "trace_id": trace_id}


async def journal_task_end(
    task_id: str, status: str = "ok", error: str = "",
    meta: dict | None = None,
) -> None:
    rec = get_recorder()
    if rec is None:
        return
    try:
        rec.end_task(task_id, status=status, error=error, meta=meta)
    except Exception:
        logger.exception("journal task_end")


class _SyncToAsyncCtx:
    """Адаптер sync-контекст-менеджера → async (для `async with` в
    orchestrator._run_one; bugfix Этапа 5 — раньше при инициализированном
    журнале агент падал с TypeError)."""

    def __init__(self, cm):
        self._cm = cm

    async def __aenter__(self):
        return self._cm.__enter__()

    async def __aexit__(self, *exc):
        return self._cm.__exit__(*exc)


def journal_agent_context(agent_id: str):
    """Асинхронный контекст-менеджер для одного агента внутри handle().

    Без журнала — nullcontext (совместим и с `async with`)."""
    rec = get_recorder()
    if rec is None:
        import contextlib
        return contextlib.nullcontext()
    return _SyncToAsyncCtx(rec.context(agent_id=agent_id))


def journal_set_project_root(path: str) -> None:
    """Обновляет корень проекта в журнале (при смене папки через UI)."""
    rec = get_recorder()
    if rec is None:
        return
    try:
        from pathlib import Path as _P
        rec.project_root = _P(path).resolve()
        rec.record_system("project_root_changed", meta={"new_root": path})
    except Exception:
        logger.exception("journal set_project_root")


# ═════════════════════════════════════════════════════════════
# 3. WS-сессии
# ═════════════════════════════════════════════════════════════
def ws_session_start() -> str:
    """Регистрирует новую сессию; возвращает session_id для contextvar."""
    rec = get_recorder()
    session_id = uuid.uuid4().hex[:12]
    if rec is not None:
        try:
            rec.start_session(session_id, meta={"source": "websocket"})
        except Exception:
            logger.exception("journal session_start")
    return session_id


def ws_session_end(session_id: str, reason: str = "disconnected") -> None:
    rec = get_recorder()
    if rec is None:
        return
    try:
        rec.end_session(session_id, reason=reason)
    except Exception:
        logger.exception("journal session_end")


# ═════════════════════════════════════════════════════════════
# 4. Инициализация в lifespan (src/main.py)
# ═════════════════════════════════════════════════════════════
def init_journal(base_dir, project_root: str = ""):
    """Создаёт JournalRecorder и все подсистемы. Возвращает recorder.

    Вызывается ОДИН раз в lifespan до MCPManager.
    """
    from pathlib import Path as _P
    from .config import JournalConfig
    from .recorder import JournalRecorder

    cfg = JournalConfig.from_env(
        base_dir=_P(base_dir) / "data" / "journal")
    try:
        # Наложить секцию journal: из settings.yaml, если есть
        from src.config import get_yaml_config
        section = (get_yaml_config() or {}).get("journal")
        if isinstance(section, dict):
            cfg.apply_yaml(section)
    except Exception:
        pass

    recorder = JournalRecorder(cfg, project_root=project_root or "")
    set_recorder(recorder)
    recorder.startup()
    logger.info(
        "Journal готов: %s (событий в базе: %d)",
        recorder.cfg.db_path, recorder.store.stats()["total_events"],
    )
    return recorder


def attach_to_main(app, state: dict, include_router: bool = True) -> None:
    """Подключает HTTP-роутер журнала + фоновые задачи.

    Вызывается в lifespan после инициализации recorder и mcp_manager:
        attach_to_main(app, state)

    include_router=False — роутер /api/journal/* монтируется отдельно
    (Этап 5: фичей features/journal через FeatureLoader), здесь остаются
    только фоновые задачи (ретенция, watcher).
    """
    if include_router:
        from .api import build_router
        try:
            app.include_router(build_router())
        except Exception:
            logger.exception("journal router attach")

    rec = state.get("journal")
    if rec is None:
        return

    # Ретенция — фоновый поток
    try:
        from .retention import RetentionManager, start_background_sweep
        manager = RetentionManager(rec.cfg, store=rec.store)
        state["journal_retention"] = manager
        start_background_sweep(rec.cfg, manager)
    except Exception:
        logger.exception("journal retention start")

    # Watcher внешних изменений (опционально)
    if rec.cfg.watch_project:
        try:
            rec.start_watcher()
        except Exception:
            logger.exception("journal watcher start")


def shutdown_journal(state: dict) -> None:
    rec = state.get("journal")
    if rec is None:
        return
    try:
        rec.shutdown()
    except Exception:
        logger.exception("journal shutdown")
    try:
        set_recorder(None)   # гигиена: глобальная ссылка не переживает остановку
    except Exception:
        pass

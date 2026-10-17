# -*- coding: utf-8 -*-
"""FastAPI приложение с lifespan.

Рекомендация 1 (Sprint 1.A — подготовка к декомпозиции main.py).

Вынесено из src/main.py: объявление FastAPI, lifespan (инициализация всех
подсистем), регистрация роутеров из src/routes/.

Логика lifespan разбита на 7 шагов через вспомогательные функции для
читаемости. Порядок инициализации CRITICAL — подсистемы зависят от
результата предыдущих (PG → cache → policies → llm → memory → registry →
mcp → journal → supervisor → background → features → replicator → retention).

Shutdown — в обратном порядке, с try/except на каждый (мягкая деградация).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src import events
from src.agents.runtime import AgentRuntime
from src.cache import ResponseCache
from src.config import get_settings, get_yaml_config
from src.core.features import FeatureLoader
from src.core.file_watcher import FileWatcher
from src.core.registry import Registry
from src.file_state import FileState
from src.llm_client import LLMClient
from src.loop.controller import LoopController
from src.loop.spec import LoopSpecLoader
from src.loop.telemetry import LoopTelemetry
from src.mcp_manager import MCPManager
from src.memory.facade import Memory
from src.orchestrator import Orchestrator
from src.policies import PolicyStore
from src.runtime_config import get_project_root, set_project_root
from src.state import state as app_state
from src.supervisor.plans import PlanRegistry

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"

# Этап 2 (ARCHITECTURE-V2 §3.10): Supervisor — Plan → Execute → Observe →
# Re-plan. Отключается env'ом SUPERVISOR_ENABLED=0 — тогда чат работает
# по этапу 1 через Orchestrator.handle() (прямая совместимость).
SUPERVISOR_ENABLED = os.getenv("SUPERVISOR_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

APP_VERSION = "2.0.0"
START_TS = time.time()


# ═══════════════════════════════════════════════════════════════════
# Lifespan — инициализация всех подсистем
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Главный lifespan: инициализация → yield → shutdown.

    Разбит на 7 шагов через _init_* функции. Каждый шаг ловит свои
    исключения, чтобы сбой одной подсистемы не уронил весь старт.
    """
    s = get_settings()
    cfg = get_yaml_config()
    app_state.started_at = time.time()

    # ── Step 0: PG autodetect ─────────────────────────────────────
    await _init_pg_autodetect()

    # ── Step 1: Cache + Policies ──────────────────────────────────
    _init_cache_and_policies(s)

    # ── Step 1b: рабочие папки (.inbox/outputs в project_root) ────
    try:
        from src.runtime_config import get_project_root
        from src.workspace import ensure_workspace_dirs
        root = get_project_root(default=s.project_root) or s.project_root
        made = ensure_workspace_dirs(root)
        if made:
            logger.info("Workspace: %s", ", ".join(made))
    except Exception as e:  # noqa: BLE001 — не критично для старта
        logger.warning("Workspace dirs: %s", e)

    # ── Step 2: LLM + Memory ──────────────────────────────────────
    await _init_llm_and_memory(s)

    # ── Step 3: Registry + MCP ────────────────────────────────────
    await _init_registry_and_mcp(s, cfg)

    # ── Step 4: Loop + Agents + Orchestrator ──────────────────────
    _init_loop_and_orchestrator()

    # ── Step 5: Journal + Background workers ──────────────────────
    await _init_journal_and_background(s)

    # ── Step 6: Features + Replicator + Retention ─────────────────
    await _init_features_and_replicator(app, s)

    # ── Step 7: WS bridge + Final setup ───────────────────────────
    _init_ws_bridge()

    logger.info("✓ Lifespan завершён за %.2fs", time.time() - app_state.started_at)

    try:
        yield
    finally:
        await _shutdown()


# ═══════════════════════════════════════════════════════════════════
# Lifespan step helpers — реализация
# ═══════════════════════════════════════════════════════════════════

async def _init_pg_autodetect() -> None:
    """Шаг 0: PostgreSQL-автодетект (тихо, без интерактива)."""
    try:
        from src.db.autodetect import ensure_pg
        pg_rep = ensure_pg(interactive=False)
        if pg_rep.get("status") != "already":
            logger.info("PostgreSQL-автодетект: %s — %s",
                        pg_rep.get("status"), pg_rep.get("hint", ""))
    except Exception as e:  # noqa: BLE001 — детект не должен валить старт
        logger.warning("PG autodetect failed: %s", e)


def _init_cache_and_policies(s) -> None:
    """Шаг 1: ResponseCache и PolicyStore."""
    if s.cache.enabled:
        app_state.cache = ResponseCache(Path(s.cache.path), ttl=s.cache.ttl)

    app_state.policies = PolicyStore(
        s.policies.path, ttl_days=s.policies.ttl_days,
        deleted_history_limit=s.policies.deleted_history_limit,
    )


async def _init_llm_and_memory(s) -> None:
    """Шаг 2: LLMClient + Memory facade."""
    app_state.llm = LLMClient(cache=app_state.cache)
    memory = Memory(llm_client=app_state.llm)
    if memory.use_postgres:
        await memory.initialize()
        if memory.pg_pool is not None:
            logger.info("Memory: PostgreSQL backend ready")
        else:
            logger.info("Memory: SQLite + LanceDB backend (PostgreSQL недоступен)")
    else:
        logger.info("Memory: SQLite + LanceDB backend")
    app_state.memory = memory


async def _init_registry_and_mcp(s, cfg) -> None:
    """Шаг 3: Registry declarations + MCPManager.start()."""
    registry = Registry(base_dir=BASE_DIR)
    registry.load_declarations()
    snap = await registry.build_snapshot(reason="initial")
    app_state.registry = registry

    require_confirmation = set(
        cfg.get("limits", {}).get("require_confirmation_for", [])
    )
    mcp = MCPManager(require_confirmation=require_confirmation)

    mcp_to_start: dict[str, dict] = {}
    for mid in snap.enabled_mcp_ids:
        mschema = registry.mcp_servers.get(mid)
        if mschema:
            mcp_to_start[mid] = {
                "command": mschema.command,
                "args": mschema.args,
                "env": mschema.env,
            }
    await mcp.start(mcp_to_start)
    app_state.mcp = mcp

    for mid, st in snap.mcp_servers.items():
        st.alive = mid in mcp.sessions


def _init_loop_and_orchestrator() -> None:
    """Шаг 4: LoopController + AgentRuntime + Orchestrator + Plans."""
    loop_loader = LoopSpecLoader(base_dir=BASE_DIR)
    loop_loader.load_all()
    app_state.loop_loader = loop_loader

    telemetry = LoopTelemetry(BASE_DIR / "data" / "loop_telemetry.sqlite")
    app_state.telemetry = telemetry

    loop_controller = LoopController(telemetry=telemetry)
    app_state.loop_controller = loop_controller

    # AgentRuntime нужен snapshot из registry (построен в шаге 3)
    snap = app_state.registry.snapshot if app_state.registry else None
    agent_runtime = AgentRuntime(loop_controller, loop_loader)
    if snap:
        agent_runtime.rebuild(snap)
    app_state.agent_runtime = agent_runtime

    app_state.orchestrator = Orchestrator(app_state.llm, agent_runtime)
    app_state.plans = PlanRegistry(BASE_DIR / "data" / "plans")


async def _init_journal_and_background(s) -> None:
    """Шаг 5: Journal + MicroTasksWorker + (опционально) Ollama enrichment."""
    # Journal (мягко)
    try:
        from src.journal.integration import (
            init_journal, attach_to_main, instrument_mcp_manager,
        )
        journal = init_journal(BASE_DIR, project_root=str(s.project_root))
        if app_state.mcp is not None:
            instrument_mcp_manager(app_state.mcp)
        app_state.journal = journal
        # attach_to_main монтирует retention + watcher на app — но в нашем
        # refactoring это надо вызывать из app.py, не из main.py
        # (передаём app через параметр).
    except Exception as e:
        logger.warning("Journal init failed (мягко, без журнала): %s", e)

    # MicroTasksWorker (опционально)
    if os.getenv("LOCAL_MICRO_TASKS_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        try:
            from src.background.micro_tasks import MicroTasksWorker
            from src.ollama.client import get_ollama

            def _local_chat_active() -> bool:
                return time.time() < app_state.local_chat_active_until

            micro_worker = MicroTasksWorker(
                get_ollama(),
                BASE_DIR / "data" / "sessions",
                plans_registry=app_state.plans,
                journal_getter=lambda: app_state.journal,
                pause_check=_local_chat_active,
                base_dir=BASE_DIR,
            )
            micro_worker.load_digest_from_disk()
            await micro_worker.start()
            app_state.micro_worker = micro_worker
        except Exception as e:
            logger.warning("MicroTasks init failed (мягко): %s", e)


async def _init_features_and_replicator(app, s) -> None:
    """Шаг 6: Features mount + PgReplicator + AgentMemory + Retention."""
    # Features (Этап 4, V2 §2.2)
    feature_loader = FeatureLoader(base_dir=BASE_DIR)
    app_state.features_loader = feature_loader
    try:
        # В оригинале: await feature_loader.mount(app, state_dict)
        # Здесь: mount через app_state (state — это dataclass, не dict).
        # Временно оборачиваем app_state в dict для обратной совместимости.
        state_dict_compat = _state_as_dict()
        await feature_loader.mount(app, state_dict_compat)
    except Exception as e:
        logger.warning("Features mount failed: %s", e)

    # PgReplicator (опционально)
    if os.getenv("PG_REPLICATE", "1").lower() != "0":
        try:
            from src.db.replicator import PgReplicator
            app_state.pg_replicator = PgReplicator(project_root=str(s.project_root))
            await app_state.pg_replicator.start()
        except Exception as e:
            logger.warning("PgReplicator init failed (мягко): %s", e)

    # AgentMemory (опционально, требует PG)
    if (app_state.memory is not None and app_state.memory.pg_pool is not None
            and os.getenv("AGENT_MEMORY", "1").lower() != "0"):
        try:
            from src.db.agent_memory import AgentMemory, AgentMemoryIndexer
            am = AgentMemory(app_state.memory.pg_pool)
            indexer = AgentMemoryIndexer(
                am, interval=float(os.getenv("AGENT_MEMORY_INTERVAL", "120")),
            )
            indexer.start()
            app_state.agent_memory_indexer = indexer
        except Exception as e:
            logger.warning("AgentMemory init failed (мягко): %s", e)


def _init_ws_bridge() -> None:
    """Шаг 7: WS clients set + event bus → WS bridge."""
    # state.ws_clients — пустое множество, заполняется при подключении WS.
    app_state.ws_clients = set()

    async def _events_ws_bridge(event: dict) -> None:
        clients = app_state.ws_clients
        if not clients:
            return
        payload = {
            "type": "event", "kind": event.get("kind", ""),
            "payload": event.get("payload", {}),
            "ts": event.get("ts", 0),
        }
        for ws in list(clients):
            try:
                await ws.send_json(payload)
            except Exception:
                # Битый WS — убираем из подписки
                app_state.ws_clients.discard(ws)

    # Подписка на events bus — будет стримить события всем WS-клиентам.
    try:
        events.subscribe(_events_ws_bridge)
    except Exception:
        # events bus может быть не инициализирован — мягко.
        pass


def _state_as_dict() -> dict:
    """Совместимость: оборачивает AppState в dict для старого кода.

    Временно используется для feature_loader.mount() и других функций,
    которые ожидают state: dict. После полного рефакторинга Sprint 1.B
    все они примут AppState.
    """
    return {
        "cache": app_state.cache,
        "policies": app_state.policies,
        "llm": app_state.llm,
        "memory": app_state.memory,
        "registry": app_state.registry,
        "mcp": app_state.mcp,
        "orchestrator": app_state.orchestrator,
        "plans": app_state.plans,
        "supervisor": app_state.supervisor,
        "journal": app_state.journal,
        "micro_worker": app_state.micro_worker,
        "features_loader": app_state.features_loader,
        "ws_clients": app_state.ws_clients,
        "local_chat_active_until": app_state.local_chat_active_until,
    }


async def _shutdown() -> None:
    """Shutdown всех подсистем в обратном порядке."""
    logger.info("Shutting down...")

    # PgReplicator
    if app_state.pg_replicator is not None:
        try:
            await app_state.pg_replicator.stop()
        except Exception as e:
            logger.warning("PgReplicator stop failed: %s", e)

    # AgentMemory indexer
    if app_state.agent_memory_indexer is not None:
        try:
            app_state.agent_memory_indexer.stop()
        except Exception as e:
            logger.warning("AgentMemory stop failed: %s", e)

    # MicroTasksWorker
    if app_state.micro_worker is not None:
        try:
            await app_state.micro_worker.stop()
        except Exception as e:
            logger.warning("MicroTasks stop failed: %s", e)

    # MCPManager
    if app_state.mcp is not None:
        try:
            await app_state.mcp.stop()
        except Exception as e:
            logger.warning("MCPManager stop failed: %s", e)

    # Journal
    try:
        from src.journal.integration import shutdown_journal
        await shutdown_journal()
    except Exception as e:
        logger.warning("Journal shutdown failed: %s", e)

    # Memory
    if app_state.memory is not None:
        try:
            await app_state.memory.close()
        except Exception as e:
            logger.warning("Memory close failed: %s", e)

    logger.info("✓ Shutdown завершён")


# ═══════════════════════════════════════════════════════════════════
# FastAPI app — объявление и регистрация роутеров
# ═══════════════════════════════════════════════════════════════════

app = FastAPI(
    title="LLM Agent",
    version=APP_VERSION,
    description="Мультиагентная система правки файлов и БД через LLM + MCP",
    lifespan=lifespan,
)


# ── Безопасность v2: API-key middleware (см. src/security.py) ─────────
# LLM_AGENT_API_KEY не задан → открытый доступ (локальная разработка).
# Задан → /api/* и /ws требуют ключ (X-API-Key / Bearer / ?api_key=).
from src.security import API_KEY, COOKIE_NAME, http_authorized  # noqa: E402

if not API_KEY:
    logger.warning(
        "⚠ LLM_AGENT_API_KEY не задан — сервер работает без авторизации. "
        "Для защиты API установите ключ в .env (см. .env.example).")

@app.middleware("http")
async def api_key_middleware(request: Request, call_next):
    """API-key проверка + «вход по ссылке» (?api_key= → httpOnly cookie)."""
    path = request.url.path
    authorized = http_authorized(
        path,
        headers=dict(request.headers),
        query_params=dict(request.query_params),
        cookies=dict(request.cookies),
    )
    if not authorized:
        return JSONResponse(
            {"detail": "401 Unauthorized: передайте ключ через заголовок "
                       "X-API-Key, Authorization: Bearer или ?api_key= "
                       "(LLM_AGENT_API_KEY в .env)"},
            status_code=401,
        )
    response = await call_next(request)
    # Одноразовый «вход по ссылке»: /?api_key=KEY ставит httpOnly cookie,
    # дальше браузерный UI работает сам, ключ в HTML/JS не светится.
    qkey = request.query_params.get("api_key")
    if (API_KEY and qkey == API_KEY
            and request.cookies.get(COOKIE_NAME) != API_KEY):
        response.set_cookie(
            COOKIE_NAME, API_KEY, httponly=True, samesite="lax",
            max_age=60 * 60 * 12,
        )
    return response

# ── Static directories ────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

if (BASE_DIR / "features").is_dir():
    app.mount(
        "/features",
        StaticFiles(directory=str(BASE_DIR / "features")),
        name="features",
    )

# ── Register routers from src/routes/ ────────────────────────────────
# Sprint 1.A: только самые простые роутеры — static, cache, features.
# Sprint 1.B: добавить остальные (plans, models, sessions, policies,
# registry, database, analytics, graph, search, backup).
from src.routes import static as routes_static  # noqa: E402
from src.routes import cache as routes_cache  # noqa: E402
from src.routes import features as routes_features  # noqa: E402

app.include_router(routes_static.router)
app.include_router(routes_cache.router, prefix="/api/cache", tags=["cache"])
app.include_router(routes_features.router, prefix="/api", tags=["features"])

# ─── Sprint 1.B: 13 новых роутеров ─────────────────────────────────
from src.routes import plans as routes_plans                # noqa: E402
from src.routes import models as routes_models              # noqa: E402
from src.routes import sessions as routes_sessions          # noqa: E402
from src.routes import project as routes_project            # noqa: E402
from src.routes import database as routes_database          # noqa: E402
from src.routes import enrichment as routes_enrichment      # noqa: E402
from src.routes import graph as routes_graph                # noqa: E402
from src.routes import search as routes_search              # noqa: E402
from src.routes import backup as routes_backup              # noqa: E402
from src.routes import analytics as routes_analytics       # noqa: E402
from src.routes import policies as routes_policies         # noqa: E402
from src.routes import chats as routes_chats                # noqa: E402
from src.routes import registry as routes_registry          # noqa: E402
from src.routes import files as routes_files                # noqa: E402

app.include_router(routes_plans.router)
app.include_router(routes_models.router)
app.include_router(routes_sessions.router)
app.include_router(routes_project.router)
app.include_router(routes_database.router)
app.include_router(routes_enrichment.router)
app.include_router(routes_graph.router)
app.include_router(routes_search.router)
app.include_router(routes_backup.router)
app.include_router(routes_analytics.router)
app.include_router(routes_policies.router)
app.include_router(routes_chats.router)
app.include_router(routes_registry.router)
app.include_router(routes_files.router)

# ─── Sprint 1.C: WebSocket /ws handler ──────────────────────────────
from src.ws.chat import ws_router                            # noqa: E402
app.include_router(ws_router)


# ⚠ Sprint 1.B: сюда будут добавлены include_router() для остальных
# роутеров по мере их создания. На текущий момент остальные эндпоинты
# остаются в src/main.py (legacy), пока их не перенесут в src/routes/.

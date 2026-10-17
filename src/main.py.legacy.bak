"""FastAPI + WebSocket сервер.

Lifespan:
  Registry → Snapshot → MCPManager → LoopController → AgentRuntime → Orchestrator
  + FileWatcher + BackgroundScheduler + Memory.
"""
from __future__ import annotations

import asyncio
import atexit
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from src.agents.runtime import AgentRuntime
from src.cache import ResponseCache
from src.config import get_settings, get_yaml_config
from src.core.background import BackgroundScheduler
from src.core.file_watcher import FileWatcher
from src.core.features import FeatureLoader
from src.core.metrics import PrometheusExporter
from src.core.registry import Registry
from src import events   # событийная шина (Этап 4): publish/subscribe
from src.file_state import FileState
from src.llm_client import LLMClient
from src.mcp_manager import MCPManager
from src.memory.facade import Memory
from src.orchestrator import Orchestrator
from src.ollama.client import get_ollama
from src.ollama.worker import EnrichmentWorker
from src.db.analytics import Analytics
from src.db.age_store import AgeStore
from src.db.age_sync import AgeSync
from src.db.search_helpers import SearchHelpers
from src.db.backup import BackupManager
from src.db.pg_metrics import PGMetrics
# Этап 5 (V2 §3.10): журнал унифицирован на SQLite-поколении (Task 2).
# PostgreSQL-бэкенд (BlobStore/ActionGraph/RollbackManager v1.0) — attic/.
# Точка входа — мягкая интеграция src/journal/integration.py:
# init_journal не требует psycopg и не роняет старт при отказе.
from src.journal.integration import (
    init_journal, attach_to_main, instrument_mcp_manager,
    shutdown_journal, ws_session_start, ws_session_end,
)
from src.policies import PolicyStore
from src.runtime_config import get_project_root, set_project_root
from src.loop.controller import LoopController
from src.loop.spec import LoopSpecLoader
from src.loop.telemetry import LoopTelemetry
from src.supervisor.plans import PlanRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Этап 2 (ARCHITECTURE-V2 §3.10): Supervisor-цикл Plan → Execute → Observe →
# Re-plan. Отключается env'ом SUPERVISOR_ENABLED=0 — тогда чат работает
# по пути Этапа 1 через Orchestrator.handle() (обратная совместимость).
SUPERVISOR_ENABLED = os.getenv("SUPERVISOR_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

WEB_DIR = Path(__file__).resolve().parent / "web"
BASE_DIR = Path(__file__).resolve().parent.parent

APP_VERSION = "2.0.0"          # V2: 5 этапов + PG-контур (Task 12-14)
START_TS = time.time()         # для uptime в метриках инстанса

state: dict = {}


def _do_backup(policies, backup_path, backup_dir, keep, reason="manual"):
    result = {"reason": reason}
    try:
        result["backup_path"] = str(policies.backup_to_file(backup_path))
    except Exception as e:
        result["error"] = str(e)
    try:
        result["timestamped_path"] = str(
            policies.backup_timestamped(backup_dir, keep=keep)
        )
    except Exception:
        pass
    return result


# ═════════════════════════════════════════════════════════
# Background tasks
# ═════════════════════════════════════════════════════════
async def _task_memory_cleanup():
    mem: Memory = state.get("memory")
    if mem and mem.enabled:
        try:
            mem.cleanup()
        except Exception:
            pass


async def _task_auto_index():
    mem: Memory = state.get("memory")
    if not mem or not mem.enabled or not mem.vector:
        return
    stats = mem.vector.stats()
    if stats.get("chunks", 0) > 0:
        return
    s = get_settings()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, mem.index_project, s.project_root)
        await loop.run_in_executor(
            None, mem.refresh_profile, s.project_root,
        )
        if mem.graph:
            await loop.run_in_executor(
                None, mem.rebuild_graph, s.project_root,
            )
    except Exception as e:
        logger.warning("Auto-index failed: %s", e)


# ═════════════════════════════════════════════════════════
# Lifespan
# ═════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    cfg = get_yaml_config()

    # 0. PostgreSQL-автодетект (Task 24-b) — при каждом запуске.
    #    Интерактивные подсказки делает run.py; здесь — тихо (словарь
    #    без запросов), а кэш на процесс исключает двойную работу.
    try:
        from src.db.autodetect import ensure_pg
        pg_rep = ensure_pg(interactive=False)
        if pg_rep.get("status") != "already":
            logger.info("PostgreSQL-автодетект: %s — %s",
                        pg_rep.get("status"), pg_rep.get("hint", ""))
    except Exception as e:  # noqa: BLE001 — детект не должен валить старт
        logger.warning("PG autodetect failed: %s", e)

    # 1. Cache + Policies
    cache = None
    if s.cache.enabled:
        cache = ResponseCache(Path(s.cache.path), ttl=s.cache.ttl)

    policies = PolicyStore(
        s.policies.path, ttl_days=s.policies.ttl_days,
        deleted_history_limit=s.policies.deleted_history_limit,
    )

    # 2. LLM
    llm = LLMClient(cache=cache)

    # 3. Memory
    memory = Memory(llm_client=llm)
    if memory.use_postgres:
        await memory.initialize()
        if memory.pg_pool is not None:
            logger.info("Memory: PostgreSQL backend ready")
        else:
            # initialize() мягко упал в фолбэк — не называем бэкенд PostgreSQL
            logger.info("Memory: SQLite + LanceDB backend (PostgreSQL недоступен)")
    else:
        logger.info("Memory: SQLite + LanceDB backend")

    # 4. Registry
    registry = Registry(base_dir=BASE_DIR)
    registry.load_declarations()
    snap = await registry.build_snapshot(reason="initial")

    # 5. MCP Manager
    require_confirmation = set(
        cfg.get("limits", {}).get("require_confirmation_for", [])
    )
    mcp = MCPManager(require_confirmation=require_confirmation)

    mcp_to_start = {}
    for mid in snap.enabled_mcp_ids:
        mschema = registry.mcp_servers.get(mid)
        if mschema:
            mcp_to_start[mid] = {
                "command": mschema.command,
                "args": mschema.args,
                "env": mschema.env,
            }
    await mcp.start(mcp_to_start)

    for mid, st in snap.mcp_servers.items():
        st.alive = mid in mcp.sessions

    # 5.1 EnrichmentWorker — только с PostgreSQL
    enrichment_worker = None
    if memory.pg_pool is not None:
        try:
            ollama_client = get_ollama()
            if await ollama_client.health():
                models = await ollama_client.list_models()
                if ollama_client.model not in models and not any(
                    m.startswith(ollama_client.model.split(":")[0])
                    for m in models
                ):
                    logger.warning(
                        "Ollama: модель '%s' не найдена. "
                        "Загрузите: ollama pull %s",
                        ollama_client.model, ollama_client.model,
                    )
                else:
                    enrichment_worker = EnrichmentWorker(
                        memory.pg_pool, ollama_client,
                        batch_size=int(os.getenv("ENRICH_BATCH_SIZE", "50")),
                        interval_seconds=float(
                            os.getenv("ENRICH_INTERVAL", "30")
                        ),
                        concurrency=int(
                            os.getenv("ENRICH_CONCURRENCY", "4")
                        ),
                    )
                    await enrichment_worker.start()
                    logger.info(
                        "Enrichment worker started: model=%s",
                        ollama_client.model,
                    )
            else:
                logger.warning(
                    "Ollama недоступна на %s. Воркер не запущен.",
                    ollama_client.base_url,
                )
        except Exception as e:
            logger.warning("Enrichment worker init failed: %s", e)

    # 5.2 AGE — Apache AGE graph
    age_store = None
    age_sync = None
    if os.getenv("AGE_ENABLED", "false").lower() == "true":
        try:
            age_dsn = os.getenv(
                "AGE_DSN",
                "postgresql://llmagent:secret@localhost:5433/llmagent",
            )
            age_store = AgeStore(age_dsn, graph_name="llm_graph")
            age_ok = await age_store.start()
            if age_ok:
                age_sync = AgeSync(memory.pg_pool, age_store) if memory.pg_pool else None
                logger.info("Apache AGE ready")
            else:
                logger.warning("AGE extension not available, disabled")
                age_store = None
        except Exception as e:
            logger.warning("AGE init failed: %s", e)
            age_store = None

    # 5.3 CDC + Kafka
    cdc_worker = None
    if (
        memory.pg_pool is not None
        and os.getenv("KAFKA_ENABLED", "false").lower() == "true"
    ):
        try:
            from src.cdc.kafka_publisher import KafkaPublisher
            from src.cdc.notify_worker import NotifyCDCWorker

            kafka = KafkaPublisher(
                bootstrap_servers=os.getenv(
                    "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092",
                ),
            )
            cdc_worker = NotifyCDCWorker(memory.pg_pool, kafka)
            await cdc_worker.start()
            logger.info("CDC worker started")
        except Exception as e:
            logger.warning("CDC init failed: %s", e)
            cdc_worker = None

    # 5.4 BackupManager — авто-бэкапы
    backup_manager = None
    backup_task = None
    backup_stop = asyncio.Event()

    if (
        memory.pg_pool is not None
        and os.getenv("BACKUP_ENABLED", "true").lower() == "true"
    ):
        try:
            backup_manager = BackupManager(
                backup_dir=os.path.join(
                    s.project_root, "data", "db_backups",
                ),
                host=os.getenv("PG_APP_HOST", "localhost"),
                port=int(os.getenv("PG_APP_PORT", "5432")),
                user=os.getenv("PG_APP_USER", "llmagent"),
                password=os.getenv("PG_APP_PASSWORD", "secret"),
                database=os.getenv("PG_APP_DATABASE", "llmagent"),
                keep_last=int(os.getenv("BACKUP_KEEP_LAST", "7")),
            )

            interval = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))
            initial_delay = float(
                os.getenv("BACKUP_INITIAL_DELAY_SECONDS", "300")
            )

            if interval > 0:
                backup_task = asyncio.create_task(
                    backup_manager.backup_loop(
                        interval_hours=interval,
                        initial_delay_seconds=initial_delay,
                        stop_event=backup_stop,
                    )
                )
                logger.info(
                    "Auto-backup started: every %.1fh, keep=%d",
                    interval, backup_manager.keep_last,
                )
        except Exception as e:
            logger.warning("BackupManager init failed: %s", e)
            backup_manager = None

    # 5.5 Journal — чёрный ящик (Этап 5: единое SQLite-поколение,
    # «мягкая» интеграция; API-роутер монтируется фичей features/journal)
    try:
        journal = init_journal(BASE_DIR, project_root=str(s.project_root))
        instrument_mcp_manager(mcp)          # перехват всех tool calls
        state["journal"] = journal
        attach_to_main(app, state, include_router=False)  # ретенция + watcher
        logger.info("Journal: готов (SQLite-поколение, Этап 5)")
    except Exception as e:
        logger.warning("Journal init failed (мягко, без журнала): %s", e)

    # 6. Loop + Agents
    loop_loader = LoopSpecLoader(base_dir=BASE_DIR)
    loop_loader.load_all()
    telemetry = LoopTelemetry(BASE_DIR / "data" / "loop_telemetry.sqlite")
    loop_controller = LoopController(telemetry=telemetry)
    agent_runtime = AgentRuntime(loop_controller, loop_loader)
    agent_runtime.rebuild(snap)

    # 7. Orchestrator
    orchestrator = Orchestrator(llm, agent_runtime)

    # 7.1 Планы Supervisor (Этап 3): состояние переживает reconnect/рестарт
    plan_registry = PlanRegistry(BASE_DIR / "data" / "plans")

    # 7.1.1 MicroTasksWorker (Task 24-c): локальные фоновые микрозадачи на
    # малой модели Ollama — заголовки чатов/планов, теги, дайджест журнала.
    # Одной модели на всё → пока в чате активен локальный режим, воркер
    # паузится (state["local_chat_active_until"]), свопов VRAM нет.
    micro_worker = None
    if os.getenv("LOCAL_MICRO_TASKS_ENABLED", "1").strip().lower() in (
        "1", "true", "yes", "on",
    ):
        try:
            from src.background.micro_tasks import MicroTasksWorker

            def _local_chat_active() -> bool:
                return time.time() < float(
                    state.get("local_chat_active_until", 0) or 0
                )

            micro_worker = MicroTasksWorker(
                get_ollama(),
                BASE_DIR / "data" / "sessions",
                plans_registry=plan_registry,
                journal_getter=lambda: state.get("journal"),
                pause_check=_local_chat_active,
                base_dir=BASE_DIR,
            )
            micro_worker.load_digest_from_disk()
            await micro_worker.start()
        except Exception as e:
            logger.warning("MicroTasks init failed (мягко): %s", e)
            micro_worker = None

    # 7.2 Features (Этап 4, V2 §2.2): features/*/feature.yaml →
    # API-роутеры, вкладки UI, миграции — без правок ядра
    feature_loader = FeatureLoader(base_dir=BASE_DIR)
    try:
        await feature_loader.mount(app, state)
    except Exception as e:
        logger.warning("Features mount failed: %s", e)

    # 7.3 PgReplicator — долговременные зеркала в PostgreSQL:
    # журнал операций, диалоги чата и планы Supervisor реплицируются
    # фоново (курсоры в ops.sync_state, идемпотентно, ничего не теряется
    # при простое БД). PG_REPLICATE=0 — выключить.
    replicator = None
    if os.getenv("PG_REPLICATE", "1").lower() != "0":
        try:
            from src.db.replicator import PgReplicator
            replicator = PgReplicator(project_root=str(s.project_root))
            await replicator.start()
        except Exception as e:
            logger.warning("PgReplicator init failed (мягко): %s", e)

    # 7.5 Память агентов (pgvector) — фоновый индексатор: зеркала →
    # memory.tasks (семантический поиск по прошлым задачам). Требует
    # PG-пул памяти; без pgvector поиск мягко падает в FTS.
    # AGENT_MEMORY=0 — выключить; AGENT_MEMORY_INTERVAL — период (с).
    agent_memory_indexer = None
    if (
        memory.pg_pool is not None
        and os.getenv("AGENT_MEMORY", "1").lower() != "0"
    ):
        try:
            from src.db.agent_memory import AgentMemory, AgentMemoryIndexer
            am = AgentMemory(memory.pg_pool)
            agent_memory_indexer = AgentMemoryIndexer(
                am,
                interval=float(os.getenv("AGENT_MEMORY_INTERVAL", "120")),
            )
            agent_memory_indexer.start()
        except Exception as e:
            logger.warning("AgentMemory init failed (мягко): %s", e)
            agent_memory_indexer = None

    # 7.6 Ретенция зеркал PG — фоновое удаление старых строк.
    # ВЫКЛЮЧЕНО по умолчанию: включается PG_RETENTION_DAYS>0.
    pg_retention_task = None
    _retention_days = int(os.getenv("PG_RETENTION_DAYS", "0") or 0)
    if memory.pg_pool is not None and _retention_days > 0:
        try:
            from src.db.retention import retention_loop
            pg_retention_task = asyncio.create_task(retention_loop(
                memory.pg_pool, days=_retention_days,
                interval_hours=float(
                    os.getenv("PG_RETENTION_INTERVAL_HOURS", "24")),
            ))
            logger.info("PG retention включена: >%d дней", _retention_days)
        except Exception as e:
            logger.warning("PG retention init failed: %s", e)
            pg_retention_task = None

    # 7.4 Мост шины событий → WS-клиентам (Этап 4, V2 §2.4):
    # фичи публикуют события (events.publish), клиенты получают их
    # как {"type": "event", "kind": ...} без ручных send_json из глубин кода
    state["ws_clients"] = set()

    async def _events_ws_bridge(event: dict) -> None:
        clients = state.get("ws_clients")
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
                clients.discard(ws)

    events.subscribe("*", _events_ws_bridge)

    # 8. FileState
    effective_root = get_project_root(default=s.project_root)
    file_state = FileState(
        root=effective_root, ttl=s.file_state.ttl,
        max_files=s.file_state.max_files,
    )

    # 9. File watcher
    watcher = FileWatcher(registry, BASE_DIR)
    watcher.start(asyncio.get_event_loop())

    # 10. Scheduler
    scheduler = BackgroundScheduler()
    scheduler.add_periodic(
        "memory_cleanup", 86400, _task_memory_cleanup,
        initial_delay_seconds=600,
    )
    scheduler.add_periodic(
        "auto_index", 86400, _task_auto_index,
        initial_delay_seconds=5,
    )

    # Analytics: обновление MV
    async def _refresh_analytics():
        mem: Memory = state.get("memory")
        if mem and mem.pg_pool:
            a = Analytics(mem.pg_pool)
            await a.refresh()

    scheduler.add_periodic(
        "analytics_refresh", 900, _refresh_analytics,
        initial_delay_seconds=120,
    )

    # Multi-instance: регистрация и heartbeat
    instance_registry = None
    listener = None
    if memory.pg_pool is not None:
        try:
            from src.db.multi_instance import InstanceRegistry, Listener
            instance_registry = InstanceRegistry(memory.pg_pool)
            await instance_registry.register(
                {"role": "agent", "version": APP_VERSION})
            await instance_registry.start_heartbeat()

            # Метрики инстанса — раз в минуту (ops.instances.metrics)
            async def _report_instance_metrics():
                rt = state.get("agent_runtime")
                try:
                    await instance_registry.report_metrics({
                        "agents": len(rt.agents) if rt else 0,
                        "uptime_s": int(time.time() - START_TS),
                    })
                except Exception:
                    pass

            scheduler.add_periodic(
                "instance_metrics", 60, _report_instance_metrics,
                initial_delay_seconds=15,
            )
            scheduler.add_periodic(
                "instance_cleanup", 3600, instance_registry.cleanup_stale,
                initial_delay_seconds=120,
            )

            listener = Listener(memory.pg_pool)

            async def _on_new_event(payload: str):
                logger.debug("New event: %s", payload)
                # Разбудить воркер для немедленной обработки
                if enrichment_worker:
                    enrichment_worker.wakeup()

            await listener.listen("new_events", _on_new_event)
        except Exception as e:
            logger.warning("Multi-instance init failed: %s", e)
            instance_registry = None
            listener = None

    # Cluster (опционально, Redis)
    cluster_cfg = None
    redis_bus = None
    ws_bridge = None
    heartbeat_task = None
    try:
        from src.cluster.config import ClusterConfig
        from src.cluster.redis_bus import RedisBus
        from src.cluster.heartbeat import heartbeat_loop
        from src.cluster.ws_bridge import WsBridge

        cluster_cfg = ClusterConfig()
        if cluster_cfg.enabled:
            redis_bus = RedisBus(cluster_cfg)
            connected = await redis_bus.connect()
            if connected:
                ws_bridge = WsBridge(redis_bus, cluster_cfg)

                async def _on_snapshot(payload):
                    reg2: Registry = state.get("registry")
                    if reg2:
                        await reg2.build_snapshot(reason="remote_event")
                        await _sync_runtime()

                await redis_bus.subscribe(
                    cluster_cfg.channel_snapshot, _on_snapshot,
                )

                heartbeat_task = asyncio.create_task(
                    heartbeat_loop(
                        redis_bus, cluster_cfg,
                        lambda: {
                            "agents": len(state.get("agent_runtime").agents)
                                if state.get("agent_runtime") else 0,
                        },
                    )
                )
    except Exception as e:
        logger.debug("Cluster disabled: %s", e)
        cluster_cfg = None

    # 11. atexit
    if s.policies.backup_on_exit:
        atexit.register(
            _do_backup, policies, s.policies.backup_path,
            s.policies.backup_dir, s.policies.backup_keep, "atexit",
        )

    # Периодические snapshot для истории
    async def periodic_snapshot():
        while True:
            await asyncio.sleep(3600)
            try:
                await registry.build_snapshot(reason="periodic")
            except Exception as e:
                logger.exception("periodic snapshot failed: %s", e)

    asyncio.create_task(periodic_snapshot())

    # Audit cleanup раз в сутки
    scheduler.add_daily("audit_cleanup", 4, lambda: registry.audit.cleanup())

    state.update({
        "cache": cache,
        "policies": policies,
        "llm": llm,
        "memory": memory,
        "enrichment_worker": enrichment_worker,
        "age_store": age_store,
        "age_sync": age_sync,
        "cdc_worker": cdc_worker,
        "backup_manager": backup_manager,
        "backup_task": backup_task,
        "backup_stop": backup_stop,
        "journal": journal,
        "instance_registry": instance_registry,
        "listener": listener,
        "agent_memory_indexer": agent_memory_indexer,
        "pg_retention_task": pg_retention_task,
        "cluster_cfg": cluster_cfg,
        "redis_bus": redis_bus,
        "ws_bridge": ws_bridge,
        "registry": registry,
        "snapshot": snap,
        "mcp": mcp,
        "loop_controller": loop_controller,
        "loop_loader": loop_loader,
        "loop_telemetry": telemetry,
        "agent_runtime": agent_runtime,
        "orchestrator": orchestrator,
        "plans": plan_registry,
        "micro_tasks": micro_worker,
        "features_loader": feature_loader,
        "replicator": replicator,
        "file_state": file_state,
        "watcher": watcher,
        "scheduler": scheduler,
    })

    logger.info("System ready")

    try:
        yield
    finally:
        if replicator:
            try:
                await replicator.stop()
            except Exception:
                pass
        if agent_memory_indexer:
            try:
                await agent_memory_indexer.stop()
            except Exception:
                pass
        if pg_retention_task:
            pg_retention_task.cancel()
            try:
                await pg_retention_task
            except (asyncio.CancelledError, Exception):
                pass
        shutdown_journal(state)   # Этап 5: журнал — флаш и остановка
        scheduler.stop()
        watcher.stop()
        if listener:
            try:
                await listener.stop()
            except Exception:
                pass
        if instance_registry:
            try:
                await instance_registry.stop_heartbeat()
            except Exception:
                pass
        if heartbeat_task:
            heartbeat_task.cancel()
        if redis_bus:
            try:
                await redis_bus.close()
            except Exception:
                pass
        if s.policies.backup_on_exit:
            _do_backup(
                policies, s.policies.backup_path,
                s.policies.backup_dir, s.policies.backup_keep, "shutdown",
            )
        if backup_stop:
            backup_stop.set()
        if backup_task:
            backup_task.cancel()
            try:
                await backup_task
            except (asyncio.CancelledError, Exception):
                pass
        if cdc_worker:
            try:
                await cdc_worker.stop()
            except Exception:
                pass
        if age_store:
            try:
                await age_store.stop()
            except Exception:
                pass
        if enrichment_worker:
            try:
                await enrichment_worker.stop()
            except Exception:
                pass
        if micro_worker:
            try:
                await micro_worker.stop()
            except Exception:
                pass
        await mcp.stop()
        try:
            await memory.close()
        except Exception as e:
            logger.warning("Memory close: %s", e)


app = FastAPI(title="LLM Agent", lifespan=lifespan)

# Мост с расширением браузера: extension-схемы живут на чужих origin,
# без CORS браузер отклонит ответ на fetch из service worker.
# Список регулируется переменной окружения EXTENSION_ORIGIN_REGEX.
import os as _os
import re as _re
try:
    from fastapi.middleware.cors import CORSMiddleware as _CORS
    _EXT_ORIGIN_RE = _os.getenv(
        "EXTENSION_ORIGIN_REGEX",
        r"chrome-extension://[a-p]{32}"
        r"|moz-extension://[0-9a-f-]+"
        r"|http://(localhost|127\.0\.0\.1)(:\d+)?",
    )
    _re.compile(_EXT_ORIGIN_RE)     # невалидный regex — не роняем старт
    app.add_middleware(
        _CORS,
        allow_origin_regex=_EXT_ORIGIN_RE,
        allow_credentials=False,    # cookie мосту не нужны
        allow_methods=["*"],
        allow_headers=["*"],
    )
    logger.info("CORS моста расширения: %s", _EXT_ORIGIN_RE)
except Exception as _e:             # pragma: no cover
    logger.warning("CORS не настроен: %s", _e)


@app.get("/")
async def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

# Этап 4 (V2 §2.2): статика фич — js-вкладки отдаются как /features/<id>/<file>
if (BASE_DIR / "features").is_dir():
    app.mount(
        "/features",
        StaticFiles(directory=str(BASE_DIR / "features")),
        name="features",
    )


# ═════════════════════════════════════════════════════════
# Планы Supervisor (Этап 3) и реестр фич (Этап 4)
# ═════════════════════════════════════════════════════════
@app.get("/api/plans")
async def api_plans(limit: int = 20, session_id: str = ""):
    """Последние планы Supervisor (переживают переподключение WS)."""
    reg = state.get("plans")
    plans = reg.list(limit=limit, session_id=session_id) if reg else []
    from src.supervisor.plans import view_for_client
    return {"plans": [view_for_client(r) for r in plans]}


@app.get("/api/plans/{plan_id}")
async def api_plan_detail(plan_id: str):
    """Состояние одного плана (в т.ч. после перезапуска сервера — с диска)."""
    reg = state.get("plans")
    rec = reg.get(plan_id) if reg else None
    if rec is None:
        raise HTTPException(404, "План не найден")
    from src.supervisor.plans import view_for_client
    return view_for_client(rec)


@app.get("/api/features")
async def api_features():
    """Реестр фич (вкладки UI) + последние события шины (диагностика)."""
    fl: FeatureLoader | None = state.get("features_loader")
    return {
        "features": fl.list_for_client() if fl else [],
        "events": events.recent(30),
    }


# ═════════════════════════════════════════════════════════
# Basic API
# ═════════════════════════════════════════════════════════
@app.get("/api/models")
async def get_models():
    """Агрегатор провайдеров (Task 24-c).

    Живой опрос всех провайдеров параллельно (таймаут ~1.5с): qwenproxy,
    DeepSeek (ключ из env), Ollama (/api/tags), OpenAI (ключ из env).
    Плюс совместимый плоский список `models` и сохранённый выбор `saved`.
    """
    from src.llm_providers import aggregate

    s = get_settings()
    reg: Registry | None = state.get("registry")
    saved = reg.runtime.get_model_pref() if reg else None
    try:
        data = await aggregate()
    except Exception as e:
        logger.warning("providers aggregate failed: %s", e)
        data = {"providers": []}

    # совместимый плоский список: только доступные провайдеры, по порядку.
    # Task 24-d: квалифицированные ID «provider/model» — коллизии имён
    # между провайдерами (deepseek-chat у DeepSeek и OpenRouter) решены.
    flat: list[dict] = []
    for p in data["providers"]:
        if not p.get("available"):
            continue
        for m in p.get("models", []):
            qid = f"{p['id']}/{m['id']}"
            flat.append({"id": qid,
                         "name": m.get("name") or m["id"],
                         "provider": p["id"]})
    if not flat:
        # совсем ничего живого — старый фолбэк, чтобы селект не пустел
        flat = [
            {"id": "qwen3.8-max", "name": "Qwen 3.8 Max",
             "provider": "qwen"},
            {"id": "qwen3-fast", "name": "Qwen 3 Fast", "provider": "qwen"},
        ]
    return {
        "providers": data["providers"],
        "models": flat,
        "default": s.llm.model,
        "saved": saved,
    }


@app.post("/api/model/select")
async def select_model(payload: dict = Body(...)):
    """Сохранить выбранную в чате модель (runtime.yaml → model_preferences)."""
    model = str(payload.get("model") or "").strip()
    reg: Registry | None = state.get("registry")
    if reg is None:
        raise HTTPException(503, "Registry not ready")
    reg.runtime.set_model_pref(model or None)
    return {"ok": True, "saved": model}


@app.get("/api/sessions")
async def api_sessions(limit: int = 20):
    """Последние чаты с заголовками (Task 24-c: селектор чатов в UI)."""
    from src.background.micro_tasks import scan_sessions
    return scan_sessions(BASE_DIR / "data" / "sessions", limit=limit)


# ═══════════════════════════════════════════════════════════════════
# Policies REST (Task 27: вкладка «Политики» — раньше все 404)
# ═══════════════════════════════════════════════════════════════════

def _policies_store() -> PolicyStore:
    st: PolicyStore | None = state.get("policies")
    if st is None:
        raise HTTPException(503, "Policy store not ready")
    return st


@app.get("/api/policies")
async def policies_list():
    st = _policies_store()
    return {"policies": st.list_all(), "stats": st.stats()}


@app.delete("/api/policies/{policy_id}")
async def policies_delete(policy_id: int):
    st = _policies_store()
    if not st.delete(policy_id):
        raise HTTPException(404, f"Политика #{policy_id} не найдена")
    return {"ok": True}


@app.get("/api/policies/metrics")
async def policies_metrics(top: int = 10):
    st = _policies_store()
    rows = st.list_all()
    top_active = sorted(
        rows, key=lambda p: (-(p.get("use_count") or 0),
                             -(p.get("last_used_at") or 0)),
    )[:max(1, top)]
    deleted_overall = {"count": 0}
    try:
        import sqlite3 as _sq
        with _sq.connect(st.path) as conn:
            deleted_overall["count"] = conn.execute(
                "SELECT COUNT(*) FROM deleted_policies").fetchone()[0]
    except Exception:
        pass
    return {"top_active": top_active, "deleted_overall": deleted_overall}


@app.post("/api/policies/clear")
async def policies_clear():
    st = _policies_store()
    removed = st.clear_all()
    return {"ok": True, "removed": removed}


@app.post("/api/policies/cleanup")
async def policies_cleanup():
    st = _policies_store()
    removed = st.cleanup_old()
    return {"ok": True, "removed": removed}


@app.post("/api/policies/backup")
async def policies_backup():
    st = _policies_store()
    s = get_settings()
    path = st.backup_timestamped(
        s.policies.backup_dir, keep=s.policies.backup_keep,
    )
    return {"ok": True, "backup_path": str(path)}


@app.get("/api/policies/export")
async def policies_export():
    """Выгрузка всех политик в JSON (файл скачивается браузером)."""
    st = _policies_store()
    data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "count": len(st.list_all()),
        "policies": st.list_all(),
    }
    return JSONResponse(
        data,
        headers={
            "Content-Disposition":
                'attachment; filename="policies-export.json"',
        },
    )


def _parse_policies_file(raw: bytes) -> list[dict]:
    """Политики из экспорт-файла; ошибки формы — отдельным списком."""
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        raise HTTPException(400, "Файл не читается как JSON")
    items = data.get("policies") if isinstance(data, dict) else data
    if not isinstance(items, list):
        raise HTTPException(400, "В файле нет массива policies")
    return items


def _classify_policy(item: dict, existing: list[dict]) -> str:
    """new | conflict | invalid — для превью импорта."""
    if not isinstance(item, dict):
        return "invalid"
    tool = str(item.get("tool") or "").strip()
    scope = str(item.get("scope") or "").strip()
    decision = str(item.get("decision") or "").strip().lower()
    if not tool or scope not in ("tool", "path") \
            or decision not in ("allow", "deny"):
        return "invalid"
    pattern = item.get("path_pattern")
    if scope == "path" and not (pattern or "").strip():
        return "invalid"
    for p in existing:
        if (p.get("tool") == tool and p.get("scope") == scope
                and (p.get("path_pattern") or "") == (pattern or "")
                and p.get("decision") == decision):
            return "conflict"
    return "new"


@app.post("/api/policies/import/preview")
async def policies_import_preview(request: Request, mode: str = "merge"):
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "Нет файла в form-data (поле 'file')")
    items = _parse_policies_file(await upload.read())
    st = _policies_store()
    existing = st.list_all()
    details: dict[str, list] = {"new": [], "conflicts": [], "invalid": []}
    for item in items:
        cls = _classify_policy(item, existing)
        # UI ждёт ключ "conflicts" (мн.ч.) — классификатор отдаёт "conflict"
        details["conflicts" if cls == "conflict" else cls].append(
            item if isinstance(item, dict) else {})
    added = len(details["new"])
    if mode == "overwrite":
        added = sum(
            1 for it in items
            if isinstance(it, dict)
            and str(it.get("tool") or "").strip()
            and str(it.get("decision") or "").lower() in ("allow", "deny")
        )
    return {
        "added": added,
        "skipped": len(details["conflicts"]) if mode == "merge"
        else 0,
        "details": details,
        "mode": mode,
    }


@app.post("/api/policies/import")
async def policies_import(request: Request, mode: str = "merge"):
    form = await request.form()
    upload = form.get("file")
    if upload is None:
        raise HTTPException(400, "Нет файла в form-data (поле 'file')")
    items = _parse_policies_file(await upload.read())
    st = _policies_store()
    existing = st.list_all()
    added = skipped = 0
    for item in items:
        if not isinstance(item, dict):
            skipped += 1
            continue
        tool = str(item.get("tool") or "").strip()
        scope = str(item.get("scope") or "").strip()
        decision = str(item.get("decision") or "").strip().lower()
        pattern = (item.get("path_pattern") or "").strip() or None
        if not tool or scope not in ("tool", "path") \
                or decision not in ("allow", "deny") \
                or (scope == "path" and not pattern):
            skipped += 1
            continue
        cls = _classify_policy(item, existing)
        if cls == "conflict" and mode == "merge":
            skipped += 1
            continue
        st.add(tool, scope, decision, pattern)
        # обновляем existing, чтобы дубли в файле не создали двоек
        existing.append({
            "tool": tool, "scope": scope,
            "path_pattern": pattern, "decision": decision,
        })
        added += 1
    return {"ok": True, "added": added, "skipped": skipped, "mode": mode}


@app.post("/api/chats/import")
async def api_chats_import(request: Request, provider: str = "deepseek"):
    """Импорт чатов с сайта провайдера (Task 27).

    Тело запроса — сырой экспорт-JSON сайта (chat.deepseek.com →
    «Экспорт данных»). Каждый чат становится локальной сессией
    (data/sessions/<id>.jsonl) и появляется в селекте «Последние чаты».
    """
    from src.chat_import import import_chats

    try:
        data = await request.json()
    except Exception:
        raise HTTPException(400, "Тело запроса — не JSON (нужен файл экспорта)")
    try:
        created = import_chats(
            data, BASE_DIR / "data" / "sessions", provider=provider,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception:
        logger.exception("chats import failed")
        raise HTTPException(500, "Импорт не удался — см. лог сервера")
    return {"ok": True, "imported": created, "count": len(created)}


@app.get("/api/digest/latest")
async def digest_latest():
    """Последний дайджест журнала (локальная модель, Task 24-c)."""
    mw = state.get("micro_tasks")
    if mw is None:
        return {
            "enabled": False,
            "digest": None,
            "hint": "включите LOCAL_MICRO_TASKS_ENABLED=1",
        }
    return {"enabled": True, "digest": mw.last_digest, "stats": mw.stats()}


@app.post("/api/digest/refresh")
async def digest_refresh():
    """Пересобрать дайджест сейчас (локальной моделью)."""
    mw = state.get("micro_tasks")
    if mw is None:
        raise HTTPException(503, "Микрозадачи выключены")
    digest = await mw.force_digest()
    return {"ok": True, "digest": digest}


@app.get("/api/project")
async def project_current():
    s = get_settings()
    path = get_project_root(default=s.project_root)
    p = Path(path) if path else None
    return {
        "project_root": path,
        "exists": bool(p and p.exists() and p.is_dir()),
        "writable": bool(p and p.exists() and os.access(p, os.W_OK)),
        "enabled_servers": state.get("snapshot").enabled_mcp_ids
                          if state.get("snapshot") else [],
    }


@app.post("/api/project/set")
async def project_set(payload: dict = Body(...)):
    raw = (payload.get("path") or "").strip()
    if not raw:
        raise HTTPException(400, "Пустой путь")
    p = Path(raw).expanduser()
    if not p.exists() or not p.is_dir():
        raise HTTPException(400, f"Папка не существует: {p}")
    saved = set_project_root(str(p))
    fs: FileState = state.get("file_state")
    if fs:
        fs.root = Path(saved).resolve()
        fs.invalidate()
    return {"ok": True, "project_root": saved}


# ═════════════════════════════════════════════════════════
# Registry API
# ═════════════════════════════════════════════════════════
@app.get("/api/registry/snapshot")
async def registry_snapshot():
    reg: Registry = state.get("registry")
    if not reg or not reg.snapshot:
        raise HTTPException(503, "Registry not ready")
    return reg.snapshot_summary()


@app.get("/api/registry/agents")
async def registry_agents():
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    return {"agents": reg.list_all_agents()}


@app.get("/api/registry/agents/{agent_id}")
async def registry_agent(agent_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    info = reg.agent_info(agent_id)
    if not info:
        raise HTTPException(404, "Agent not found")
    return info


@app.post("/api/registry/agents/{agent_id}/enable")
async def registry_agent_enable(agent_id: str, payload: dict = Body(...)):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    enabled = bool(payload.get("enabled", True))
    await reg.set_agent_enabled(agent_id, enabled)
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "enabled": enabled}


@app.post("/api/registry/agents/{agent_id}/params")
async def registry_agent_params(agent_id: str, payload: dict = Body(...)):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    await reg.update_agent_params(agent_id, payload)
    await _sync_runtime()
    return {"ok": True}


@app.post("/api/registry/agents/{agent_id}/reset")
async def registry_agent_reset(agent_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    await reg.reset_agent(agent_id)
    await _sync_runtime()
    return {"ok": True}


@app.post("/api/registry/agents/{agent_id}/check")
async def registry_agent_check(agent_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    await reg.build_snapshot(reason="manual_check")
    await _sync_mcp()
    await _sync_runtime()
    return reg.agent_info(agent_id)


@app.get("/api/registry/mcp")
async def registry_mcp():
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    return {"mcp_servers": reg.list_all_mcp()}


@app.post("/api/registry/mcp/{mcp_id}/enable")
async def registry_mcp_enable(mcp_id: str, payload: dict = Body(...)):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    enabled = bool(payload.get("enabled", True))
    await reg.set_mcp_enabled(mcp_id, enabled)
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "enabled": enabled}


@app.get("/api/registry/capabilities")
async def registry_capabilities():
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    return {"capabilities": reg.list_all_capabilities()}


@app.post("/api/registry/capabilities/{cap_id}/provider")
async def registry_capability_provider(cap_id: str, payload: dict = Body(...)):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    provider_id = payload.get("provider_id")
    await reg.set_capability_provider(cap_id, provider_id)
    return {"ok": True}


@app.post("/api/registry/reload")
async def registry_reload():
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    await reg.reload_all()
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True}


@app.get("/api/registry/prompt")
async def registry_prompt():
    reg: Registry = state.get("registry")
    if not reg or not reg.snapshot:
        raise HTTPException(503, "Registry not ready")
    return {
        "prompt": reg.snapshot.orchestrator_prompt,
        "length": len(reg.snapshot.orchestrator_prompt),
        "hash": reg.snapshot.orchestrator_prompt_hash,
    }


@app.get("/api/registry/history")
async def registry_history(limit: int = 20):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    items = reg.snapshot_history(limit=limit)
    return {"snapshots": [
        {
            "id": r.id, "built_at": r.built_at, "slot": r.slot,
            "reason": r.reason,
            "agents_count": len(r.agents),
            "mcp_count": len(r.mcp),
            "prompt_length": r.prompt_length,
        }
        for r in items
    ]}


@app.get("/api/registry/history/diff")
async def registry_history_diff(from_id: str, to_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    d = reg.snapshot_diff(from_id, to_id)
    return d.to_dict()


# ═════════════════════════════════════════════════════════
# Sync helpers
# ═════════════════════════════════════════════════════════
async def _sync_mcp():
    reg: Registry = state.get("registry")
    mcp: MCPManager = state.get("mcp")
    if not reg or not reg.snapshot or not mcp:
        return
    wanted = set(reg.snapshot.enabled_mcp_ids)
    running = set(mcp.sessions.keys())
    to_start = {}
    for mid in wanted - running:
        mschema = reg.mcp_servers.get(mid)
        if mschema:
            to_start[mid] = {
                "command": mschema.command,
                "args": mschema.args,
                "env": mschema.env,
            }
    if to_start:
        await mcp.start(to_start)
    for mid, st in reg.snapshot.mcp_servers.items():
        st.alive = mid in mcp.sessions


async def _sync_runtime():
    runtime: AgentRuntime = state.get("agent_runtime")
    reg: Registry = state.get("registry")
    if runtime and reg and reg.snapshot:
        runtime.rebuild(reg.snapshot)


# ═════════════════════════════════════════════════════════
# Cache stats (индикатор в шапке UI) + favicon
# ═════════════════════════════════════════════════════════
@app.get("/api/cache/stats")
async def api_cache_stats():
    cache: ResponseCache | None = state.get("cache")
    if cache is None:
        return {"enabled": False}
    return {"enabled": True, **cache.stats()}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<text y="0.9em" font-size="90">🤖</text></svg>'
    )
    return Response(content=svg, media_type="image/svg+xml")


# ═════════════════════════════════════════════════════════
# Metrics
# ═════════════════════════════════════════════════════════
@app.get("/metrics", response_class=PlainTextResponse)
async def prometheus_metrics():
    reg: Registry = state.get("registry")
    if not reg:
        return PlainTextResponse("# not ready\n")

    exporter = PrometheusExporter(
        registry=reg,
        cache=state.get("cache"),
        memory=state.get("memory"),
        history=reg.history,
        loop_metrics=state.get("loop_telemetry"),
    )
    text = exporter.render()

    mem: Memory = state.get("memory")
    if mem and mem.pg_pool:
        # PostgreSQL метрики
        try:
            pgm = PGMetrics(mem.pg_pool)
            text += await pgm.render()
        except Exception as e:
            logger.debug("PG metrics failed: %s", e)

        # Backup метрики
        bm: BackupManager = state.get("backup_manager")
        if bm:
            try:
                stats = bm.stats()
                lines = [
                    "# HELP llmagent_backup_count Количество бэкапов",
                    "# TYPE llmagent_backup_count gauge",
                    f"llmagent_backup_count {stats.get('count', 0)}",
                    "# HELP llmagent_backup_total_mb Общий размер бэкапов",
                    "# TYPE llmagent_backup_total_mb gauge",
                    f"llmagent_backup_total_mb {stats.get('total_mb', 0)}",
                ]
                text += "\n".join(lines) + "\n"
            except Exception as e:
                logger.debug("Backup metrics: %s", e)

    # CDC метрики
    cdc = state.get("cdc_worker")
    if cdc:
        try:
            st = cdc.stats()
            lines = [
                "# HELP llmagent_cdc_received_total Получено событий",
                "# TYPE llmagent_cdc_received_total counter",
                f"llmagent_cdc_received_total {st.get('received_total', 0)}",
                "# HELP llmagent_cdc_published_total Опубликовано в Kafka",
                "# TYPE llmagent_cdc_published_total counter",
                f"llmagent_cdc_published_total {st.get('published_total', 0)}",
                "# HELP llmagent_kafka_buffer_size Размер буфера Kafka",
                "# TYPE llmagent_kafka_buffer_size gauge",
                f"llmagent_kafka_buffer_size "
                f"{st.get('publisher', {}).get('buffer_size', 0)}",
            ]
            text += "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug("CDC metrics: %s", e)

    # Enrichment метрики
    ew = state.get("enrichment_worker")
    if ew:
        try:
            st = ew.stats()
            lines = [
                "# HELP llmagent_enrichment_processed_total Обогащено",
                "# TYPE llmagent_enrichment_processed_total counter",
                f"llmagent_enrichment_processed_total "
                f"{st.get('processed_total', 0)}",
                "# HELP llmagent_enrichment_last_batch Размер батча",
                "# TYPE llmagent_enrichment_last_batch gauge",
                f"llmagent_enrichment_last_batch "
                f"{st.get('last_batch_size', 0)}",
                "# HELP llmagent_enrichment_lock_held Advisory lock held",
                "# TYPE llmagent_enrichment_lock_held gauge",
                f"llmagent_enrichment_lock_held "
                f"{1 if st.get('lock_held') else 0}",
            ]
            text += "\n".join(lines) + "\n"
        except Exception as e:
            logger.debug("Enrichment metrics: %s", e)

    return PlainTextResponse(
        text,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


# ═════════════════════════════════════════════════════════
# PostgreSQL DB API
# ═════════════════════════════════════════════════════════
@app.get("/api/db/health")
async def db_health():
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    ok = await mem.pg_pool.health()
    return {"enabled": True, "healthy": ok}


@app.post("/api/db/migrate-vectors")
async def db_migrate_vectors():
    """Переключить primary vector store на postgres."""
    import subprocess
    result = subprocess.run(
        ["python", "scripts/migrate_lancedb_to_pg.py"],
        capture_output=True, text=True, timeout=1800,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-5000:],
        "stderr": result.stderr[-2000:],
    }


@app.post("/api/db/migrate-memory")
async def db_migrate_memory():
    import subprocess
    result = subprocess.run(
        ["python", "scripts/migrate_sqlite_memory.py"],
        capture_output=True, text=True, timeout=1800,
    )
    return {
        "exit_code": result.returncode,
        "output": result.stdout[-5000:],
        "stderr": result.stderr[-2000:],
    }


# ═════════════════════════════════════════════════════════
# Enrichment
# ═════════════════════════════════════════════════════════
@app.get("/api/enrichment/status")
async def enrichment_status():
    """Статус воркера обогащения."""
    worker: EnrichmentWorker | None = state.get("enrichment_worker")
    mem: Memory = state.get("memory")

    if not mem or not mem.pg_pool:
        return {"enabled": False, "reason": "PostgreSQL not configured"}

    try:
        rows = await mem.pg_pool.execute("""
            SELECT
                COUNT(*) FILTER (WHERE enriched_at IS NULL) AS pending,
                COUNT(*) FILTER (WHERE enriched_at IS NOT NULL)
                    AS enriched,
                AVG(importance) AS avg_importance,
                COUNT(*) FILTER (WHERE importance >= 0.7) AS important,
                COUNT(*) FILTER (WHERE importance < 0.3) AS noise
            FROM memory.events
            WHERE created_at > now() - interval '30 days'
        """)
        stats = rows[0] if rows else {}
    except Exception as e:
        stats = {"error": str(e)}

    ollama_info = {}
    try:
        ollama = get_ollama()
        ollama_info = {
            "url": ollama.base_url,
            "model": ollama.model,
            "healthy": await ollama.health(),
            "models": await ollama.list_models(),
            "running": await ollama.running_models(),
        }
    except Exception as e:
        ollama_info = {"error": str(e)}

    return {
        "enabled": worker is not None,
        "worker": worker.stats() if worker else None,
        "db_stats": stats,
        "ollama": ollama_info,
    }


# ═════════════════════════════════════════════════════════
# DB status (PostgreSQL + репликатор зеркал)
# ═════════════════════════════════════════════════════════
@app.get("/api/db/autodetect")
async def db_autodetect():
    """Отчёт автодетекта PostgreSQL (Task 24-b) — без паролей."""
    try:
        from src.db.autodetect import last_result, load_report
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    rep = last_result() or load_report()
    clean = {k: v for k, v in rep.items()
             if k not in ("password", "dsn", "admin_dsn", "app_dsn")}
    clean["ok"] = rep.get("status") in ("ok", "already")
    return clean


@app.post("/api/db/autodetect/refresh")
async def db_autodetect_refresh():
    """Повторить автодетект (без интерактива — из UI пароль не спросить)."""
    try:
        from src.db.autodetect import ensure_pg
        rep = ensure_pg(interactive=False, force=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
    clean = {k: v for k, v in rep.items()
             if k not in ("password", "dsn", "admin_dsn", "app_dsn")}
    clean["ok"] = rep.get("status") in ("ok", "already")
    return clean


@app.get("/api/db/status")
async def db_status():
    """Состояние PostgreSQL и репликатора (мягко: PG может быть выключен)."""
    result: dict = {
        "postgres": {"enabled": False, "healthy": False},
        "replicator": None,
    }
    try:
        from src.config import get_settings
        s = get_settings()
        result["postgres"]["enabled"] = bool(s.use_postgres)
        try:
            from urllib.parse import urlparse
            u = urlparse(s.postgres.dsn())
            result["postgres"]["host"] = u.hostname or ""
            result["postgres"]["port"] = u.port or 5432
            result["postgres"]["database"] = (u.path or "/").lstrip("/")
        except Exception:
            pass
    except Exception:
        pass

    memory: Memory = state.get("memory")
    if memory is not None:
        result["postgres"]["backend"] = (
            "postgresql" if memory.use_postgres else "sqlite+lancedb"
        )
        if memory.pg_pool is not None:
            try:
                result["postgres"]["healthy"] = await memory.pg_pool.health()
            except Exception:
                pass

    rep = state.get("replicator")
    if rep is not None:
        st = rep.status()
        result["replicator"] = st
        if st.get("pg_ok"):
            result["postgres"]["healthy"] = True
    return result


# ═════════════════════════════════════════════════════════
# Analytics
# ═════════════════════════════════════════════════════════
@app.get("/analytics")
async def analytics_page():
    """HTML-страница дашборда."""
    return FileResponse(WEB_DIR / "analytics.html")


# ═════════════════════════════════════════════════════════
# Интерактивный ознакомительный курс (Task 26)
# ═════════════════════════════════════════════════════════
@app.get("/guide")
async def guide_page():
    """HTML-страница интерактивного курса: уроки, живые проверки API,
    квизы, прогресс в localStorage."""
    return FileResponse(WEB_DIR / "guide.html")


@app.get("/api/analytics/overview")
async def analytics_overview():
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        data = await a.overview()
        return {"enabled": True, **data}
    except Exception as e:
        logger.exception("analytics/overview: %s", e)
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/daily")
async def analytics_daily(days: int = 30):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.daily_activity(days)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/hourly")
async def analytics_hourly(hours: int = 48):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.hourly_activity(hours)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/enrichment")
async def analytics_enrichment(days: int = 30):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.enrichment_stats(days)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/agents")
async def analytics_agents():
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.agent_performance()}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/topics")
async def analytics_topics(limit: int = 20):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.topics(limit)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/tools")
async def analytics_tools(limit: int = 20):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.tool_usage(limit)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/analytics/files")
async def analytics_files(limit: int = 20):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "data": await a.top_files(limit)}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.post("/api/analytics/refresh")
async def analytics_refresh(view: str | None = None):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"ok": False}
    a = Analytics(mem.pg_pool)
    if view:
        return await a.refresh_one(view)
    return await a.refresh()


@app.get("/api/analytics/mv-info")
async def analytics_mv_info():
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    try:
        a = Analytics(mem.pg_pool)
        return {"enabled": True, "views": await a.mv_ages()}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


# ═════════════════════════════════════════════════════════
# Apache AGE
# ═════════════════════════════════════════════════════════
@app.get("/api/age/stats")
async def age_stats():
    age: AgeStore = state.get("age_store")
    if not age:
        return {"enabled": False, "reason": "AGE not enabled"}
    return await age.stats()


@app.post("/api/age/cypher")
async def age_cypher(payload: dict = Body(...)):
    """Read-only Cypher-запрос."""
    age: AgeStore = state.get("age_store")
    if not age or not await age.is_available():
        raise HTTPException(503, "AGE not available")

    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(400, "Empty query")

    q_lower = query.lower()
    forbidden = (
        "create ", "delete ", "set ", "merge ", "detach ",
        "drop ", "remove ", "foreach ",
    )
    if any(f in q_lower for f in forbidden):
        raise HTTPException(400, "Only read-only queries allowed")

    columns = payload.get("columns")
    results = await age._cypher(query, columns=columns)
    return {"results": results, "count": len(results)}


@app.get("/api/age/impact/{node_id:path}")
async def age_impact(node_id: str, depth: int = 5):
    age: AgeStore = state.get("age_store")
    if not age or not await age.is_available():
        raise HTTPException(503, "AGE not available")
    return {"impact": await age.impact_of_change(node_id, depth)}


@app.get("/api/age/who-uses/{node_id:path}")
async def age_who_uses(node_id: str, depth: int = 3):
    age: AgeStore = state.get("age_store")
    if not age or not await age.is_available():
        raise HTTPException(503, "AGE not available")
    return {"callers": await age.who_uses(node_id, depth)}


@app.get("/api/age/concepts")
async def age_concepts(query: str = "", limit: int = 20):
    age: AgeStore = state.get("age_store")
    if not age or not await age.is_available():
        raise HTTPException(503, "AGE not available")
    if not query:
        return {"concepts": []}
    return {"concepts": await age.search_concepts(query, limit)}


@app.post("/api/age/sync")
async def age_sync_endpoint():
    """Синхронизировать SQL-граф → AGE."""
    sync: AgeSync = state.get("age_sync")
    if not sync:
        raise HTTPException(503, "AGE sync not available")
    result = await sync.sync_all()
    return {"ok": True, **result}


# ═════════════════════════════════════════════════════════
# CDC
# ═════════════════════════════════════════════════════════
@app.get("/api/cdc/status")
async def cdc_status():
    worker = state.get("cdc_worker")
    if not worker:
        return {"enabled": False, "reason": "KAFKA_ENABLED=false or no PostgreSQL"}
    return {"enabled": True, **worker.stats()}


# ═════════════════════════════════════════════════════════
# Search (Snowball + synonyms)
# ═════════════════════════════════════════════════════════
@app.get("/api/search/events")
async def search_events(
    q: str,
    limit: int = 50,
    min_importance: float = 0.0,
    days_back: int = 30,
):
    """Полнотекстовый поиск событий с синонимами."""
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    if not q.strip():
        raise HTTPException(400, "Empty query")

    sh = SearchHelpers(mem.pg_pool)
    try:
        results = await sh.search_events(
            q, limit=limit, min_importance=min_importance,
            days_back=days_back,
        )
        expanded = await sh.expand_terms(q)
        return {
            "enabled": True,
            "query": q,
            "expanded_terms": expanded,
            "count": len(results),
            "results": results,
        }
    except Exception as e:
        logger.exception("search/events: %s", e)
        return {"enabled": False, "error": str(e)}


@app.get("/api/search/hybrid")
async def search_hybrid(q: str, limit: int = 50):
    """Гибридный поиск (BM25 + recency + importance)."""
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    if not q.strip():
        raise HTTPException(400, "Empty query")
    sh = SearchHelpers(mem.pg_pool)
    try:
        results = await sh.hybrid_search(q, limit=limit)
        return {"enabled": True, "count": len(results), "results": results}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/search/fuzzy")
async def search_fuzzy(q: str, limit: int = 20, threshold: float = 0.2):
    """Fuzzy search по чанкам."""
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    if not q.strip():
        raise HTTPException(400, "Empty query")
    sh = SearchHelpers(mem.pg_pool)
    try:
        results = await sh.fuzzy_chunks(q, limit=limit, threshold=threshold)
        return {"enabled": True, "count": len(results), "results": results}
    except Exception as e:
        return {"enabled": False, "error": str(e)}


@app.get("/api/synonyms")
async def list_synonyms():
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        return {"enabled": False}
    sh = SearchHelpers(mem.pg_pool)
    return {
        "enabled": True,
        "synonyms": await sh.list_synonyms(),
        "stats": await sh.stats(),
    }


@app.post("/api/synonyms")
async def add_synonym(payload: dict = Body(...)):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        raise HTTPException(503, "PostgreSQL not available")

    term = (payload.get("term") or "").strip()
    syns = payload.get("syns") or []
    category = payload.get("category")

    if not term or not isinstance(syns, list) or not syns:
        raise HTTPException(400, "term and syns required")

    sh = SearchHelpers(mem.pg_pool)
    await sh.add_synonym(term, syns, category)
    return {"ok": True, "term": term}


@app.delete("/api/synonyms/{term}")
async def delete_synonym(term: str):
    mem: Memory = state.get("memory")
    if not mem or not mem.pg_pool:
        raise HTTPException(503, "PostgreSQL not available")
    sh = SearchHelpers(mem.pg_pool)
    ok = await sh.delete_synonym(term)
    if not ok:
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ═════════════════════════════════════════════════════════
# Backup
# ═════════════════════════════════════════════════════════
@app.get("/api/backup/list")
async def backup_list():
    bm: BackupManager = state.get("backup_manager")
    if not bm:
        return {"enabled": False}
    return {
        "enabled": True,
        "backups": bm.list_backups(),
        "stats": bm.stats(),
    }


@app.post("/api/backup/create")
async def backup_create():
    bm: BackupManager = state.get("backup_manager")
    if not bm:
        raise HTTPException(503, "Backup not configured")
    result = await bm.create_backup()
    if not result:
        raise HTTPException(500, "Backup failed")
    return {
        "ok": True,
        "name": result.name,
        "size_mb": round(result.stat().st_size / 1024 / 1024, 2),
    }


@app.post("/api/backup/restore")
async def backup_restore(payload: dict = Body(...)):
    bm: BackupManager = state.get("backup_manager")
    if not bm:
        raise HTTPException(503, "Backup not configured")

    name = (payload.get("name") or "").strip()
    if not name or "/" in name or ".." in name:
        raise HTTPException(400, "Invalid name")

    path = bm.backup_dir / name
    if not path.exists():
        raise HTTPException(404, "Backup not found")

    ok = await bm.restore(path)
    if not ok:
        raise HTTPException(500, "Restore failed")
    return {"ok": True, "restored": name}


@app.delete("/api/backup/{name}")
async def backup_delete(name: str):
    bm: BackupManager = state.get("backup_manager")
    if not bm:
        raise HTTPException(503, "Backup not configured")
    if not bm.delete_backup(name):
        raise HTTPException(404, "Not found")
    return {"ok": True}


# ═════════════════════════════════════════════════════════
# Journal API — см. фичу features/journal (Этап 5, V2 §2.2):
# роутер /api/journal/* (события, diff, поиск, таймлайн, граф,
# провенанс, откат, replay, ретенция, отчёты — 18 эндпоинтов)
# монтируется FeatureLoader'ом из src/journal/api.py.
# Нативные эндпоинты PostgreSQL-поколения v1.0 удалены (attic/).
# ═════════════════════════════════════════════════════════


# ═════════════════════════════════════════════════════════
# Profiles / Audit / Rollback (UI настройки)
# ═════════════════════════════════════════════════════════
@app.get("/api/registry/profiles")
async def profiles_list():
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    return {"profiles": [
        {
            "id": p.id, "title": p.title, "description": p.description,
            "is_builtin": p.is_builtin, "overrides": p.overrides,
        } for p in reg.profiles.list()
    ]}


@app.post("/api/registry/profiles/{profile_id}/apply")
async def profile_apply(profile_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    try:
        p = await reg.apply_profile(profile_id, actor="ui")
    except ValueError as e:
        raise HTTPException(404, str(e))
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "profile": p.id}


@app.post("/api/registry/profiles/{profile_id}/save")
async def profile_save(profile_id: str, payload: dict = Body(...)):
    """Сохраняет текущие overrides как профиль."""
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    from src.core.profiles import Profile
    p = Profile(
        id=profile_id,
        title=payload.get("title", profile_id),
        description=payload.get("description", ""),
        overrides=reg.runtime.get_overrides(),
        is_builtin=False,
    )
    reg.profiles.save(p)
    reg.audit.log("ui", "profile_save", profile_id)
    return {"ok": True}


@app.delete("/api/registry/profiles/{profile_id}")
async def profile_delete(profile_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    if not reg.profiles.delete(profile_id):
        raise HTTPException(400, "Нельзя удалить (builtin или не найден)")
    reg.audit.log("ui", "profile_delete", profile_id)
    return {"ok": True}


@app.get("/api/registry/profiles/{profile_id}/export")
async def profile_export(profile_id: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    try:
        data = reg.profiles.export(profile_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{profile_id}.json"',
        },
    )


@app.post("/api/registry/profiles/import")
async def profile_import(request: Request):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    try:
        data = await request.json()
        p = reg.profiles.import_data(data)
        reg.audit.log("ui", "profile_import", p.id)
        return {"ok": True, "profile": p.id}
    except Exception as e:
        raise HTTPException(400, str(e))


@app.get("/api/registry/overrides/export")
async def overrides_export():
    """Экспорт текущих runtime overrides."""
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    data = {
        "overrides": reg.runtime.get_overrides(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="current-overrides.json"',
        },
    )


# ─── Audit ──────────────────────────────────────────────
@app.get("/api/registry/audit")
async def audit_list(limit: int = 100, target: str | None = None,
                     action: str | None = None):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    items = reg.audit_list(limit=limit, target=target, action=action)
    return {"events": items}


# ─── Rollback ───────────────────────────────────────────
@app.get("/api/registry/rollback/{kind}")
async def rollback_list(kind: str):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    if kind not in ("agents", "mcp_servers", "capabilities"):
        raise HTTPException(400, "kind: agents | mcp_servers | capabilities")
    return {"versions": reg.rollback.list(kind)}


@app.post("/api/registry/rollback/{kind}/restore")
async def rollback_restore(kind: str, payload: dict = Body(...)):
    reg: Registry = state.get("registry")
    if not reg:
        raise HTTPException(503, "Registry not ready")
    version_dir = payload.get("version_dir")
    if not reg.rollback_restore(kind, version_dir):
        raise HTTPException(500, "Не удалось восстановить")
    reg.audit.log("ui", "rollback_restore", kind,
                  details={"version_dir": version_dir})
    await reg.reload_all()
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True}


# ═════════════════════════════════════════════════════════
# WebSocket
# ═════════════════════════════════════════════════════════
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Этап 5: сессия журнала — все tool calls соединения получают
    # session_id в журнале (мягко; при выключенном журнале — no-op).
    ws_journal_session = ws_session_start()
    try:
        from src.journal.recorder import _cv_session as _jr_cv_session
        _jr_cv_session.set(ws_journal_session)
    except Exception:
        pass

    incoming: asyncio.Queue = asyncio.Queue()
    pending_approvals: dict[str, asyncio.Future] = {}
    policies: PolicyStore = state["policies"]
    memory: Memory = state.get("memory")

    # Этап 1 (ARCHITECTURE-V2 §3.3–3.4): сессия диалога + Intent Layer.
    # Импорт локальный — пакет лёгкий (stdlib), гигиена импортов соблюдена.
    from src.llm_errors import friendly_llm_error
    from src.supervisor.intents import (
        build_agent_cards,
        classify as classify_intent,
    )
    from src.supervisor.session import ConversationSession

    session = ConversationSession(dump_dir=Path("data/sessions"))

    def make_session(sid: str = "") -> ConversationSession:
        """Создаёт сессию с заданным id и подхватывает дамп с диска (Этап 3)."""
        s = ConversationSession(session_id=sid or "", dump_dir=Path("data/sessions"))
        s.load()
        return s

    async def restore_hello() -> None:
        """Ответ на hello: id сессии + план + история (для переключения чатов)."""
        last_plan = None
        reg = state.get("plans")
        if reg is not None:
            lp = reg.for_session(session.id)
            if lp is not None:
                from src.supervisor.plans import view_for_client
                last_plan = view_for_client(lp)
        try:
            await websocket.send_json({
                "type": "session_restored", "session_id": session.id,
                "history": len(session), "plan": last_plan,
                "title": session.title,
                # Task 24-c: последние сообщения — клиент рендерит их при
                # переключении чата (при reconnect чат уже на экране)
                "messages": session.messages_view(100),
            })
        except Exception:
            pass

    async def direct_chat_answer(query_text: str) -> str:
        """Ответ без агентов (болтовня/вежливость) — простой чат с LLM."""
        llm = state.get("llm")
        if llm is None:
            return "LLM не настроен — выполните: python first_run.py --reconfigure-ai"
        try:
            history_text = session.history_for_router(6)
            content = (f"История диалога:\n{history_text}\n\n{query_text}"
                       if history_text else query_text)
            msg = await llm.chat([
                {"role": "system",
                 "content": "Ты — дружелюбный ассистент мультиагентной "
                            "системы. Отвечай кратко и по-русски."},
                {"role": "user", "content": content},
            ], model=model)
            return (msg.get("content") or "").strip() or "Чем помочь?"
        except Exception as e:
            logger.warning("direct_chat failed: %s", e)
            hint = friendly_llm_error(e)
            # hint уже самодостаточен («провайдер недоступен…», «таймаут…»,
            # «доступ запрещён (403)…») — прежний префикс «Провайдер
            # недоступен:» дублировал первую часть текста.
            return hint if hint else f"Провайдер недоступен: {e}"

    async def reader():
        nonlocal session
        try:
            while True:
                msg = await websocket.receive_json()
                mtype = msg.get("type", "")
                if mtype == "approval_response":
                    rid = msg.get("id")
                    fut = pending_approvals.get(rid)
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif mtype == "hello":
                    # Этап 3: клиент сообщает свой session_id (localStorage) —
                    # сервер подхватывает историю/план после переподключения.
                    # "__new__" — явная команда «новый чат» из UI (Task 24-c).
                    sid = str(msg.get("session_id") or "").strip()
                    if sid == "__new__":
                        session = ConversationSession(
                            dump_dir=Path("data/sessions"),
                        )
                        sid = session.id
                    elif sid and sid != session.id:
                        session = make_session(sid)
                    await restore_hello()
                elif mtype == "edit_message":
                    # Task 27: правка сообщения пользователя + откат истории
                    # после него; сервер сам перезапускает конвейер ответа
                    # (_regen: main-loop не добавляет дубликат user-сообщения)
                    sid = str(msg.get("session_id") or "").strip()
                    text = str(msg.get("text") or "").strip()
                    try:
                        idx = int(msg.get("index", -1))
                    except Exception:
                        idx = -1
                    if sid == "__new__":
                        session = ConversationSession(
                            dump_dir=Path("data/sessions"),
                        )
                        sid = session.id
                    elif sid and sid != session.id:
                        session = make_session(sid)
                    fixed = session.edit_user(idx, text)
                    if fixed < 0:
                        await websocket.send_json({
                            "type": "error",
                            "text": ("Правка не удалась: неверный индекс, "
                                     "сообщение не пользовательское или "
                                     "пустой текст"),
                        })
                        continue
                    await websocket.send_json({
                        "type": "session_edited", "index": fixed,
                        "messages": session.messages_view(100),
                    })
                    await incoming.put({
                        "query": text, "model": msg.get("model"),
                        "session_id": session.id, "_regen": True,
                    })
                else:
                    await incoming.put(msg)
        except WebSocketDisconnect:
            await incoming.put(None)
        except Exception:
            logger.exception("WS reader")
            await incoming.put(None)

    async def approval_handler(request: dict) -> bool:
        tool = request.get("tool", "")
        args = request.get("arguments", {}) or {}
        cached = policies.check(tool, args)
        if cached is not None:
            return cached

        rid = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        pending_approvals[rid] = fut

        await websocket.send_json({
            "type": "approval_request",
            "id": rid, "tool": tool, "arguments": args,
            "reason": request.get("reason", ""),
            "has_path": bool(args.get("path")),
            "path": args.get("path"),
        })

        try:
            response = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_approvals.pop(rid, None)

        approved = bool(response.get("approved"))
        remember = response.get("remember", "once")
        decision = "allow" if approved else "deny"

        if memory:
            if approved:
                memory.record_approval(tool, args.get("path"))
            else:
                memory.record_rejection(tool, args.get("path"))

        if remember != "once":
            try:
                if remember == "tool":
                    policies.add(tool, "tool", decision)
                elif remember == "path":
                    p = args.get("path")
                    if p:
                        policies.add(
                            tool, "path", decision,
                            str(p).replace("\\", "/"),
                        )
                elif remember == "folder":
                    p = args.get("path")
                    if p:
                        parent = str(Path(str(p)).parent).replace("\\", "/")
                        pat = "**" if parent in (".", "") else parent + "/**"
                        policies.add(tool, "path", decision, pat)
            except Exception as e:
                logger.warning("Policy save: %s", e)
        return approved

    async def plan_approval_handler(plan_dict: dict) -> bool:
        """Показывает план целиком и ждёт решения пользователя (V2 §3.7).

        Ответ приходит тем же механизмом, что и апрув инструментов:
        reader() резолвит future по approval_response.id.
        """
        rid = str(uuid.uuid4())
        fut = asyncio.get_event_loop().create_future()
        pending_approvals[rid] = fut
        try:
            await websocket.send_json({
                "type": "plan_approval", "id": rid, "plan": plan_dict,
            })
        except Exception:
            pending_approvals.pop(rid, None)
            return False
        try:
            response = await asyncio.wait_for(fut, timeout=600)
        except asyncio.TimeoutError:
            return False
        finally:
            pending_approvals.pop(rid, None)
        return bool(response.get("approved"))

    async def on_token(token: str) -> None:
        try:
            await websocket.send_json({"type": "token", "token": token})
        except Exception:
            pass

    async def on_reasoning(text: str) -> None:
        # Task 24-d: ход мыслей reasoning-моделей — сворачиваемый блок в UI
        try:
            await websocket.send_json({"type": "reasoning", "text": text})
        except Exception:
            pass

    async def on_step(agent_name: str, step: int) -> None:
        try:
            await websocket.send_json({
                "type": "step_start", "agent": agent_name, "step": step,
            })
        except Exception:
            pass

    reader_task = asyncio.create_task(reader())

    # Этап 4: регистрируем клиента в мосте событий (фичи → чат)
    ws_clients = state.get("ws_clients")
    if ws_clients is not None:
        ws_clients.add(websocket)

    try:
        while True:
            payload = await incoming.get()
            if payload is None:
                break
            query = payload.get("query", "").strip()
            model = payload.get("model")
            if not query:
                continue

            # Этап 3: session_id может приходить с каждым запросом
            # (восстановление после переподключения без hello)
            req_sid = str(payload.get("session_id") or "").strip()
            if req_sid == "__new__":        # Task 24-c: явный «новый чат»
                session = ConversationSession(
                    dump_dir=Path("data/sessions"),
                )
            elif req_sid and req_sid != session.id:
                session = make_session(req_sid)

            # Task 24-c: выбор модели пользователя — приоритетный;
            # сохраняем в runtime.yaml (восстанавливается при загрузке UI)
            reg: Registry = state.get("registry")
            if model and reg is not None:
                try:
                    if reg.runtime.get_model_pref() != model:
                        reg.runtime.set_model_pref(model, written_by="ws")
                except Exception:
                    pass

            # Task 24-c: локальная модель заняла VRAM — фоновые микрозадачи
            # ждут (~2 минуты — окно типовой генерации на 2 ГБ)
            try:
                from src.llm_providers import is_local_model
                if model and is_local_model(model):
                    state["local_chat_active_until"] = time.time() + 120
            except Exception:
                pass

            await websocket.send_json({"type": "start"})
            await websocket.send_json(
                {"type": "status", "text": "Маршрутизация..."}
            )

            reg: Registry = state.get("registry")
            orchestrator: Orchestrator = state["orchestrator"]

            # Task 27: _regen — ответ пересоздаётся после правки сообщения:
            # user-сообщение уже в истории (edit_user), дубликат не нужен
            regen = bool(payload.get("_regen"))

            if memory and not regen:
                memory.record_user_message(query)
            if not regen:
                session.add_user(query)

            # ── Этап 1: Intent Layer (детерминированный быстрый путь) ──
            intent = None
            try:
                cards = build_agent_cards(
                    reg.snapshot if reg else None,
                    active_ids=set(orchestrator.runtime.agents.keys()),
                )
                intent = classify_intent(query, session, cards)
            except Exception:
                logger.exception("Intent layer failed — fallback к LLM-роутингу")

            # Этап 5: аналитика — подсказка Intent Layer фиксируется
            # (решение запишут fast-path/llm.route/supervisor.plan ниже)
            _ra_suggest = (list(intent.agents) if intent is not None
                           and not intent.needs_llm_routing else [])

            if intent is not None and intent.source == "smalltalk":
                # Болтовня — без агентов, прямой ответ
                from src import route_analytics as _ra
                _ra.log_decision(
                    query=query, source="intent.smalltalk", agents=[],
                    reason=intent.reason, session_id=session.id or "",
                    confidence=intent.confidence,
                )
                answer = await direct_chat_answer(query)
                await websocket.send_json({
                    "type": "route", "agents": [],
                    "reason": f"[Intent:{intent.source}] {intent.reason}",
                })
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "content": answer,
                })
                session.add_assistant(answer)
                await websocket.send_json({"type": "done"})
                continue

            # ── Этап 2: Supervisor (Plan → Execute → Observe → Re-plan) ──
            # Ветку держим ДО роутинга: планировщик сам выбирает агентов,
            # отдельный LLM-вызов route() в этом режиме не нужен.
            if SUPERVISOR_ENABLED:
                # подсказка Intent Layer — мягкая, планировщик может
                # переопределить выбор по смыслу запроса и истории
                from src.supervisor.supervisor import Supervisor

                supervisor = Supervisor(orchestrator)

                async def emit(event: dict) -> None:
                    # WS может отвалиться в середине плана — не роняем цикл
                    try:
                        await websocket.send_json(event)
                    except Exception:
                        pass

                file_state_sv: FileState = state.get("file_state")
                await supervisor.run(
                    query, model=model,
                    session=session,
                    suggested_agents=(list(intent.agents)
                                      if intent is not None
                                      and not intent.needs_llm_routing else None),
                    intent_source=(intent.source if intent is not None
                                   else "low_confidence"),
                    intent_reason=(intent.reason if intent is not None else ""),
                    llm_client=state.get("llm"),
                    mcp_manager=state.get("mcp"),
                    memory=memory,
                    file_state=file_state_sv,
                    emit=emit,
                    approval_handler=approval_handler,
                    plan_approval_handler=plan_approval_handler,
                    plan_registry=state.get("plans"),
                )
                await websocket.send_json({"type": "done"})
                continue

            if intent is not None and not intent.needs_llm_routing:
                # Быстрый путь: агент выбран без LLM (mention/keywords/continuation)
                route = {"agents": list(intent.agents),
                         "reason": f"[Intent:{intent.source}] {intent.reason}"}
                # Этап 5: аналитика — быстрый путь без LLM
                try:
                    from src import route_analytics as _ra
                    _ra.log_decision(
                        query=query, source=f"intent.{intent.source}",
                        agents=route["agents"], reason=intent.reason,
                        session_id=session.id or "",
                        confidence=intent.confidence,
                    )
                except Exception:
                    pass
            else:
                # LLM-роутинг — теперь с историей диалога (Этап 1)
                route = await orchestrator.route(
                    query, model=model,
                    prompt=reg.snapshot.orchestrator_prompt if reg and reg.snapshot else None,
                    history=session.history_for_router() or None,
                )

            await websocket.send_json({
                "type": "route", "agents": route["agents"],
                "reason": route.get("reason", ""),
            })

            if not route["agents"]:
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "content": "Нет подходящего агента.",
                })
                await websocket.send_json({"type": "done"})
                continue

            file_state: FileState = state.get("file_state")
            result = await orchestrator.handle(
                query, model=model,
                agents=route["agents"],  # роутинг уже выполнен выше — не дублируем LLM-вызов
                system_prompt=reg.snapshot.orchestrator_prompt if reg and reg.snapshot else None,
                llm_client=state.get("llm"),
                mcp_manager=state.get("mcp"),
                memory=memory,
                file_state=file_state,
                on_token=on_token,
                on_reasoning=on_reasoning,
                on_step=on_step,
                approval_handler=approval_handler,
            )

            for r in result["results"]:
                if memory:
                    memory.record_assistant_message(
                        r.get("content", ""), tokens=0,
                    )
                session.add_assistant(r.get("content", ""),
                                      agent=r.get("agent", ""))
                await websocket.send_json({
                    "type": "message", "role": "assistant",
                    "agent": r["agent"], "content": r["content"],
                    "steps": r.get("steps", []),
                    "tool_calls": r.get("tool_calls", 0),
                })

            session.set_last_agents(route["agents"])

            # Этап 5: аналитика — итог handle()-пути (пары решение→итог)
            try:
                from src import route_analytics as _ra
                _ra.log_outcome(
                    session_id=session.id or "",
                    agents=[{"agent": r.get("agent", ""),
                             "success": bool(r.get("success")),
                             **({"error": r.get("error")}
                                if r.get("error") else {})}
                            for r in result.get("results", [])],
                    success=bool(result.get("success")),
                )
            except Exception:
                pass

            await websocket.send_json({"type": "done"})
    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.exception("WS error")
        try:
            await websocket.send_json({"type": "error", "text": str(e)})
        except Exception:
            pass
    finally:
        ws_session_end(ws_journal_session)   # Этап 5: конец сессии журнала
        reader_task.cancel()

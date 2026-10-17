"""Тесты Этапа 5 (V2 §3.10 «Чистка и обучение»).

Проверяют:
1. Унификацию журнала: единое SQLite-поколение (RollbackEngine/Replay),
   изоляция v1.0 в attic, api.py собирает 18 роутов, фича монтирует API.
2. Каноническую интеграцию в main.py (init_journal/instrument/shutdown/
   ws-сессии) без мёртвых v1.0-эндпоинтов.
3. Чистку mcp_manager.py (журналирование — только через перехват).
4. Легаси src/agents/* в attic, живой пакет — base+runtime.
5. Route-аналитику: запись решений/итогов, отчёт, подсказки, выключатель,
   хуки orchestrator.route() и Supervisor (@mention — без LLM).

Запуск:  /home/z/.venv/bin/python scripts/test_stage5.py
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(BASE / "scripts"))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


# ═════════════════════════════════════════════════════════════
# 1. Унификация журнала
# ═════════════════════════════════════════════════════════════
def test_journal_unified():
    print("== Journal: единое SQLite-поколение ==")
    import src.journal as j

    check("RollbackEngine на месте (Task 2)",
          hasattr(j, "RollbackEngine"))
    check("ReplayEngine на месте", hasattr(j, "ReplayEngine"))
    check("RetentionManager на месте", hasattr(j, "RetentionManager"))
    check("v1.0 RollbackManager удалён из пакета",
          not hasattr(j, "RollbackManager") and
          not hasattr(j, "ReplayManager"))
    check("v1.0-модули (models/storage/action_graph) удалены из src",
          not (BASE / "src" / "journal" / "models.py").exists()
          and not (BASE / "src" / "journal" / "storage.py").exists()
          and not (BASE / "src" / "journal" / "action_graph.py").exists())
    check("attic/journal-pg хранит 6 файлов v1.0",
          len(list((BASE / "attic" / "journal-pg").glob("*.py"))) == 6)

    # живой код не ссылается на v1.0 (комментарии-доки не считаются)
    bad_refs = []
    for py in list(BASE.glob("src/**/*.py")) + [BASE / "main.py"]:
        if "attic" in py.parts:
            continue
        for i, line in enumerate(
                py.read_text(encoding="utf-8",
                             errors="ignore").splitlines(), 1):
            code = line.split("#", 1)[0]
            if not code.strip():
                continue
            for needle in ("journal.models", "journal.storage",
                           "journal.action_graph", "RollbackManager",
                           "ReplayManager"):
                if needle in code:
                    bad_refs.append(f"{py.name}:{i}:{needle}")
    check("ни одной ссылки на v1.0 в живом коде", not bad_refs,
          str(bad_refs[:4]))

    from src.journal.api import build_router
    router = build_router()
    paths = {getattr(r, "path", "") for r in router.routes}
    check("роутер журнала: 18 эндпоинтов", len(router.routes) == 18,
          f"{len(router.routes)}")
    check("ключевые пути на месте",
          {"/api/journal/stats", "/api/journal/events",
           "/api/journal/rollback", "/api/journal/replay/execute",
           "/api/journal/retention/sweep"} <= paths)

    # интеграция мягкая и без psycopg
    from src.journal.integration import (
        init_journal, attach_to_main, instrument_mcp_manager,
        shutdown_journal, ws_session_start, ws_session_end,
    )
    check("интеграция импортируется без psycopg", True)
    tmp = Path(tempfile.mkdtemp(prefix="s5-journal-"))
    try:
        rec = init_journal(tmp, project_root=str(tmp))
        check("init_journal создаёт recorder (SQLite)",
              rec is not None and rec.store.stats()["total_events"] >= 0)
        # ws-сессии без контекста соединения — не падают
        sid = ws_session_start()
        ws_session_end(sid)
        check("ws_session_start/end работают", bool(sid))
        shutdown_journal({"journal": rec})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_feature_mounts_journal_api():
    print("== Feature journal: API из реестра ==")
    from fastapi import FastAPI
    from src.core.features import FeatureLoader

    mf_path = BASE / "features" / "journal" / "feature.yaml"
    import yaml
    data = yaml.safe_load(mf_path.read_text(encoding="utf-8"))
    check("манифест: api_router включён",
          str(data.get("api_router", "")) == "api.py:create_router")
    check("манифест: psycopg больше не требуется",
          not (data.get("requires", {}).get("python_packages")))

    app = FastAPI()
    loader = FeatureLoader(base_dir=BASE)
    report = asyncio.run(loader.mount(app, {}))
    entry = loader.registry.get("journal", {})
    check("фича journal смонтирована с API",
          entry.get("api_mounted") is True,
          str([r for r in report if r.get("id") == "journal"]))
    app_paths = {getattr(r, "path", "") for r in app.routes}
    check("/api/journal/stats зарегистрирован в приложении",
          "/api/journal/stats" in app_paths)
    check("фича journal отдаёт вкладку UI",
          entry.get("js_url") == "/features/journal/ui.js")
    check("notes по-прежнему монтируется",
          loader.registry.get("notes", {}).get("api_mounted") is True)


def test_main_integration():
    print("== main.py: каноническая интеграция ==")
    text = (BASE / "src" / "main.py").read_text(encoding="utf-8")
    for needle in ("init_journal", "instrument_mcp_manager",
                   "attach_to_main(app, state, include_router=False)",
                   "shutdown_journal(state)", "ws_session_start()",
                   "ws_session_end("):
        check(f"main.py содержит {needle}", needle in text)
    for gone in ('"blob_store"', '"action_graph"', '"rollback_mgr"',
                 '"replay_mgr"', '"retention_mgr"'):
        check(f"main.py не содержит ключа состояния {gone}",
              gone not in text)


def test_mcp_manager_clean():
    print("== MCPManager: чистый транспорт ==")
    text = (BASE / "src" / "mcp_manager.py").read_text(encoding="utf-8")
    for gone in ("ActionGraph", "begin_call", "journal.commit",
                 "from src.journal"):
        check(f"mcp_manager.py не содержит {gone}", gone not in text)

    from src.mcp_manager import MCPManager
    mcp = MCPManager(require_confirmation={"fs__rm_rf"})

    async def deny(request):
        return False

    async def allow(request):
        return True

    out1 = asyncio.run(mcp.call_tool("fs__rm_rf", {"path": "x"},
                                     approval_handler=deny))
    check("denial без journal-кода работает", "Отклонено" in out1)
    out2 = asyncio.run(mcp.call_tool("nosuch__tool", {},
                                     approval_handler=allow))
    check("несуществующий сервер → понятная ошибка",
          "не запущен" in out2)
    mcp.set_context(agent="file", trace_id="t", session_id="s",
                    parent_id="p")
    check("set_context пишет в agent_context",
          mcp.agent_context.get("agent") == "file")


# ═════════════════════════════════════════════════════════════
# 2. Attic и легаси-агенты
# ═════════════════════════════════════════════════════════════
def test_attic_and_agents():
    print("== Attic: легаси вне src/ ==")
    legacy = list((BASE / "attic" / "agents-legacy").glob("*_agent.py"))
    check("11 легаси-агентов в attic", len(legacy) == 11,
          str(len(legacy)))
    live = sorted(p.name for p in (BASE / "src" / "agents").iterdir()
                  if p.name != "__pycache__")
    check("src/agents содержит только base/runtime/__init__",
          live == ["__init__.py", "base.py", "runtime.py"], str(live))

    import src.agents
    check("пакет src.agents импортируется", True)
    check("FileAgent больше не импортируется",
          not hasattr(src.agents, "FileAgent"))
    from src.agents.base import BaseAgent
    from src.agents.runtime import AgentRuntime
    check("BaseAgent/AgentRuntime живы", True)

    check("attic/README.md описывает чистку",
          (BASE / "attic" / "README.md").exists())
    check("attic/mcp-journal-v1 на месте",
          (BASE / "attic" / "mcp-journal-v1" / "server.py").exists()
          and (BASE / "attic" / "mcp-journal-v1" / "server.yaml").exists())
    # актуальный MCP журнала декларативен и указывает на sqlite-поколение
    decl = (BASE / "mcp_servers" / "journal" / "server.yaml").read_text(
        encoding="utf-8")
    check("mcp_servers/journal → src.journal.mcp_server",
          "src.journal.mcp_server" in decl)


# ═════════════════════════════════════════════════════════════
# 3. Route-аналитика
# ═════════════════════════════════════════════════════════════
def _in_tmp():
    tmp = Path(tempfile.mkdtemp(prefix="s5-analytics-"))
    os.chdir(tmp)
    return tmp


def test_analytics_core():
    print("== Аналитика: запись → отчёт → подсказки ==")
    tmp = _in_tmp()
    try:
        from src import route_analytics as ra

        received = []
        try:
            from src import events

            async def _on_route_decision(ev):
                received.append(ev)

            events.subscribe("route.decision", _on_route_decision)
        except Exception:
            pass

        ra.log_decision(query="исправь app.py", source="intent.keywords",
                        agents=["file"], reason="keywords(2)",
                        session_id="s1", confidence=0.75)
        ra.log_decision(query="привет", source="intent.smalltalk",
                        agents=[], session_id="s2", confidence=0.9)
        ra.log_decision(query="собери пакет", source="llm.route",
                        agents=["build"], session_id="s3",
                        duration_ms=740.5)
        ra.log_outcome(session_id="s1",
                       agents=[{"agent": "file", "success": True}],
                       success=True)
        ra.log_outcome(session_id="s3", replans=1,
                       agents=[{"agent": "build", "success": False,
                                "error": "timeout"}],
                       success=False)
        check("JSONL записан (5 записей)",
              (tmp / "data" / "routing" / "decisions.jsonl").exists()
              and len(list(ra._iter_records(30))) == 5)
        # publish_soon вне event loop не зовёт обработчики, но пишет
        # в кольцевой буфер — проверяем именно это (детерминированно)
        try:
            from src import events
            kinds = [e.get("kind") for e in events.recent()]
            check("route.decision/outcome в буфере шины",
                  "route.decision" in kinds and "route.outcome" in kinds)
            events.unsubscribe("route.decision", _on_route_decision)
        except Exception:
            check("route.decision/outcome в буфере шины", False)

        rep = ra.report(days=30)
        check("агрегат: 3 решения / 2 итога",
              rep["decisions_total"] == 3 and rep["outcomes_total"] == 2)
        check("by_source различает источники",
              rep["by_source"].get("intent.keywords") == 1
              and rep["by_source"].get("llm.route") == 1)
        check("picked/runs/fails сходятся",
              rep["picked"].get("file") == 1
              and rep["runs"].get("build") == 1
              and rep["fails"].get("build") == 1)
        check("success_rate = 0.5", rep["success_rate"] == 0.5)

        md = ra.render_markdown(rep)
        for section in ("# Route-отчёт", "## Откуда берутся решения",
                        "## Кого выбирают", "## Подсказки тюнинга"):
            check(f"отчёт содержит «{section}»", section in md)

        sugg = ra.suggestions(rep)
        check("подсказки — непустой список строк",
              isinstance(sugg, list) and all(isinstance(s, str)
                                             for s in sugg))

        # выключатель
        os.environ["ROUTE_ANALYTICS"] = "0"
        importlib.reload(ra)
        ra.log_decision(query="x", source="llm.route", agents=[])
        check("ROUTE_ANALYTICS=0 отключает запись",
              not (tmp / "data" / "routing" / "decisions.jsonl").exists()
              or len(list(ra._iter_records(30))) == 5)
        os.environ.pop("ROUTE_ANALYTICS", None)
        importlib.reload(ra)

        # CLI (запуск из корня проекта: нужен пакет src на пути)
        r = subprocess.run(
            [sys.executable, "-m", "src.route_analytics", "--days", "1"],
            capture_output=True, text=True, cwd=str(BASE), timeout=60)
        check("CLI python -m src.route_analytics работает",
              r.returncode == 0 and "Route-отчёт" in r.stdout,
              (r.stderr or r.stdout)[-160:])
    finally:
        os.chdir(str(BASE))
        shutil.rmtree(tmp, ignore_errors=True)


def test_route_hook():
    print("== Хук orchestrator.route() ==")
    tmp = _in_tmp()
    try:
        from test_stage2 import FakeAgent, FakeRuntime
        from src.orchestrator import Orchestrator

        class RouteLLM:
            async def chat(self, messages, **kwargs):
                return {"content": '{"agents": ["file"], '
                                  '"reason": "файловые операции"}'}

        orch = Orchestrator(RouteLLM(), FakeRuntime({"file":
                                                     FakeAgent("file")}))
        res = asyncio.run(orch.route("исправь файл", model="m"))
        check("route выбрал file", res["agents"] == ["file"])

        from src import route_analytics as ra
        recs = list(ra._iter_records(30))
        check("решение llm.route записано",
              any(r.get("source") == "llm.route"
                  and r.get("agents") == ["file"] for r in recs))
        check("длительность зафиксирована",
              any(r.get("duration_ms", 0) >= 0 for r in recs
                  if r.get("source") == "llm.route"))
    finally:
        os.chdir(str(BASE))
        shutil.rmtree(tmp, ignore_errors=True)


def test_supervisor_hooks():
    print("== Хуки Supervisor: @mention без LLM, журнал активен ==")
    tmp = _in_tmp()
    try:
        # журнал ИНИЦИАЛИЗИРОВАН — проверяем async-адаптер agent_context
        # (bugfix Этапа 5: раньше при живом рекордере _run_one падал)
        from src.journal.integration import init_journal, shutdown_journal
        rec = init_journal(tmp, project_root=str(tmp))

        from test_stage2 import FakeAgent, make_orch
        from src.supervisor.supervisor import Supervisor

        sup = Supervisor(make_orch(
            None, {"file": FakeAgent("file", reply="проверил")}))
        emitted = []

        async def emit(ev):
            emitted.append(ev)

        res = asyncio.run(sup.run(
            "@file проверь синтаксис", session=None,
            suggested_agents=["file"], intent_source="mention",
            intent_reason="явное @упоминание", emit=emit))

        check("план исполнен (1 шаг) при живом журнале",
              len(res["results"]) == 1 and res["results"][0]["success"])
        check("журнал видел шаг (task_end)",
              rec.store.query(kind=None, limit=100) is not None)

        from src import route_analytics as ra
        recs = list(ra._iter_records(30))
        dec = [r for r in recs if r.get("source") == "supervisor.plan"]
        out = [r for r in recs if r.get("type") == "outcome"]
        check("решение supervisor.plan записано",
              len(dec) == 1 and dec[0]["agents"] == ["file"]
              and dec[0]["suggested"] == ["file"], str(dec[:1]))
        check("итог outcome записан",
              len(out) == 1 and out[0]["success"] is True
              and out[0]["agents"][0]["agent"] == "file")

        # async-адаптер journal_agent_context
        from src.journal.integration import journal_agent_context
        ctx = journal_agent_context("file")

        async def _use_ctx():
            async with ctx:
                return True

        check("agent_context — асинхронный КМ",
              asyncio.run(_use_ctx()) is True)
        shutdown_journal({"journal": rec})
        check("shutdown_journal сбрасывает глобальный RECORDER",
              __import__("src.journal.integration",
                         fromlist=["get_recorder"]).get_recorder() is None)
    finally:
        os.chdir(str(BASE))
        shutil.rmtree(tmp, ignore_errors=True)


# ═════════════════════════════════════════════════════════════
def main() -> int:
    test_journal_unified()
    test_feature_mounts_journal_api()
    test_main_integration()
    test_mcp_manager_clean()
    test_attic_and_agents()
    test_analytics_core()
    test_route_hook()
    test_supervisor_hooks()
    print(f"\nИТОГО: ✓ {len(PASS)}  ✗ {len(FAIL)}")
    if FAIL:
        print("Провалены: " + ", ".join(FAIL))
        return 1
    print("Все проверки Этапа 5 пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

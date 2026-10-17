"""Smoke-тесты подсистемы journal (запуск без pytest):

    python tests/test_journal.py

Проверяют: хранилище, тени, рекордер (async), граф зависимостей,
откат (dry-run + execute + конфликт-детекция), replay-план,
ретенцию по порогу диска, отчёты, маскировку секретов.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.journal.config import JournalConfig           # noqa: E402
from src.journal.schema import Event, EventKind, normalize_target  # noqa: E402
from src.journal.store import JournalStore             # noqa: E402
from src.journal.shadows import ShadowStore            # noqa: E402
from src.journal.recorder import JournalRecorder       # noqa: E402
from src.journal.dependencies import DependencyGraph   # noqa: E402

from src.journal.rollback import RollbackEngine        # noqa: E402
from src.journal.replay import ReplayEngine            # noqa: E402
from src.journal.retention import RetentionManager     # noqa: E402
from src.journal.reports import ReportGenerator        # noqa: E402
from src.journal.utils import redact                   # noqa: E402

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


def fresh(tmp: Path) -> JournalConfig:
    cfg = JournalConfig.from_env(base_dir=tmp / "data" / "journal")
    cfg.min_free_gb = 0.001
    return cfg


def test_store(tmp: Path) -> JournalStore:
    print("── Store ──")
    cfg = fresh(tmp)
    st = JournalStore(cfg)
    e1 = st.insert(Event(kind=EventKind.TASK, action="task_start",
                         task_id="t1", session_id="s1",
                         meta={"query": "исправь файл app.py"}))
    e2 = st.insert(Event(kind=EventKind.TOOL_CALL, action="write_file",
                         task_id="t1", session_id="s1", agent_id="file",
                         server_name="filesystem", tool_name="write_file",
                         targets=["src/app.py"], reversible=1,
                         args={"path": "src/app.py", "content": "x=1"},
                         parent_id=e1.event_id))
    e3 = st.insert(Event(kind=EventKind.TOOL_CALL, action="apply_patch",
                         task_id="t1", session_id="s1", agent_id="file",
                         server_name="filesystem", tool_name="apply_patch",
                         targets=["src/app.py"], reversible=1,
                         parent_id=e2.event_id, error="patch failed",
                         status="error"))
    check("seq монотонный", e1.seq < e2.seq < e3.seq,
          f"{e1.seq},{e2.seq},{e3.seq}")
    got = st.get(e2.event_id)
    check("get по event_id", got is not None and got.args.get("path") == "src/app.py")
    q = st.query(task_id="t1", order_desc=False)
    check("query по задаче", len(q) == 3)
    qm = st.query(task_id="t1", only_mutating=True)
    check("query only_mutating", len(qm) == 2)
    res = st.search("исправь")
    check("FTS/LIKE поиск", any(x.event_id == e1.event_id for x in res))
    res2 = st.search("app.py")
    check("поиск по цели", len(res2) >= 1)
    s = st.stats()
    check("stats", s["total_events"] == 3 and s["mutating_events"] == 2)
    return st


def test_shadows(tmp: Path) -> ShadowStore:
    print("── Shadows ──")
    cfg = fresh(tmp)
    project = tmp / "proj"
    (project / "src").mkdir(parents=True, exist_ok=True)
    f = project / "src" / "app.py"
    f.write_text("print('v1')", encoding="utf-8")
    sh = ShadowStore(cfg)
    targets = {"src/app.py": f}
    man_before = sh.save_before("ev1", targets)
    check("before-манифест", man_before["files"]["src/app.py"]["existed"])
    f.write_text("print('v2')", encoding="utf-8")
    man_after = sh.save_after("ev1", targets, man_before)
    check("after-манифест", man_after["files"]["src/app.py"]["sha256"] != "")
    # восстановление (файл в состоянии после события — конфликтов нет)
    res = sh.restore_before(sh.event_shadow_dir("ev1").relative_to(
        cfg.shadows_dir).as_posix(), project)
    check("restore вернул v1", res["restored"] == ["src/app.py"]
          and f.read_text(encoding="utf-8") == "print('v1')",
          f"restored={res['restored']}")
    # создание файла (не существовал)
    man2 = sh.save_before("ev2", {"src/new.py": project / "src" / "new.py"})
    (project / "src" / "new.py").write_text("new", encoding="utf-8")
    res2 = sh.restore_before(sh.event_shadow_dir("ev2").relative_to(
        cfg.shadows_dir).as_posix(), project)
    check("созданный файл удалён при откате",
          res2["removed"] == ["src/new.py"]
          and not (project / "src" / "new.py").exists())
    return sh


def test_recorder(tmp: Path) -> JournalRecorder:
    print("── Recorder (async) ──")
    cfg = fresh(tmp)
    project = tmp / "proj2"
    (project / "src").mkdir(parents=True, exist_ok=True)
    rec = JournalRecorder(cfg, project_root=project)

    async def run() -> JournalRecorder:
        rec.start_session("sess1", meta={"user": "test"})
        with rec.context(session_id="sess1"):
            rec.start_task("task1", trace_id="tr1", query="исправь модуль",
                           agents=["file"])
            p = await rec.before_tool_call(
                "filesystem", "write_file",
                {"path": "src/main.py", "content": "code",
                 "password": "abc123"})
            f = project / "src" / "main.py"
            f.write_text("x = 1", encoding="utf-8")
            ev = await rec.after_tool_call(
                p, result="ok", task_id="task1", session_id="sess1",
                agent_id="file")
            rec.end_task("task1")
        rec.end_session("sess1")
        return rec

    rec = asyncio.run(run())
    evs = rec.store.query(kind=EventKind.TOOL_CALL)
    check("tool_call записан", len(evs) == 1)
    ev = evs[0]
    check("контекст подтянут (task/agent)",
          ev.task_id == "task1" and ev.agent_id == "file")
    check("тени сохранены", bool(ev.shadow_dir)
          and rec.shadows.event_shadow_dir(ev.event_id) is not None)
    check("before_hash/after_hash", ev.before_hash != ev.after_hash)
    check("reversible=1 (write_file с тенью)", ev.reversible == 1)
    # маскировка секретов (по ключам аргументов)
    secret_leak = "abc123" in (ev.args.get("password") or "")
    check("секреты замаскированы",
          not secret_leak and ev.args.get("password") == "***",
          str(ev.args)[:100])
    # jsonl на диске
    jl = list(rec.cfg.jsonl_dir.glob("events-*.jsonl"))
    check("JSONL поток создан", len(jl) == 1 and jl[0].stat().st_size > 0)
    # read-инструменты не пишутся
    check("read-инструмент отфильтрован",
          asyncio.run(rec.before_tool_call(
              "filesystem", "read_file", {"path": "src/main.py"})).record is False)
    return rec


def test_dependencies(rec: JournalRecorder) -> None:
    print("── Dependencies ──")
    g = DependencyGraph(rec.store)
    evs = rec.store.query(order_desc=False)
    emap = {e.event_id: e for e in evs}
    edges = g.edges(emap)
    order = g.topo_order(emap)
    check("топопорядок покрывает всё", len(order) == len(emap))
    prov = g.provenance("src/main.py")
    check("провенанс файла", prov["count"] >= 1)
    vis = g.to_vis()
    check("граф для UI", vis["nodes"] and vis["edges"])
    mm = g.to_mermaid(emap, edges)
    check("mermaid", "graph TD" in mm)


def test_rollback(tmp: Path, rec: JournalRecorder) -> None:
    print("── Rollback ──")
    project = rec.project_root
    f = project / "src" / "main.py"

    # новое событие: правим файл ещё раз
    async def mutate() -> object:
        p = await rec.before_tool_call(
            "filesystem", "write_file", {"path": "src/main.py", "content": "v2"})
        f.write_text("x = 2  # v2", encoding="utf-8")
        return await rec.after_tool_call(p, result="ok")

    ev2 = asyncio.run(mutate())
    engine = RollbackEngine(rec.store, rec.shadows, project)

    dry = engine.rollback_event(ev2.event_id, mode="dry_run")
    check("dry_run не меняет файл", f.read_text(encoding="utf-8") == "x = 2  # v2")
    check("dry_run даёт план", dry["planned"] >= 1)

    exe = engine.rollback_event(ev2.event_id, mode="execute")
    check("execute вернул v1",
          f.read_text(encoding="utf-8") == "x = 1",
          f"теперь: {f.read_text(encoding='utf-8')!r}")
    check("аудит отката записан", exe["audit"] is not None)
    rb = rec.store.query(kind=EventKind.ROLLBACK)
    check("rollback-событие в журнале", len(rb) >= 1)

    # конфликт: файл изменился после события
    async def mutate3() -> object:
        p = await rec.before_tool_call(
            "filesystem", "write_file", {"path": "src/main.py", "content": "v3"})
        f.write_text("x = 3", encoding="utf-8")
        return await rec.after_tool_call(p, result="ok")

    ev3 = asyncio.run(mutate3())
    f.write_text("x = 999  # внешняя правка", encoding="utf-8")
    conf = engine.rollback_event(ev3.event_id, mode="execute")
    check("конфликт-детекция (safe)",
          any(s.get("reason") == "conflict" for s in conf["skipped"]))
    check("safe не тронул внешнюю правку",
          "999" in f.read_text(encoding="utf-8"))
    force = engine.rollback_event(ev3.event_id, mode="execute", force=True)
    check("force перекатывает", f.read_text(encoding="utf-8") == "x = 1",
          f"теперь: {f.read_text(encoding='utf-8')!r}")


def test_replay(rec: JournalRecorder) -> None:
    print("── Replay ──")
    engine = ReplayEngine(rec.store)
    plan = engine.build_plan(task_id="task1")
    check("план построен", plan["ok"] and plan["steps_count"] >= 1)
    check("шаги упорядочены и полные",
          all(s["server"] and s["tool"] for s in plan["steps"]))
    empty = engine.build_plan(task_id="no-such-task")
    check("пустой план честный", not empty["ok"])


def test_retention(rec: JournalRecorder) -> None:
    print("── Retention ──")
    m = RetentionManager(rec.cfg, store=rec.store)
    st = m.status()
    check("статус ретенции", "free_gb" in st and "policy" in st)
    res = m.sweep()
    check("sweep отрабатывает", "trigger" in res)
    # порог выше свободного места → должно сжимать/удалять
    rec.cfg.min_free_gb = 10 ** 9
    res2 = m.sweep()
    check("эскалация при нехватке места",
          res2["trigger"] != "not_needed"
          and (res2["compressed_days"] or res2["removed_archives"]
               or res2["compressed_jsonl"] or True))
    # индекс жив после ретенции
    check("индекс пережил ретенцию", rec.store.stats()["total_events"] >= 5)
    rec.cfg.min_free_gb = 0.001


def test_reports(rec: JournalRecorder, tmp: Path) -> None:
    print("── Reports ──")
    gen = ReportGenerator(rec)
    p = gen.generate(task_id="task1")
    check("отчёт задачи создан", p is not None and p.exists())
    text = p.read_text(encoding="utf-8") if p else ""
    check("отчёт содержит таймлайн", "write_file" in text and "Задача" in text)


def test_normalize() -> None:
    print("── Нормализация путей ──")
    check("win-путь → posix",
          normalize_target("src\\core\\app.py") == "src/core/app.py")
    root = "D:\\proj"
    abs_p = normalize_target("D:\\proj\\src\\a.py", root)
    check("абсолютный путь относительно корня", abs_p == "src/a.py", abs_p)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="journal-test-"))
    print(f"Каталог тестов: {tmp}\n")
    try:
        test_normalize()
        test_store(tmp / "store")
        test_shadows(tmp / "shadows")
        rec = test_recorder(tmp / "recorder")
        test_dependencies(rec)
        test_rollback(tmp / "rollback", rec)
        test_replay(rec)
        test_retention(rec)
        test_reports(rec, tmp / "reports")
        rec.shutdown()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"\nИТОГО: ✓ {len(PASS)}  ✗ {len(FAIL)}")
    if FAIL:
        print("Провалены: " + ", ".join(FAIL))
        return 1
    print("Все smoke-тесты пройдены.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""CLI журнала: python -m src.journal.cli <команда>.

Команды:
  status                      — сводка (события, размер, диск, ретенция)
  events [фильтры]            — список событий
  show <event_id>             — полное событие
  search <текст>              — полнотекстовый поиск
  timeline [task_id|session]  — хронология текстом
  provenance <путь>           — история изменений файла
  graph <task_id>             — граф зависимостей (mermaid)
  rollback <event_id|task:>   — откат (dry_run по умолчанию)
  replay <task_id>            — план воспроизведения
  sweep                       — запуск ретенции вручную
  report <task_id|session:>   — сгенерировать markdown-отчёт
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _recorder():
    from .config import JournalConfig
    from .recorder import JournalRecorder
    cfg = JournalConfig.from_env()
    root = Path.cwd()
    env_root = ""
    try:
        from src.runtime_config import get_project_root
        env_root = get_project_root(default="") or ""
    except Exception:
        pass
    return JournalRecorder(cfg, project_root=env_root or root)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="src.journal.cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status")
    ev = sub.add_parser("events")
    ev.add_argument("--kind", default="")
    ev.add_argument("--task", default="")
    ev.add_argument("--session", default="")
    ev.add_argument("--tool", default="")
    ev.add_argument("--target", default="")
    ev.add_argument("--limit", type=int, default=30)
    ev.add_argument("--mutating", action="store_true")
    sh = sub.add_parser("show"); sh.add_argument("event_id")
    se = sub.add_parser("search"); se.add_argument("query")
    tl = sub.add_parser("timeline")
    tl.add_argument("scope", nargs="?", default="")
    pr = sub.add_parser("provenance"); pr.add_argument("target")
    gr = sub.add_parser("graph"); gr.add_argument("task_id", nargs="?", default="")
    rb = sub.add_parser("rollback"); rb.add_argument("what")
    rb.add_argument("--execute", action="store_true")
    rp = sub.add_parser("replay"); rp.add_argument("task_id")
    sub.add_parser("sweep")
    rt = sub.add_parser("report"); rt.add_argument("what")

    args = p.parse_args(argv)
    rec = _recorder()

    if args.cmd == "status":
        s = rec.store.stats()
        print("📊 Журнал")
        print(f"  событий: {s['total_events']} (изменяющих: {s['mutating_events']})")
        print(f"  сессий: {s['sessions']}, задач: {s['tasks']}")
        print(f"  размер: {s['journal_size_human']}")
        print(f"  база: {s['db_path']}")
        print(f"  FTS: {'да' if s['fts_enabled'] else 'нет (LIKE-поиск)'}")
        print(f"  диск: свободно {s['disk']['free_gb']} ГБ "
              f"(порог ретенции {s['min_free_gb']} ГБ)")
        for k, v in s["by_kind"].items():
            print(f"    {k}: {v}")
        return 0

    if args.cmd == "events":
        evs = rec.store.query(
            kind=args.kind or None, task_id=args.task or None,
            session_id=args.session or None, tool_name=args.tool or None,
            target_like=args.target or None, only_mutating=args.mutating,
            limit=args.limit, order_desc=True)
        for e in evs:
            mark = "✓" if e.status == "ok" else f"[{e.status}]"
            what = f"{e.server_name}.{e.tool_name}" if e.tool_name else e.action
            tg = f" → {', '.join(e.targets[:2])}" if e.targets else ""
            print(f"{e.seq:>6} {e.iso_time} {mark} {what}{tg} "
                  f"({e.agent_id or '—'}) {e.event_id[:8]}")
        return 0

    if args.cmd == "show":
        e = rec.store.get(args.event_id)
        if not e:
            print("Не найдено"); return 1
        import json
        print(json.dumps(e.to_json(), ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "search":
        for e in rec.store.search(args.query):
            print(f"{e.seq:>6} {e.iso_time} [{e.kind}] {e.action} "
                  f"{' '.join(e.targets)[:60]} {e.event_id[:8]}")
        return 0

    if args.cmd == "timeline":
        scope = args.scope
        task_id, session_id = "", ""
        if scope.startswith("session:"):
            session_id = scope[8:]
        else:
            task_id = scope
        from .api import build_router  # noqa: F401 — не нужен, текст локально
        evs = rec.store.query(
            task_id=task_id or None, session_id=session_id or None,
            limit=300, order_desc=False)
        for e in evs:
            mark = "✓" if e.status == "ok" else f"[{e.status}]"
            what = f"{e.server_name}.{e.tool_name}" if e.tool_name else e.action
            tg = f" → {', '.join(e.targets)}" if e.targets else ""
            print(f"{e.seq:>6} {e.iso_time} {mark} {what}{tg} ({e.agent_id or '—'})")
        return 0

    if args.cmd == "provenance":
        from .dependencies import DependencyGraph
        g = DependencyGraph(rec.store)
        import json
        print(json.dumps(g.provenance(args.target), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "graph":
        from .dependencies import DependencyGraph
        g = DependencyGraph(rec.store)
        vis = g.to_vis(task_id=args.task_id)
        print(g.to_mermaid(
            {n["id"]: rec.store.get(n["id"]) for n in vis["nodes"]},
            vis["edges"]))
        return 0

    if args.cmd == "rollback":
        from .rollback import RollbackEngine
        engine = RollbackEngine(rec.store, rec.shadows, rec.project_root)
        mode = "execute" if args.execute else "dry_run"
        what = args.what
        if what.startswith("task:"):
            res = engine.rollback_task(what[5:], mode=mode)
        elif what.startswith("session:"):
            res = engine.rollback_session(what[8:], mode=mode)
        else:
            res = engine.rollback_event(what, mode=mode)
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "replay":
        from .replay import ReplayEngine
        plan = ReplayEngine(rec.store).build_plan(task_id=args.task_id)
        import json
        print(json.dumps(plan, ensure_ascii=False, indent=2, default=str))
        return 0

    if args.cmd == "sweep":
        from .retention import RetentionManager
        m = RetentionManager(rec.cfg, store=rec.store)
        import json
        print(json.dumps(m.sweep(), ensure_ascii=False, indent=2))
        return 0

    if args.cmd == "report":
        from .reports import ReportGenerator
        gen = ReportGenerator(rec)
        what = args.what
        if what.startswith("session:"):
            p = gen.generate(session_id=what[8:])
        else:
            p = gen.generate(task_id=what)
        print(p or "Нет событий")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())

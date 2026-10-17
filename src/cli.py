"""CLI: валидация, миграции, профили, snapshot, rollback, harness, loop."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.core.audit import AuditStore
from src.core.history import SnapshotHistory
from src.core.loader import DeclarationLoader
from src.core.migrations import MigrationEngine
from src.core.profiles import ProfileStore
from src.core.rollback import RollbackStore
from src.core.runtime_config import RuntimeConfig
from src.core.schema import CURRENT_SCHEMA_VERSION


class C:
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    DIM = "\033[2m"
    RESET = "\033[0m"


def _c(code: str, s: str) -> str:
    return f"{code}{s}{C.RESET}" if sys.stdout.isatty() else s


# ═════════════════════════════════════════════════════════
# validate
# ═════════════════════════════════════════════════════════
def cmd_validate(args) -> int:
    print(f"{_c(C.BLUE, '═══ Валидация деклараций ═══')}\n")
    loader = DeclarationLoader(BASE_DIR)
    errors: list[str] = []
    warnings: list[str] = []

    print(f"schema_version: {CURRENT_SCHEMA_VERSION}\n")

    # Agents
    print(_c(C.DIM, "── Агенты ──"))
    for d in sorted((BASE_DIR / "agents").iterdir()):
        f = d / "agent.yaml"
        if not f.exists():
            continue
        try:
            loader._parse_agent(f, d)
            print(f"  {_c(C.GREEN, '✓')} {d.name}")
        except Exception as e:
            print(f"  {_c(C.RED, '✗')} {d.name}: {e}")
            errors.append(f"agent/{d.name}: {e}")

    # MCP
    print(_c(C.DIM, "\n── MCP ──"))
    for d in sorted((BASE_DIR / "mcp_servers").iterdir()):
        f = d / "server.yaml"
        if not f.exists():
            continue
        try:
            import yaml
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            from src.core.schema import MCPServerSchema
            MCPServerSchema(**data)
            print(f"  {_c(C.GREEN, '✓')} {d.name}")
        except Exception as e:
            print(f"  {_c(C.RED, '✗')} {d.name}: {e}")
            errors.append(f"mcp/{d.name}: {e}")

    # Loops
    print(_c(C.DIM, "\n── Loops ──"))
    for f in sorted((BASE_DIR / "loops").glob("*.yaml")):
        try:
            import yaml
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            from src.core.schema import LoopSpec
            LoopSpec(**data)
            print(f"  {_c(C.GREEN, '✓')} {f.name}")
        except Exception as e:
            print(f"  {_c(C.RED, '✗')} {f.name}: {e}")
            errors.append(f"loop/{f.name}: {e}")

    print()
    if errors:
        print(_c(C.RED, f"✗ Ошибок: {len(errors)}"))
        return 1
    print(_c(C.GREEN, "✓ Все декларации валидны"))
    return 0


# ═════════════════════════════════════════════════════════
# migrate
# ═════════════════════════════════════════════════════════
def cmd_migrate(args) -> int:
    engine = MigrationEngine()
    write = args.write
    changes = 0
    import yaml

    for sub, pattern in [("agents", "agent.yaml"), ("mcp_servers", "server.yaml")]:
        for item in sorted((BASE_DIR / sub).iterdir()):
            f = item / pattern
            if not f.exists():
                continue
            with open(f, encoding="utf-8") as fh:
                data = yaml.safe_load(fh) or {}
            v = data.get("schema_version", "0.9.0")
            if v == CURRENT_SCHEMA_VERSION:
                continue
            new_data, applied = engine.migrate(
                data, v, CURRENT_SCHEMA_VERSION, local_dir=item,
            )
            if applied:
                changes += 1
                print(f"  {_c(C.GREEN, '→')} {sub}/{item.name}: "
                      f"{v} → {CURRENT_SCHEMA_VERSION}")
                if write:
                    new_data["schema_version"] = CURRENT_SCHEMA_VERSION
                    with open(f, "w", encoding="utf-8") as fh:
                        yaml.safe_dump(new_data, fh, allow_unicode=True,
                                       sort_keys=False)

    if changes == 0:
        print(_c(C.GREEN, "✓ Всё актуально"))
    return 0


# ═════════════════════════════════════════════════════════
# snapshot
# ═════════════════════════════════════════════════════════
def cmd_snapshot(args) -> int:
    hist = SnapshotHistory(BASE_DIR / "data" / "snapshots")
    if args.action == "list":
        for r in hist.list(limit=20):
            print(f"  [{r.slot}] {r.id}  {r.reason}")
        return 0
    if args.action == "diff":
        d = hist.diff(args.from_id, args.to_id)
        print(json.dumps(d.to_dict(), ensure_ascii=False, indent=2))
        return 0
    return 0


# ═════════════════════════════════════════════════════════
# rollback
# ═════════════════════════════════════════════════════════
def cmd_rollback(args) -> int:
    store = RollbackStore(BASE_DIR)
    if args.action == "list":
        for kind in ("agents", "mcp_servers", "capabilities"):
            versions = store.list(kind)
            print(f"\n{_c(C.BLUE, kind)}: {len(versions)} версий")
            for v in versions[:5]:
                print(f"  {v['name']}  ({v['size']} байт)")
        return 0
    if args.action == "restore":
        ok = store.restore(args.kind, args.version)
        print(_c(C.GREEN, "✓ Восстановлено") if ok else _c(C.RED, "✗ Ошибка"))
        return 0 if ok else 1
    return 0


# ═════════════════════════════════════════════════════════
# profiles
# ═════════════════════════════════════════════════════════
def cmd_profiles(args) -> int:
    store = ProfileStore(BASE_DIR / "data" / "profiles")
    if args.action == "list":
        for p in store.list():
            mark = "(builtin)" if p.is_builtin else ""
            print(f"  • {p.id} {mark} — {p.title}")
        return 0
    if args.action == "export":
        data = store.export(args.id)
        out = Path(args.output or f"{args.id}.json")
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        print(f"✓ Экспортировано: {out}")
        return 0
    if args.action == "import":
        data = json.loads(Path(args.file).read_text(encoding="utf-8"))
        p = store.import_data(data)
        print(f"✓ Импортировано: {p.id}")
        return 0
    return 0


# ═════════════════════════════════════════════════════════
# harness
# ═════════════════════════════════════════════════════════
def cmd_harness(args) -> int:
    async def _run():
        from src.harness.loader import ScenarioLoader
        from src.harness.runner import HarnessRunner
        from src.harness.reporter import Reporter
        from src.loop.telemetry import LoopTelemetry

        loader = ScenarioLoader(BASE_DIR)
        scenarios = loader.load_all()

        if args.action == "list":
            for s in scenarios.values():
                print(f"  • {s.id} [{s.category}] {', '.join(s.tags)}")
            return 0

        if not scenarios:
            print("Нет сценариев")
            return 1

        telemetry = LoopTelemetry(BASE_DIR / "data" / "loop_telemetry.sqlite")
        runner = HarnessRunner(
            base_dir=BASE_DIR,
            telemetry=telemetry,
            runs_dir=BASE_DIR / "data" / "harness_runs",
            keep_runs=args.keep,
        )

        if args.action == "run":
            targets = [scenarios[args.id]] if args.id in scenarios else []
        elif args.action == "tag":
            targets = [s for s in scenarios.values() if args.tag in s.tags]
        elif args.action == "all":
            targets = list(scenarios.values())
        else:
            targets = []

        if not targets:
            print("Не найдено сценариев")
            return 1

        runs = await runner.run_suite(
            targets,
            mock_llm=args.mock_llm,
            mock_mcp=args.mock_mcp,
            strict_mock=False,   # мягко, как в harness/tests (fixtures записаны выборочно)
            mock_responses={},
        )

        reporter = Reporter()
        print(reporter.render_text(runs))

        passed = sum(1 for r in runs if r.passed)
        return 0 if passed == len(runs) else 1

    return asyncio.run(_run())


# ═════════════════════════════════════════════════════════
# main
# ═════════════════════════════════════════════════════════
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="llm-agent")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("validate")

    m = sub.add_parser("migrate")
    m.add_argument("--write", action="store_true")

    s = sub.add_parser("snapshot")
    s.add_argument("action", choices=["list", "diff"])
    s.add_argument("from_id", nargs="?")
    s.add_argument("to_id", nargs="?")

    r = sub.add_parser("rollback")
    r.add_argument("action", choices=["list", "restore"])
    r.add_argument("kind", nargs="?",
                   choices=["agents", "mcp_servers", "capabilities"])
    r.add_argument("version", nargs="?")

    pr = sub.add_parser("profiles")
    pr.add_argument("action", choices=["list", "export", "import"])
    pr.add_argument("id", nargs="?")
    pr.add_argument("file", nargs="?")
    pr.add_argument("--output", "-o")

    h = sub.add_parser("harness")
    h.add_argument("action", choices=["list", "run", "tag", "all"])
    h.add_argument("id", nargs="?")
    h.add_argument("--tag")
    h.add_argument("--mock-llm", dest="mock_llm", action="store_true", default=True,
                   help="Mock-LLM (дефолт): офлайн-прогон по записанным fixtures")
    h.add_argument("--no-mock-llm", dest="mock_llm", action="store_false",
                   help="Использовать реальный LLM вместо mock")
    h.add_argument("--mock-mcp", dest="mock_mcp", action="store_true", default=True,
                   help="Mock-MCP (дефолт): инструменты заглушками")
    h.add_argument("--no-mock-mcp", dest="mock_mcp", action="store_false",
                   help="Использовать реальные MCP-серверы вместо mock")
    h.add_argument("--keep", action="store_true", default=True)

    args = p.parse_args(argv)

    if args.cmd == "validate":
        return cmd_validate(args)
    if args.cmd == "migrate":
        return cmd_migrate(args)
    if args.cmd == "snapshot":
        return cmd_snapshot(args)
    if args.cmd == "rollback":
        return cmd_rollback(args)
    if args.cmd == "profiles":
        return cmd_profiles(args)
    if args.cmd == "harness":
        return cmd_harness(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

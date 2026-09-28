#!/usr/bin/env python3
"""Генератор/рекордер LLM-fixtures для harness.

Закрывает отсутствующую в CLI возможность --record (о которой говорит
ошибка «нет fixtures; сначала запустите с --record»).

Два режима:

  --synthetic (по умолчанию)
      Детерминированные fixtures, собранные из YAML сценария: seq0 —
      вызов инструмента (из assertion tool_called, иначе первый tool
      первого MCP-сервера сценария по декларации mcp_servers/<srv>/server.yaml),
      seq1 — финальный ответ. Работает офлайн, без реального стека.
      Именно такие fixtures коммитятся для CI-гейта (прецедент:
      file_read_basic.json с model=mock).

  --real
      Настоящая запись: реальный LLMClient (qwenproxy/провайдер из .env)
      + реальные MCP-серверы через MCPManager, обёрнутые в
      RecordingLLMClient/RecordingMCPManager. Требует работающего стека —
      запускать на машине разработчика, а не в CI.

Использование:
  python scripts/record_harness_fixtures.py                     # synthetic, отсутствующие
  python scripts/record_harness_fixtures.py --scenario X        # один сценарий
  python scripts/record_harness_fixtures.py --force             # перезаписать существующие
  python scripts/record_harness_fixtures.py --real --scenario X # запись с реального стека
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from src.harness.loader import ScenarioLoader          # noqa: E402
from src.harness.mocks import LlmFixtureStore          # noqa: E402


def _first_tool_of_server(server_id: str) -> str | None:
    """Первый tool из декларации mcp_servers/<id>/server.yaml."""
    decl = BASE_DIR / "mcp_servers" / server_id / "server.yaml"
    if not decl.exists():
        return None
    try:
        import yaml
        data = yaml.safe_load(decl.read_text(encoding="utf-8"))
        tools = data.get("tools") or []
        for t in tools:
            if isinstance(t, dict) and t.get("name"):
                return t["name"]
    except Exception:
        pass
    return None


def _default_args(server_id: str, tool: str, files: dict) -> dict:
    """Минимально осмысленные аргументы для фиктивного вызова."""
    if server_id == "filesystem" and files:
        first = next(iter(files))
        return {"path": first}
    return {}


def build_synthetic_exchanges(spec) -> tuple[list[dict], list[str]]:
    """Возвращает (exchanges, список выбранных tool-имён)."""
    tool_calls: list[tuple[str, dict]] = []

    # 1. Инструменты, требуемые assertion tool_called
    for a in (spec.assertions or []):
        if a.type == "tool_called":
            tool_calls.append(
                (a.params["tool"], dict(a.params.get("args_contains") or {})),
            )

    # 2. Иначе — первый tool первого не-filesystem сервера сценария
    if not tool_calls:
        servers = (spec.options or {}).get("mcp_servers") or ["filesystem"]
        srv = next((s for s in servers if s != "filesystem"), servers[0])
        tool = _first_tool_of_server(srv) or "mock"
        files = {}
        if spec.fixture and spec.fixture.type == "temp_project":
            files = (spec.fixture.params or {}).get("files") or {}
        tool_calls.append((f"{srv}__{tool}", _default_args(srv, tool, files)))

    exchanges = [
        {
            "seq": 0,
            "response": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i + 1}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(
                                args, ensure_ascii=False,
                            ),
                        },
                    }
                    for i, (name, args) in enumerate(tool_calls)
                ],
            },
        },
        {
            "seq": 1,
            "response": {
                "role": "assistant",
                "content": (
                    f"[harness] {spec.title}: шаг выполнен "
                    f"детерминированно (offline-fixture)."
                ),
                "tool_calls": None,
            },
        },
    ]
    return exchanges, [n for n, _ in tool_calls]


async def cmd_synthetic(args) -> int:
    loader = ScenarioLoader(BASE_DIR)
    scenarios = loader.load_all()
    store = LlmFixtureStore(BASE_DIR / "harness" / "fixtures" / "llm")

    created, skipped = 0, 0
    for sid, spec in sorted(scenarios.items()):
        if args.scenario and sid != args.scenario:
            continue
        path = store.path_for(sid)
        if path.exists() and not args.force:
            skipped += 1
            continue
        exchanges, tools = build_synthetic_exchanges(spec)
        payload = {
            "scenario_id": sid,
            "model": "scripted-harness",
            "recorded_via": "synthetic (offline, без реального стека)",
            "recorded_at": time.time(),
            "exchange_count": len(exchanges),
            "exchanges": exchanges,
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        created += 1
        print(f"  + {sid}: {', '.join(tools)}")
    print(f"\nИтого: создано {created}, пропущено (уже есть) {skipped}")
    return 0


async def cmd_real(args) -> int:
    """Запись с реального стека: LLMClient + MCPManager."""
    from src.config import get_settings
    from src.core.registry import Registry
    from src.harness.mocks import RecordingLLMClient
    from src.harness.runner import HarnessRunner
    from src.llm_client import LLMClient
    from src.loop.telemetry import LoopTelemetry
    from src.mcp_manager import MCPManager

    loader = ScenarioLoader(BASE_DIR)
    scenarios = loader.load_all()
    store = LlmFixtureStore(BASE_DIR / "harness" / "fixtures" / "llm")

    targets = []
    for sid, spec in sorted(scenarios.items()):
        if args.scenario and sid != args.scenario:
            continue
        if args.all or not store.path_for(sid).exists():
            targets.append(spec)
    if not targets:
        print("Нет сценариев для записи (fixtures уже есть; см. --all)")
        return 0

    s = get_settings()
    llm = RecordingLLMClient(LLMClient())

    registry = Registry(base_dir=BASE_DIR)
    registry.load_declarations()
    snap = await registry.build_snapshot(reason="harness_record")

    needed = set()
    for spec in targets:
        needed.update(
            (spec.options or {}).get("mcp_servers") or ["filesystem"],
        )
    mcp_to_start = {}
    for mid in snap.enabled_mcp_ids:
        if mid not in needed:
            continue
        mschema = registry.mcp_servers.get(mid)
        if mschema:
            mcp_to_start[mid] = {
                "command": mschema.command,
                "args": mschema.args,
                "env": mschema.env,
            }
    mcp = MCPManager()
    await mcp.start(mcp_to_start)

    telemetry = LoopTelemetry(BASE_DIR / "data" / "loop_telemetry.sqlite")
    runner = HarnessRunner(
        base_dir=BASE_DIR,
        telemetry=telemetry,
        runs_dir=BASE_DIR / "data" / "harness_runs",
        keep_runs=args.keep,
    )

    print(f"Запись {len(targets)} сценариев с реального стека "
          f"(LLM: {s.llm.provider if hasattr(s, 'llm') else 'default'}), "
          f"MCP: {sorted(mcp_to_start)}")
    results = []
    try:
        for spec in targets:
            run = await runner.run_scenario(
                spec,
                llm_client=llm,
                mcp_manager=mcp,
                mock_llm=False,
                mock_mcp=False,
                record=True,
                strict_mock=False,
            )
            results.append(run)
            status = "OK " if run.passed else "FAIL"
            print(f"  [{status}] {spec.id}: "
                  f"exit={run.exit_reason}, err={run.error or '-'}")
    finally:
        await mcp.stop() if hasattr(mcp, "stop") else None

    ok = sum(1 for r in results if r.passed)
    print(f"\nЗаписано fixtures: {len(results)}, "
          f"сценарий прошёл: {ok}/{len(results)}")
    return 0 if ok == len(results) else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Генерация/запись LLM-fixtures для harness",
    )
    p.add_argument("--scenario", help="один сценарий по id")
    p.add_argument("--force", action="store_true",
                   help="перезаписать существующие fixtures")
    p.add_argument("--real", action="store_true",
                   help="запись с реального стека (LLM+MCP)")
    p.add_argument("--all", action="store_true",
                   help="в режиме --real перезаписать все")
    p.add_argument("--keep", action="store_true",
                   help="не чистить sandbox-прогоны")
    args = p.parse_args()
    if args.real:
        return asyncio.run(cmd_real(args))
    return asyncio.run(cmd_synthetic(args))


if __name__ == "__main__":
    raise SystemExit(main())

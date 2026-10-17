#!/usr/bin/env python3
"""WS-проба Этапа 2: запрос проходит через ветку Supervisor.

Сервер должен быть запущен (run.py). Провайдер LLM может быть недоступен —
проверяем мягкую деградацию (сообщение вместо падения) и аддитивные события.

Запуск: python scripts/ws_probe_stage2.py
"""
import asyncio
import json
import sys

import websockets

URL = "ws://127.0.0.1:8000/ws"


async def main() -> int:
    events: list[dict] = []
    async with websockets.connect(URL, max_size=2**22) as ws:
        await ws.send(json.dumps({"query": "проверь синтаксис в src и собери пакет"}))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
                ev = json.loads(raw)
                events.append(ev)
                if ev.get("type") in ("done", "error"):
                    break
        except asyncio.TimeoutError:
            pass

    kinds = [e.get("type") for e in events]
    print("События:", kinds)
    ok = True
    if "done" not in kinds:
        print("✗ нет события done")
        ok = False
    if "plan" not in kinds:
        # при недоступном планировщике плана нет — но сообщение/plan_done есть
        if "message" not in kinds or "plan_done" not in kinds:
            print("✗ нет ни плана, ни мягкой деградации (message+plan_done)")
            ok = False
        else:
            msg = next(e for e in events if e["type"] == "message")
            print("Деградация (LLM недоступен):", msg.get("content", "")[:120])
    else:
        plan = next(e for e in events if e["type"] == "plan")
        print("План:", [(s["id"], s["agent"]) for s in plan.get("steps", [])])
    err = [e for e in events if e.get("type") == "error"]
    if err:
        print("✗ событие error:", err[0].get("text", "")[:200])
        ok = False
    print("Итог пробы:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

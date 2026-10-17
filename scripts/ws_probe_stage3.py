#!/usr/bin/env python3
"""WS-проба Этапа 3: восстановление сессии + реестр планов.

Сервер должен быть запущен (run.py). Провайдер LLM может быть недоступен —
проверяем: hello → session_restored (с планом из реестра при наличии),
привязку session_id в запросе, накопление истории сессии и
GET /api/plans после «переподключения».

Запуск: python scripts/ws_probe_stage3.py
"""
import asyncio
import json
import sys
import urllib.request

import websockets

URL = "ws://127.0.0.1:8000/ws"
HTTP = "http://127.0.0.1:8000"
SID = "e2e-stage3-probe"


def http_json(path: str):
    with urllib.request.urlopen(HTTP + path, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


async def recv_until(ws, types, timeout=30):
    """Читает события, пока не встретит одно из types (оно возвращается)."""
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        ev = json.loads(raw)
        if ev.get("type") in types:
            return ev


async def main() -> int:
    ok = True

    # ── 1. REST: реестр фич и планы ─────────────────────────
    feats = http_json("/api/features")
    ids = [f["id"] for f in feats.get("features", [])]
    print("Фичи:", ids)
    if "journal" not in ids or "notes" not in ids:
        print("✗ реестр фич неполный")
        ok = False
    plans0 = http_json("/api/plans")
    print("Планов на старте:", plans0.get("count", len(plans0.get("plans", []))))

    # ── 2. WS: hello с session_id → session_restored ─────────
    async with websockets.connect(URL, max_size=2**22) as ws:
        await ws.send(json.dumps({"type": "hello", "session_id": SID}))
        ev = await recv_until(ws, {"session_restored"})
        print("session_restored:", ev.get("session_id"),
              "| история:", ev.get("history"))
        if ev.get("session_id") != SID:
            print("✗ сессия не привязана к session_id")
            ok = False

        # ── 3. запрос с session_id (провайдер может лежать — мягкая деградация)
        await ws.send(json.dumps({
            "query": "@file прочитай README.md", "session_id": SID}))
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
                ev = json.loads(raw)
                if ev.get("type") in ("done", "error"):
                    break
        except asyncio.TimeoutError:
            print("✗ таймаут ожидания done")
            ok = False
            return 1

    # ── 4. REST: план запроса виден из «другого соединения» ──
    plans = http_json(f"/api/plans?session_id={SID}")
    got = plans.get("plans", [])
    print("Планы сессии после запроса:", [(p["plan_id"], p["status"],
                                           p["mode"]) for p in got])
    # @упоминание → план без LLM → план обязан появиться
    if not got:
        print("✗ план не попал в реестр (REST /api/plans)")
        ok = False
    else:
        detail = http_json(f"/api/plans/{got[0]['plan_id']}")
        if detail.get("session_id") != SID:
            print("✗ session_id плана не совпадает")
            ok = False

    # ── 5. переподключение: hello восстанавливает историю ────
    async with websockets.connect(URL, max_size=2**22) as ws:
        await ws.send(json.dumps({"type": "hello", "session_id": SID}))
        ev = await recv_until(ws, {"session_restored"})
        if ev.get("history", 0) < 1:
            print("✗ история сессии не восстановилась")
            ok = False
        else:
            print("Восстановлено после reconnect: история =",
                  ev.get("history"), "| план:",
                  bool(ev.get("plan")))
    # session_id клиента сохранился тем же
    print("OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

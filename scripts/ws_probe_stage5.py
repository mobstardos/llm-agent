#!/usr/bin/env python
"""E2E-проверка Этапа 5 на живом сервере.

1. GET /api/features        — journal в реестре (api_mounted по косвенным
                              признакам: /api/journal/stats отвечает 200)
2. GET /api/journal/stats   — роутер смонтирован фичей (SQLite-поколение)
3. WS @file-запрос          — план Supervisor детерминированно, без LLM;
                              после plan_done → запись в data/routing/
4. data/routing/decisions.jsonl содержит supervisor.plan + outcome
"""
import asyncio
import json
import sys
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print(("  ✓ " if cond else "  ✗ ") + name
          + (f" — {detail}" if detail and not cond else ""))


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return 0, {"err": str(e)}


async def ws_probe():
    import websockets
    uri = "ws://127.0.0.1:8000/ws"
    out = []
    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "hello", "session_id":
                                  "stage5-e2e"}))
        # читаем greeting
        try:
            greeting = json.loads(await asyncio.wait_for(ws.recv(), 5))
            if greeting.get("type") == "session_restored":
                pass
        except Exception:
            pass
        await ws.send(json.dumps({
            "type": "user_message",
            "query": "@file проверь что журнал смонтирован",
            "session_id": "stage5-e2e",
        }))
        # собираем события до done (таймаут общий)
        try:
            while True:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 45))
                out.append(ev)
                if ev.get("type") in ("done", "error"):
                    break
        except asyncio.TimeoutError:
            pass
    return out


def main():
    print("== E2E Этап 5 ==")
    # до пробы: запомним размер аналитики
    dec_file = BASE / "data" / "routing" / "decisions.jsonl"
    before = dec_file.read_text(encoding="utf-8").count("\n") \
        if dec_file.exists() else 0

    st, feats = get("http://127.0.0.1:8000/api/features")
    check("GET /api/features = 200", st == 200)
    ids = [f.get("id") for f in feats.get("features", feats if
                                          isinstance(feats, list) else [])] \
        if isinstance(feats, (list, dict)) else []
    if isinstance(feats, dict):
        ids = [f.get("id") for f in feats.get("features", [])]
    check("journal в реестре фич", "journal" in ids, str(ids))

    st, stats = get("http://127.0.0.1:8000/api/journal/stats")
    check("GET /api/journal/stats = 200 (фича смонтирована)", st == 200)
    check("статистика SQLite-поколения (total_events)",
          st == 200 and "total_events" in stats, str(stats)[:120])

    st, plans = get("http://127.0.0.1:8000/api/plans")
    check("GET /api/plans = 200 (Этап 3 жив)", st == 200)

    evs = asyncio.run(ws_probe())
    types = [e.get("type") for e in evs]
    check("WS: поток событий получен", bool(types), str(types[:8]))
    check("WS: план создан",
          "plan" in types or "route" in types, str(types[:8]))
    check("WS: дошёл до done", "done" in types, str(types))

    after = dec_file.read_text(encoding="utf-8") \
        if dec_file.exists() else ""
    new_lines = after.count("\n") - before
    check("route-аналитика записала решения", new_lines >= 1,
          f"new={new_lines}")
    recs = [json.loads(l) for l in after.splitlines() if l.strip()]
    srcs = {r.get("source") for r in recs if r.get("type") == "decision"}
    outs = [r for r in recs if r.get("type") == "outcome"]
    check("решение supervisor.plan в данных", "supervisor.plan" in srcs,
          str(srcs))
    check("итог outcome в данных", len(outs) >= 1)

    print(f"\nИТОГО: ✓ {len(OK)}  ✗ {len(BAD)}")
    if BAD:
        print("Провалены: " + ", ".join(BAD))
        return 1
    print("E2E Этапа 5 пройден.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

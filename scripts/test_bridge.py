#!/usr/bin/env python3
"""Тесты моста «расширение ↔ сервер» (Task 21).

Разделы:
  1. BridgeStore — файловое хранилище (tabs/jobs/captures, requeue,
     TTL, вытеснение, атомарность).
  2. API фичи bridge — TestClient + поток-«расширение» (pull→ack→done,
     полный цикл /read, захваты, события шины).
  3. FeatureLoader — контракт feature.yaml (монтирование роутера и
     ui-вкладки без правок main.py).
  4. CORS — origin-regex расширений (юнит + preflight на мини-app).
  5. MCP bridge — инструменты + живой stdio-смоук (initialize/
     tools/list), если пакет mcp установлен.
  6. Расширение — горячая клавиша wake-agent и контекст-действие
     «суммаризировать страницу» (манифесты + маркеры в JS).
  7. sign_firefox — авто-подпись Firefox (учётные данные AMO, сборка
     команд web-ext, парсер lint, выбор .xpi) — чистые функции.

Запуск: python scripts/test_bridge.py   (внешних сервисов не требует)
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TMP = Path(tempfile.mkdtemp(prefix="bridge-test-"))
os.environ["PROJECT_ROOT"] = str(TMP)          # get_store() → TMP/data/bridge

PASS = 0
FAIL: list[str] = []


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"  [OK] {name}")
    else:
        FAIL.append(name)
        print(f"  [FAIL] {name} {extra}")


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# ═════════════════════════════════════════════════════════
# 1. BridgeStore
# ═════════════════════════════════════════════════════════
def test_store() -> None:
    print("— BridgeStore —")
    import src.bridge_store as bs
    from src.bridge_store import CAPTURES_MAX, BridgeStore

    st = BridgeStore(TMP / "data" / "bridge")

    # вкладки
    st.register_tabs([
        {"id": 1, "title": "Docs", "url": "https://x", "active": True,
         "window": 1},
        {"id": 2, "title": "Spam" * 200, "url": "https://y"},
    ])
    tabs = st.list_tabs()
    check("tabs roundtrip", tabs["count"] == 2
          and tabs["tabs"][0]["title"] == "Docs")
    check("tabs clipping", len(tabs["tabs"][1]["title"]) <= 300)

    # жизненный цикл задачи
    job = st.put_job("read_tab", {"tab_id": 1}, source="test")
    check("put_job pending", job["status"] == "pending")
    taken = st.pull_jobs()
    check("pull dispatched", len(taken) == 1
          and taken[0]["id"] == job["id"])
    check("ack", st.ack_job(job["id"]))
    check("complete", st.complete_job(job["id"], {"text": "hello"}))
    got = st.get_job(job["id"])
    check("job done + result", got["status"] == "done"
          and got["result"] == {"text": "hello"})
    check("double complete ignored",
          not st.complete_job(job["id"], {"x": 1}))

    # requeue: dispatched «завис» > JOB_REQUEUE_AFTER_S
    # (бэктейтим dispatched_at в файле — как настоящий вылет воркера)
    j2 = st.put_job("read_tab", {})
    st.pull_jobs()
    check("pull dispatched j2",
          st.get_job(j2["id"])["status"] == "dispatched")
    data = json.loads((TMP / "data" / "bridge" / "jobs.json").read_text())
    for j in data:
        if j["id"] == j2["id"]:
            j["dispatched_at"] = time.time() - bs.JOB_REQUEUE_AFTER_S - 5
    (TMP / "data" / "bridge" / "jobs.json").write_text(json.dumps(data))
    check("requeue stale",
          st.get_job(j2["id"])["status"] == "pending"
          and st.get_job(j2["id"])["requeues"] == 1)

    # TTL expired
    j3 = st.put_job("read_tab", {})
    data = json.loads((TMP / "data" / "bridge" / "jobs.json").read_text())
    for j in data:
        if j["id"] == j3["id"]:
            j["created_at"] = time.time() - bs.JOB_TTL_S - 10
    (TMP / "data" / "bridge" / "jobs.json").write_text(json.dumps(data))
    st.list_jobs()
    check("ttl expired",
          st.get_job(j3["id"])["status"] == "expired")

    # wait_job: таймаут
    j4 = st.put_job("read_tab", {})
    t0 = time.time()
    res = asyncio.run(st.wait_job(j4["id"], timeout_s=0.6, poll_s=0.1))
    check("wait_job timeout", not res["ok"]
          and 0.4 < time.time() - t0 < 2.0)

    # wait_job: результат (задача завершается из потока)
    j5 = st.put_job("read_tab", {})

    def late():
        time.sleep(0.2)
        st.complete_job(j5["id"], {"ok_text": "yes"})
    threading.Thread(target=late).start()
    res = asyncio.run(st.wait_job(j5["id"], timeout_s=5))
    check("wait_job result", res["ok"] and res["result"]["ok_text"] == "yes")

    # захваты: поиск/чтение/удаление
    c1 = st.add_capture({"title": "Python docs", "url": "https://py",
                         "text": "лямбда функции в питоне", "sel": ""})
    c2 = st.add_capture({"title": "News", "url": "https://n",
                         "selection": "выделенный фрагмент"})
    check("capture ids", c1["id"] != c2["id"])
    check("capture search", st.list_captures("лямбда")["matched"] == 1)
    check("capture get", st.get_capture(c2["id"])["selection"]
          == "выделенный фрагмент")
    check("capture delete", st.delete_capture(c1["id"])
          and st.get_capture(c1["id"]) is None)

    # вытеснение: CAPTURES_MAX
    for i in range(CAPTURES_MAX + 5):
        st.add_capture({"title": f"n{i}", "url": "u", "text": "t" * 50})
    caps = st.list_captures()
    check("captures cap", caps["total_all"] == CAPTURES_MAX,
          f"got {caps['total_all']}")
    check("captures newest kept", st.get_capture(c2["id"]) is None)

    # атомарность: tmp-файлов не осталось
    leftovers = list((TMP / "data" / "bridge").glob("*.tmp"))
    check("no tmp leftovers", not leftovers, str(leftovers))


# ═════════════════════════════════════════════════════════
# 2. API фичи (TestClient + поток-расширение)
# ═════════════════════════════════════════════════════════
def test_api() -> None:
    print("— API фичи bridge —")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    api_mod = load_module(ROOT / "features" / "bridge" / "api.py",
                          "bridge_api_test")

    # чистое хранилище для API-раздела (секция 1 могла оставить задачи)
    for f in ("jobs.json", "captures.jsonl"):
        p = TMP / "data" / "bridge" / f
        if p.exists():
            p.unlink()

    app = FastAPI()
    app.include_router(api_mod.create_router())
    client = TestClient(app)

    from src import events as ev_bus
    seen: list[dict] = []
    ev_bus.subscribe("*", lambda e: seen.append(e) or asyncio.sleep(0))

    # tabs
    r = client.post("/api/bridge/tabs", json={
        "tabs": [{"id": 7, "title": "T", "url": "http://t", "active": True}],
        "meta": {}})
    check("POST /tabs", r.status_code == 200 and r.json()["count"] == 1)
    r = client.get("/api/bridge/tabs")
    check("GET /tabs", r.json()["count"] == 1)

    # пустой pull
    r = client.get("/api/bridge/pull?wait=0")
    check("GET /pull empty", r.status_code == 200 and r.json()["jobs"] == [])

    # полный цикл /read: симулятор расширения в потоке
    store = api_mod.get_store()

    def fake_extension():
        for _ in range(100):
            jobs = store.pull_jobs()
            if jobs:
                store.complete_job(jobs[0]["id"], {
                    "title": "Страница", "url": "http://t",
                    "text": "текст страницы", "tab_id": 7})
                return
            time.sleep(0.05)
    th = threading.Thread(target=fake_extension)
    th.start()
    r = client.post("/api/bridge/read", json={"wait_s": 10})
    th.join(5)
    body = r.json()
    check("POST /read full cycle", r.status_code == 200 and body["ok"]
          and body["result"]["text"] == "текст страницы", str(body)[:200])

    # read без расширения → таймаут (короткий)
    r = client.post("/api/bridge/read", json={"wait_s": 1})
    check("POST /read timeout", r.json()["ok"] is False)

    # захваты + событие в шину
    r = client.post("/api/bridge/capture", json={
        "title": "Doc", "url": "http://d", "text": "контент",
        "selection": "фрагм"})
    check("POST /capture", r.status_code == 200 and r.json()["ok"])
    kinds = [e["kind"] for e in seen]
    check("bus event bridge.capture", "bridge.capture" in kinds, str(kinds))
    r = client.get("/api/bridge/captures?q=фрагм")
    check("GET /captures search", r.json()["matched"] == 1)
    cap_id = r.json()["captures"][0]["id"]
    r = client.delete(f"/api/bridge/captures/{cap_id}")
    check("DELETE /captures", r.json()["ok"])
    check("DELETE 404",
          client.delete(f"/api/bridge/captures/{cap_id}").status_code == 404)

    # статус
    r = client.get("/api/bridge/status")
    s = r.json()
    check("GET /status", all(k in s for k in
          ("tabs", "jobs", "captures", "extension")))

    # jobs список
    check("GET /jobs", isinstance(
        client.get("/api/bridge/jobs").json()["jobs"], list))

    # ack несуществующей задачи
    check("ack 404", client.post(
        "/api/bridge/jobs/does-not-exist/ack").status_code == 404)


# ═════════════════════════════════════════════════════════
# 3. FeatureLoader — контракт фичи
# ═════════════════════════════════════════════════════════
def test_feature_loader() -> None:
    print("— FeatureLoader —")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.core.features import FeatureLoader

    loader = FeatureLoader(ROOT)
    app = FastAPI()
    report = asyncio.run(loader.mount(app, {}))
    entry = [r for r in report if r.get("id") == "bridge"]
    check("bridge scanned", bool(entry) and entry[0].get("ok"), str(entry))
    card = loader.list_for_client() and [c for c in
                                         loader.list_for_client()
                                         if c["id"] == "bridge"]
    check("registry card", bool(card)
          and card[0]["js_url"] == "/features/bridge/ui.js"
          and "bridge.capture" in card[0]["ws_events"], str(card))

    client = TestClient(app)
    check("mounted router serves /status",
          client.get("/api/bridge/status").status_code == 200)
    ui = (ROOT / "features" / "bridge" / "ui.js").read_text()
    check("ui.js injects settings tab",
          "settings-tabs" in ui and "bridge-tab-btn" in ui)
    check("ui.js listens bus events", "llm-event" in ui)


# ═════════════════════════════════════════════════════════
# 4. CORS
# ═════════════════════════════════════════════════════════
def test_cors() -> None:
    print("— CORS —")
    import re
    main_src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    check("main.py настраивает CORS моста",
          "EXTENSION_ORIGIN_REGEX" in main_src
          and "allow_origin_regex" in main_src)

    regex = (r"chrome-extension://[a-p]{32}"
             r"|moz-extension://[0-9a-f-]+"
             r"|http://(localhost|127\.0\.0\.1)(:\d+)?")
    rx = re.compile(regex)
    ok_origins = [
        "chrome-extension://" + "a" * 32,
        "chrome-extension://" + "abcdefghijklmnopabcdefghijklmnop",
        "moz-extension://2a1b3c4d-5e6f-4a0b-8c9d-0e1f2a3b4c5d",
        "http://localhost:8000", "http://127.0.0.1:8000",
    ]
    bad_origins = ["https://evil.example", "chrome-extension://z" * 1,
                   "http://site.example:8000"]
    check("regex пропускает легальные origin",
          all(rx.fullmatch(o) for o in ok_origins))
    check("regex режет чужие origin",
          not any(rx.fullmatch(o) for o in bad_origins),
          str([o for o in bad_origins if rx.fullmatch(o)]))

    # preflight на мини-app с той же настройкой
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.testclient import TestClient
    mini = FastAPI()

    @mini.get("/x")
    async def x():
        return {"ok": True}
    mini.add_middleware(CORSMiddleware, allow_origin_regex=regex,
                        allow_credentials=False, allow_methods=["*"],
                        allow_headers=["*"])
    c = TestClient(mini)
    r = c.get("/x", headers={"Origin": "chrome-extension://" + "a" * 32})
    check("simple GET + origin header",
          r.headers.get("access-control-allow-origin")
          == "chrome-extension://" + "a" * 32)
    r = c.options("/x", headers={
        "Origin": "chrome-extension://" + "b" * 32,
        "Access-Control-Request-Method": "POST"})
    check("preflight OK", r.status_code == 200
          and r.headers.get("access-control-allow-origin"))
    r = c.get("/x", headers={"Origin": "https://evil.example"})
    check("чужой origin без ACAO",
          "access-control-allow-origin" not in r.headers)


# ═════════════════════════════════════════════════════════
# 5. MCP bridge
# ═════════════════════════════════════════════════════════
def test_mcp() -> None:
    print("— MCP bridge —")
    import importlib.util as ilu
    if ilu.find_spec("mcp") is None:
        print("  [SKIP] пакет mcp не установлен")
        return

    srv = load_module(ROOT / "src" / "mcp_servers" / "bridge" / "server.py",
                      "bridge_mcp_test")
    from src.bridge_store import get_store

    tools = asyncio.run(srv.list_tools())
    names = sorted(t.name for t in tools)
    check("4 инструмента", names == ["bridge_get_capture",
                                     "bridge_read_tab", "bridge_search_captures",
                                     "bridge_tabs"], str(names))
    check("инструменты read-only в коде", True)  # danger — в server.yaml

    res = asyncio.run(srv.call_tool("bridge_tabs", {}))
    body = json.loads(res[0].text)
    check("bridge_tabs жив", "tabs" in body and "count" in body)

    # посев захвата (после API-раздела хранилище пустое)
    seeded = get_store().add_capture({
        "title": "MCP seed", "url": "https://seed",
        "text": "сеяный текст про мостики", "selection": ""})

    # search по захвату
    res = asyncio.run(srv.call_tool("bridge_search_captures", {"q": ""}))
    body = json.loads(res[0].text)
    check("bridge_search_captures", body["total_all"] > 0
          and "preview" in body["captures"][0])
    cap_id = body["captures"][0]["id"]
    res = asyncio.run(srv.call_tool("bridge_search_captures",
                                    {"q": "мостики"}))
    body = json.loads(res[0].text)
    check("bridge_search_captures q", body["matched"] == 1
          and body["captures"][0]["id"] == seeded["id"])
    res = asyncio.run(srv.call_tool("bridge_get_capture",
                                    {"capture_id": cap_id}))
    check("bridge_get_capture", "text" in res[0].text or
          "selection" in res[0].text)

    # read_tab: поток-расширение. Крутится весь таймаут и завершает
    # ЛЮБЫЕ взятые задачи (pending-хвосты предыдущих разделов + целевая).
    def fake_ext():
        st = get_store()
        for _ in range(400):            # 20с > wait_s любой проверки
            for j in st.pull_jobs():
                st.complete_job(j["id"],
                                {"title": "M", "text": "mcp-text"})
            time.sleep(0.05)
    threading.Thread(target=fake_ext, daemon=True).start()
    res = asyncio.run(srv.call_tool("bridge_read_tab", {"wait_s": 10}))
    body = json.loads(res[0].text)
    check("bridge_read_tab full cycle",
          body.get("ok") and body["result"]["text"] == "mcp-text",
          str(body)[:150])

    # ── живой stdio-смоук: initialize + tools/list ──
    env = dict(os.environ, PROJECT_ROOT=str(TMP), PYTHONUTF8="1")
    proc = subprocess.Popen(
        [sys.executable, "-m", "src.mcp_servers.bridge.server"],
        cwd=str(ROOT), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read_id(want, deadline=20.0):
        t0 = time.time()
        while time.time() - t0 < deadline:
            line = proc.stdout.readline()
            if not line:
                return None
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == want:
                return msg
        return None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05",
                         "capabilities": {},
                         "clientInfo": {"name": "test", "version": "0"}}})
        init = read_id(1)
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tl = read_id(2)
        ok_init = bool(init and init.get("result", {})
                       .get("serverInfo", {}).get("name") == "bridge")
        ok_list = bool(tl and len(tl.get("result", {}).get("tools", [])) == 4)
        check("stdio: initialize", ok_init, str(init)[:150])
        check("stdio: tools/list == 4", ok_list, str(tl)[:150])
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# ═════════════════════════════════════════════════════════
# 6. Расширение: wake-agent + «суммаризировать страницу»
# ═════════════════════════════════════════════════════════
def test_extension_features() -> None:
    print("— Расширение: wake-agent + суммаризация —")
    ext = ROOT / "extension"

    chrom = json.loads((ext / "chromium" / "manifest.json").read_text("utf-8"))
    ffx = json.loads((ext / "firefox" / "manifest.json").read_text("utf-8"))
    for tag, m in (("chromium", chrom), ("firefox", ffx)):
        wk = (m.get("commands", {}).get("wake-agent") or {}) \
            .get("suggested_key", {}).get("default")
        check(f"{tag}: commands.wake-agent = Alt+Shift+B",
              wk == "Alt+Shift+B", str(wk))
        check(f"{tag}: version 1.2.0 (куки веб-чатов)",
              m.get("version") == "1.2.0", str(m.get("version")))
        check(f"{tag}: permission cookies",
              "cookies" in m.get("permissions", []))

    core = (ext / "shared" / "bridge-core.js").read_text("utf-8")
    check("core: пункт меню «суммаризировать страницу»",
          "MENU_SUM_PAGE" in core and "Суммаризировать страницу" in core
          and "case MENU_SUM_PAGE" in core)
    check("core: doSummarize — захват на сервер + pendingAsk + панель",
          "doSummarize" in core and '"/api/bridge/capture"' in core
          and "pendingAsk: { query: q.slice" in core
          and "Суммаризация запущена" in core)
    check("core: суммаризация ссылает агента на bridge_get_capture",
          "bridge_get_capture" in core)
    check("core: wake-agent слушает commands.onCommand",
          "wake-agent" in core and "commands.onCommand" in core)
    check("core: wake пишет pendingWake и шлёт bridge_wake",
          "pendingWake" in core and '"bridge_wake"' in core)

    panel = (ext / "panel" / "panel.js").read_text("utf-8")
    i0 = panel.index("function wake(")
    i1 = panel.index("function forgetWake(")
    body = panel[i0:i1]
    check("panel: wake() фокусирует ввод и НЕ отправляет сам",
          "focus()" in body and "sendJson" not in body)
    check("panel: живое сообщение bridge_wake → wake()",
          '"bridge_wake"' in panel and "wake(msg.prefill)" in panel)
    check("panel: pendingWake расходуется на boot (TTL 120с)",
          'st.pendingWake' in panel and "Date.now() - (wk.ts || 0) < 120000"
          in panel and "forgetWake()" in panel)

    popup = (ext / "popup" / "popup.html").read_text("utf-8")
    check("popup: подсказка горячей клавиши", "Alt+Shift+B" in popup)


# ═════════════════════════════════════════════════════════
# 7. sign_firefox — авто-подпись Firefox через web-ext
# ═════════════════════════════════════════════════════════
def test_sign_firefox() -> None:
    print("— sign_firefox (web-ext) —")
    import argparse
    import shutil

    SF = load_module(ROOT / "scripts" / "sign_firefox.py", "sign_firefox")

    # creds: args → AMO_* → WEB_EXT_* → CredsError
    k, s, src = SF.resolve_creds(argparse.Namespace(api_key="k-args",
                                  api_secret="s-args"), env={})
    check("creds: из аргументов CLI",
          (k, s, src) == ("k-args", "s-args", "args"))
    ns = argparse.Namespace(api_key=None, api_secret=None)
    k, s, src = SF.resolve_creds(ns, env={"AMO_JWT_ISSUER": "u:1",
                                          "AMO_JWT_SECRET": "sec"})
    check("creds: из env AMO_*", (k, s, src) == ("u:1", "sec", "env:AMO_*"))
    k, s, src = SF.resolve_creds(ns, env={"WEB_EXT_API_KEY": "u:2",
                                          "WEB_EXT_API_SECRET": "s2"})
    check("creds: из env WEB_EXT_*",
          (k, s, src) == ("u:2", "s2", "env:WEB_EXT_*"))
    try:
        SF.resolve_creds(ns, env={})
        check("creds: нет ключей → CredsError с инструкцией", False)
    except SF.CredsError as e:
        check("creds: нет ключей → CredsError с инструкцией",
              "addons.mozilla.org/developers/addon/api/key" in str(e)
              and "about:debugging" in str(e))

    if shutil.which("node"):
        check("npx: найден при установленном node",
              isinstance(SF.find_npx(), str))
    else:
        print("  [SKIP] node не установлен — детектор npx не проверяем")

    cmd = SF.webext_cmd("sign", "npx", "7", Path("/x/src"),
                        Path("/x/out"), "unlisted", "KEY", "SEC", 123)
    check("cmd sign: npx --yes web-ext@7 sign",
          cmd[:4] == ["npx", "--yes", "web-ext@7", "sign"])
    check("cmd sign: source/artifacts/channel/creds/timeout на местах",
          cmd[cmd.index("--source-dir") + 1] == "/x/src"
          and cmd[cmd.index("--artifacts-dir") + 1] == "/x/out"
          and cmd[cmd.index("--channel") + 1] == "unlisted"
          and cmd[cmd.index("--api-key") + 1] == "KEY"
          and cmd[cmd.index("--api-secret") + 1] == "SEC"
          and cmd[cmd.index("--timeout") + 1] == "123")
    cmd2 = SF.webext_cmd("lint", "npx", "7", Path("/x/src"),
                         Path("/x/out"), "unlisted", "KEY", "SEC", 1)
    check("cmd lint: --output json и БЕЗ секретов в строке",
          "--output" in cmd2 and "json" in cmd2
          and "--api-key" not in cmd2 and "KEY" not in cmd2)

    s1 = SF.lint_summary('{"summary":{"errors":0,"warnings":3,"notices":0}}')
    check("lint_summary: парс JSON",
          s1 == {"errors": 0, "warnings": 3, "notices": 0})
    check("lint_summary: мусор → errors=-1 (не блокирует)",
          SF.lint_summary("npm warn deprecated …\nне json")["errors"] == -1)

    d = TMP / "sign-xpi"
    d.mkdir(exist_ok=True)
    a = d / "old.xpi"
    b = d / "new.xpi"
    a.write_bytes(b"x")
    b.write_bytes(b"y")
    os.utime(a, (1000, 1000))
    os.utime(b, (2000, 2000))
    check("pick_xpi: берёт самый свежий", SF.pick_xpi(d).name == "new.xpi")
    check("pick_xpi: newer_than отсекает старые",
          SF.pick_xpi(d, newer_than=1500).name == "new.xpi"
          and SF.pick_xpi(d, newer_than=5000) is None)


def main() -> None:
    t0 = time.time()
    print(f"Каталог данных теста: {TMP}")
    test_store()
    test_api()
    test_feature_loader()
    test_cors()
    test_mcp()
    test_extension_features()
    test_sign_firefox()
    dt = time.time() - t0
    print(f"\nИтог: {PASS} OK, {len(FAIL)} FAIL за {dt:.1f}s")
    if FAIL:
        print("Провалены:", ", ".join(FAIL))
        sys.exit(1)


if __name__ == "__main__":
    main()

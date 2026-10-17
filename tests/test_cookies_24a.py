"""Тесты Task 24-a — куки веб-чатов DeepSeek/Qwen через расширение.

Запуск:  python tests/test_cookies_24a.py          (автономно)
         python -m pytest tests/test_cookies_24a.py -q

Проверяют:
  * src/env_file.py — чтение/точечное обновление .env с сохранением
    комментариев; запрет пустых значений; вливание в os.environ;
  * src/web_cookies.py — фильтр кук (чужие домены/шум/дубли/переполнение),
    env-маппинг DeepSeek/Qwen, разворот userToken-JSON, требование
    авторизационного маркера, сохранение (env + статус без значений),
    статус, wipe;
  * features/bridge/api.py — новые эндпоинты сквозь TestClient
    (POST 422 без маркеров, POST 200 с куками, GET status, DELETE);
  * расширение/манифесты — permission «cookies», версия 1.2.0,
    маркеры auth-логики в bridge-core.js и popup.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


# ═══════════════════════════════════════════════════════════════════════
# Подготовка изолированного окружения (PROJECT_ROOT → tmp)
# ═══════════════════════════════════════════════════════════════════════
def make_project(tmp: Path) -> Path:
    root = tmp / "proj"
    (root / "data" / "bridge").mkdir(parents=True)
    (root / ".env.example").write_text(
        "# Шаблон\n\nDEEPSEEK_DS_SESSION_ID=\nDEEPSEEK_SMIDV2=\n"
        "DEEPSEEK_THUMBCACHE=\nDEEPSEEK_AUTH_TOKEN=\nQWEN_WEB_COOKIES=\n"
        "QWEN_WEB_TOKEN=\nOTHER_KEY=keep\n", encoding="utf-8")
    (root / ".env").write_text(
        "# Конфиг пользователя\nDEEPSEEK_SMIDV2=old-value\n"
        "OTHER_KEY=keep\n", encoding="utf-8")
    return root


def use_root(root: Path, monkey) -> None:
    """PROJECT_ROOT в окружение (monkey — pytest.monkeypatch или мок)."""
    monkey.setenv("PROJECT_ROOT", str(root))


class _Monkey:
    """Минималистичная замена pytest.monkeypatch для автономного запуска."""

    def __init__(self):
        self._saved: dict[str, str | None] = {}

    def setenv(self, key: str, val: str) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key)
        os.environ[key] = val

    def undo(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._saved.clear()


MONKEY = _Monkey()

# ═══════════════════════════════════════════════════════════════════════
# 1. env_file
# ═══════════════════════════════════════════════════════════════════════
def test_env_file(tmp: Path) -> None:
    print("── env_file ──")
    from src import env_file

    root = make_project(tmp)
    use_root(root, MONKEY)
    check("project_root из PROJECT_ROOT",
          env_file.project_root() == root)

    env_path = root / ".env"
    rep = env_file.write_env_updates({
        "DEEPSEEK_SMIDV2": "new-smid",
        "NEW_KEY": "added",
        "EMPTY": "  ",
    }, template=root / ".env.example")
    text = env_path.read_text(encoding="utf-8")
    check("существующий ключ обновлён", "DEEPSEEK_SMIDV2=new-smid" in text)
    # семантика first_run.write_env: структура берётся из шаблона .env.example,
    # его комментарии сохраняются; комментарии старого .env — нет
    check("комментарии шаблона сохранены", "# Шаблон" in text)
    check("чужой ключ не тронут", "OTHER_KEY=keep" in text)
    check("новый ключ дописан", "NEW_KEY=added" in text)
    check("пустые значения пропущены", "EMPTY" not in text)
    check("отчёт: updated/added",
          rep["updated"] == ["DEEPSEEK_SMIDV2"] and
          rep["added"] == ["NEW_KEY"], json.dumps(rep, ensure_ascii=False))

    env_file.apply_to_environ({"DEEPSEEK_SMIDV2": "env-smid"})
    check("apply_to_environ", os.environ.get("DEEPSEEK_SMIDV2") == "env-smid")

    vals = env_file.read_env(env_path)
    check("read_env без кавычек и комментариев",
          vals.get("DEEPSEEK_SMIDV2") == "new-smid" and
          vals.get("OTHER_KEY") == "keep")


# ═══════════════════════════════════════════════════════════════════════
# 2. web_cookies
# ═══════════════════════════════════════════════════════════════════════
def test_web_cookies(tmp: Path) -> None:
    print("── web_cookies: фильтр и маппинг ──")
    from src import web_cookies as wc

    root = make_project(tmp)
    use_root(root, MONKEY)
    for k in ("DEEPSEEK_DS_SESSION_ID", "DEEPSEEK_SMIDV2",
              "DEEPSEEK_THUMBCACHE", "DEEPSEEK_AUTH_TOKEN",
              "QWEN_WEB_COOKIES", "QWEN_WEB_TOKEN"):
        os.environ.pop(k, None)

    # фильтр: чужой домен, шум, дубль, переполнение
    keep, rejected = wc.filter_cookies("deepseek", [
        {"name": "ds_session_id", "value": "sess-1", "domain": "chat.deepseek.com"},
        {"name": "smidV2", "value": "smid-1", "domain": ".deepseek.com"},
        {"name": ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae",
         "value": "thumb-1", "domain": "chat.deepseek.com"},
        {"name": "_ga", "value": "GA1.2.x", "domain": "deepseek.com"},
        {"name": "intercom-abc", "value": "noise", "domain": "deepseek.com"},
        {"name": "someone", "value": "x", "domain": "evil.com"},
        {"name": "ds_session_id", "value": "dup", "domain": "deepseek.com"},
        {"name": "big", "value": "x" * 9999, "domain": "deepseek.com"},
    ])
    names = [c["name"] for c in keep]
    check("приняты куки провайдера",
          names == ["ds_session_id", "smidV2",
                    ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae"], str(names))
    check("шум и чужие отклонены",
          set(rejected) >= {"_ga", "intercom-abc", "someone", "big"},
          str(rejected))
    check("дубль отброшен (первое вхождение)",
          keep[0]["value"] == "sess-1")

    # env-маппинг + userToken JSON
    updates = wc.env_mapping("deepseek", keep, {
        "userToken": json.dumps({"token": "bearer-abc"})})
    check("ds_session_id → DEEPSEEK_DS_SESSION_ID",
          updates.get("DEEPSEEK_DS_SESSION_ID") == "sess-1")
    check("smidV2 → DEEPSEEK_SMIDV2", updates.get("DEEPSEEK_SMIDV2") == "smid-1")
    check("thumbcache → DEEPSEEK_THUMBCACHE",
          updates.get("DEEPSEEK_THUMBCACHE") == "thumb-1")
    check("userToken-JSON развёрнут в DEEPSEEK_AUTH_TOKEN",
          updates.get("DEEPSEEK_AUTH_TOKEN") == "bearer-abc")

    updates2 = wc.env_mapping("deepseek", keep, {"userToken": "plain-token"})
    check("userToken без JSON — как есть",
          updates2.get("DEEPSEEK_AUTH_TOKEN") == "plain-token")

    qwen = wc.env_mapping("qwen", [
        {"name": "token", "value": "qtok", "domain": "chat.qwen.ai"},
        {"name": "cna", "value": "cna-1", "domain": ".qwen.ai"},
    ], {})
    check("qwen: полная Cookie-строка",
          qwen.get("QWEN_WEB_COOKIES") == "token=qtok; cna=cna-1")
    check("qwen: token → QWEN_WEB_TOKEN",
          qwen.get("QWEN_WEB_TOKEN") == "qtok")

    # required_missing
    check("deepseek без маркеров — ошибка",
          wc.required_missing("deepseek", [{"name": "cna", "value": "x"}], {}))
    check("deepseek с ds_session_id — ok",
          not wc.required_missing("deepseek", [
              {"name": "ds_session_id", "value": "x"}], {}))

    # сохранение
    res = wc.save_provider_cookies("deepseek", "https://chat.deepseek.com/",
                                   keep, {"userToken": "bearer-abc"})
    check("save: ok + saved_env", res["ok"] and
          set(res["saved_env"]) == {"DEEPSEEK_DS_SESSION_ID",
                                    "DEEPSEEK_SMIDV2",
                                    "DEEPSEEK_THUMBCACHE",
                                    "DEEPSEEK_AUTH_TOKEN"},
          str(res))
    env = (root / ".env").read_text(encoding="utf-8")
    check("значения в .env", "DEEPSEEK_DS_SESSION_ID=sess-1" in env)
    check("os.environ обновлён", os.environ.get("DEEPSEEK_SMIDV2") == "smid-1")

    status = wc.cookies_status()
    ds = status["providers"]["deepseek"]
    check("статус: configured", ds["configured"] is True)
    check("статус не содержит значений",
          "sess-1" not in json.dumps(status) and
          "smid-1" not in json.dumps(status))
    check("статус: env_present",
          "DEEPSEEK_DS_SESSION_ID" in ds["env_present"])

    # сохранение qwen
    wc.save_provider_cookies("qwen", "https://chat.qwen.ai/", [
        {"name": "token", "value": "qtok", "domain": "chat.qwen.ai"}], {})
    check("qwen сохранён",
          os.environ.get("QWEN_WEB_TOKEN") == "qtok")

    # ошибка: нет маркеров
    try:
        wc.save_provider_cookies("deepseek", "https://x", [
            {"name": "cna", "value": "1", "domain": "deepseek.com"}], {})
        ok = False
    except ValueError:
        ok = True
    check("save без авторизационных — ValueError", ok)

    # неизвестный провайдер
    try:
        wc.save_provider_cookies("openai", "https://x", [], {})
        ok = False
    except ValueError:
        ok = True
    check("неизвестный провайдер — ValueError", ok)

    # wipe
    wc.wipe_provider("deepseek")
    check("wipe: os.environ чист",
          not os.environ.get("DEEPSEEK_DS_SESSION_ID"))
    env_after = (root / ".env").read_text(encoding="utf-8")
    check("wipe: в .env пусто",
          "DEEPSEEK_DS_SESSION_ID=" in env_after and
          "sess-1" not in env_after)
    st2 = wc.cookies_status()
    check("wipe: статус сброшен",
          st2["providers"]["deepseek"]["configured"] is False)


# ═══════════════════════════════════════════════════════════════════════
# 3. API эндпоинты
# ═══════════════════════════════════════════════════════════════════════
def test_api(tmp: Path) -> None:
    print("── API /api/bridge/cookies ──")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    root = make_project(tmp)
    use_root(root, MONKEY)
    for k in ("DEEPSEEK_DS_SESSION_ID", "DEEPSEEK_SMIDV2",
              "QWEN_WEB_COOKIES", "QWEN_WEB_TOKEN"):
        os.environ.pop(k, None)

    from features.bridge.api import create_router
    app = FastAPI()
    app.include_router(create_router())
    client = TestClient(app)

    r = client.post("/api/bridge/cookies", json={
        "provider": "deepseek", "url": "https://chat.deepseek.com/",
        "cookies": [], "storage": {}})
    check("POST без кук → 422", r.status_code == 422, str(r.status_code))

    r = client.post("/api/bridge/cookies", json={
        "provider": "openai", "cookies": [{"name": "a", "value": "b"}]})
    check("POST неизвестный провайдер → 422", r.status_code == 422)

    r = client.post("/api/bridge/cookies", json={
        "provider": "deepseek", "url": "https://chat.deepseek.com/",
        "cookies": [
            {"name": "ds_session_id", "value": "sess-9",
             "domain": "chat.deepseek.com"},
            {"name": "smidV2", "value": "smid-9",
             "domain": ".deepseek.com"},
        ],
        "storage": {"userToken": "tok-9"}})
    check("POST с куками → 200", r.status_code == 200, r.text[:200])
    body = r.json()
    check("POST: saved_env содержит DEEPSEEK_*",
          "DEEPSEEK_DS_SESSION_ID" in body.get("saved_env", []))
    check("POST: значения в os.environ",
          os.environ.get("DEEPSEEK_DS_SESSION_ID") == "sess-9")

    r = client.get("/api/bridge/cookies/status")
    check("GET status → 200", r.status_code == 200)
    providers = r.json().get("providers", {})
    check("GET status: deepseek configured",
          providers.get("deepseek", {}).get("configured") is True)
    check("GET status: qwen не настроен",
          providers.get("qwen", {}).get("configured") is False)
    check("GET status: нет значений",
          "sess-9" not in r.text and "tok-9" not in r.text)

    r = client.delete("/api/bridge/cookies/deepseek")
    check("DELETE → 200", r.status_code == 200)
    check("DELETE: окружение чисто",
          not os.environ.get("DEEPSEEK_DS_SESSION_ID"))
    r = client.delete("/api/bridge/cookies/unknown")
    check("DELETE неизвестный → 404", r.status_code == 404)

    # регресс: прежние эндпоинты живы
    r = client.get("/api/bridge/status")
    check("GET /status (регресс) → 200", r.status_code == 200)


# ═══════════════════════════════════════════════════════════════════════
# 4. Расширение: манифесты и маркеры
# ═══════════════════════════════════════════════════════════════════════
def test_extension() -> None:
    print("── extension: манифесты и auth-логика ──")
    ext = BASE / "extension"

    for target in ("chromium", "firefox"):
        m = json.loads((ext / target / "manifest.json").read_text("utf-8"))
        check(f"{target}: версия 1.2.0", m.get("version") == "1.2.0")
        check(f"{target}: permission cookies",
              "cookies" in m.get("permissions", []))

    core = (ext / "shared" / "bridge-core.js").read_text("utf-8")
    for marker in ("WEB_AUTH", "authOpen", "authCollect",
                   "authCheckPending", "authLoginMarkerPresent",
                   "cookies.getAll", "localStorage.getItem",
                   "/api/bridge/cookies", "bridge-auth"):
        check(f"bridge-core: {marker}", marker in core)
    check("bridge-core: кроссбраузерность apiX сохранена",
          core.count("const apiX =") == 1)

    popup_html = (ext / "popup" / "popup.html").read_text("utf-8")
    check("popup.html: кнопки DeepSeek/Qwen",
          "auth-deepseek" in popup_html and "auth-qwen" in popup_html)
    popup_js = (ext / "popup" / "popup.js").read_text("utf-8")
    for marker in ("auth_open", "auth_collect", "auth_status",
                   "cookies/status"):
        check(f"popup.js: {marker}", marker in popup_js)

    ui_js = (BASE / "features" / "bridge" / "ui.js").read_text("utf-8")
    check("web-UI: блок кук", "bridge-cookies" in ui_js and
          "bridge.cookies" in ui_js)

    # собранный dist актуален
    for target in ("chromium", "firefox"):
        dm = json.loads((ext / "dist" / f"bridge-{target}" / "manifest.json")
                        .read_text("utf-8"))
        check(f"dist/{target}: версия 1.2.0", dm.get("version") == "1.2.0")
        dcore = (ext / "dist" / f"bridge-{target}" / "shared" /
                 "bridge-core.js").read_text("utf-8")
        check(f"dist/{target}: auth-логика собрана", "WEB_AUTH" in dcore)


# ═══════════════════════════════════════════════════════════════════════
def main() -> int:
    print("\n══ Тесты Task 24-a: куки веб-чатов через расширение ══\n")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_env_file(tmp / "t1")
        test_web_cookies(tmp / "t2")
        test_api(tmp / "t3")
    test_extension()
    MONKEY.undo()
    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        for name in FAIL:
            print(f"  ✗ {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

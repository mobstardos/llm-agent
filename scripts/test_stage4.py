#!/usr/bin/env python3
"""Тесты Этапа 4: событийная шина + Feature-система (V2 §2.2–2.4).

Запуск: python scripts/test_stage4.py
- src/events.py: publish/subscribe/wildcard/изоляция падений/буфер;
- src/core/features.py: сканирование манифестов, монтаж API-роутера,
  вкладки UI, requires, миграции (один раз), изоляция ошибок;
- фича notes: сквозной сценарий через fastapi TestClient;
- скелетонеры scripts/new_agent.py, scripts/new_feature.py.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import events as bus  # noqa: E402
from src.core.features import FeatureLoader, FeatureManifest  # noqa: E402

PASS, FAIL = "✓", "✗"
ok_count = fail_count = 0


def check(name: str, cond: bool) -> None:
    global ok_count, fail_count
    print(f"  {PASS if cond else FAIL} {name}")
    ok_count += bool(cond)
    fail_count += (not cond)


# ═══════════════════════════════════════════════════════════
# Шина событий
# ═══════════════════════════════════════════════════════════
async def test_events_bus():
    print("== events: publish/subscribe ==")
    bus.clear()
    got: list[dict] = []
    got_all: list[dict] = []

    async def h1(e):
        got.append(e)

    async def hall(e):
        got_all.append(e)

    def h_bad(e):
        raise RuntimeError("подписчик сломан специально")

    bus.subscribe("plan.created", h1)
    bus.subscribe("plan.created", h_bad)      # упадёт — не помешает h1
    bus.subscribe("*", hall)
    await bus.publish("plan.created", {"id": "p1"})
    await bus.publish("other.kind", {"x": 1})
    check("точная подписка получила только своё",
          len(got) == 1 and got[0]["payload"] == {"id": "p1"})
    check("wildcard получил всё", len(got_all) == 2)
    check("падение обработчика изолировано (h1 сработал)", len(got) == 1)
    check("событие-конверт: kind/payload/ts",
          got[0]["kind"] == "plan.created" and got[0]["ts"] > 0)
    rec = bus.recent(10)
    check("кольцевой буфер хранит события",
          len(rec) == 2 and rec[-1]["kind"] == "other.kind")

    bus.unsubscribe("plan.created", h1)
    await bus.publish("plan.created", {"id": "p2"})
    check("unsubscribe работает", len(got) == 1 and len(got_all) == 3)
    bus.clear()

    # publish_soon из синхронного кода внутри цикла
    async def hsoon(e):
        got.append(e)
    bus.subscribe("soon.kind", hsoon)
    bus.publish_soon("soon.kind", {"z": 3})
    await asyncio.sleep(0.02)
    check("publish_soon доставляет асинхронно",
          any(e["payload"] == {"z": 3} for e in got))
    bus.clear()


# ═══════════════════════════════════════════════════════════
# FeatureLoader: сканирование и монтаж
# ═══════════════════════════════════════════════════════════
def test_scan_project_features():
    print("== features: манифесты проекта (journal, notes) ==")
    loader = FeatureLoader(base_dir=ROOT)
    found = loader.scan()
    ids = [m.id for m, _ in found]
    check("найдены journal и notes", "journal" in ids and "notes" in ids)
    check("ошибок манифестов нет",
          not [r for r in loader.load_report if not r.get("ok")])
    notes = next(m for m, _ in found if m.id == "notes")
    check("notes: api_router + ui_tab + ws_events",
          notes.api_router == "api.py:create_router"
          and notes.ui_tab is not None and notes.ui_tab.file == "ui.js"
          and "notes.changed" in notes.ws_events)
    journal = next(m for m, _ in found if m.id == "journal")
    # Этап 5 выполнен: API журнала перенесён в фичу (было api_router=None)
    check("journal: ui_tab + api_router из реестра (Этап 5)",
          journal.api_router == "api.py:create_router"
          and journal.ui_tab is not None)


def make_app_and_loader(features_dir: Path, data_dir: Path):
    app = FastAPI()
    loader = FeatureLoader(base_dir=features_dir.parent, data_dir=data_dir)
    loader.features_dir = features_dir   # тестовая директория фич
    return app, loader


def test_mount_and_isolation():
    print("== features: монтаж в FastAPI + изоляция ошибок ==")
    tmp = Path(tempfile.mkdtemp(prefix="features_test_"))
    (tmp / "features" / "good").mkdir(parents=True)
    (tmp / "features" / "broken").mkdir(parents=True)
    (tmp / "features" / "hardmiss").mkdir(parents=True)
    (tmp / "features" / "mig").mkdir(parents=True)
    data_dir = tmp / "data"

    # валидная фича с api_router (файл рядом с манифестом)
    (tmp / "features" / "good" / "api.py").write_text(
        "from fastapi import APIRouter\n"
        "def create_router():\n"
        "    r = APIRouter(prefix='/api/good')\n"
        "    @r.get('')\n"
        "    async def idx(): return {'ok': True}\n"
        "    return r\n", encoding="utf-8")
    (tmp / "features" / "good" / "feature.yaml").write_text(
        "id: good\nversion: '1.0'\ntitle: Good\n"
        "api_router: api.py:create_router\n"
        "ui_tab: {file: ui.js, title: Good, icon: '✅'}\n"
        "ws_events: [good.event]\n", encoding="utf-8")
    (tmp / "features" / "good" / "ui.js").write_text("// ui", encoding="utf-8")

    # битый манифест — не должен уронить остальные
    (tmp / "features" / "broken" / "feature.yaml").write_text(
        "id: [это, не, схема\n", encoding="utf-8")

    # hard-требование к отсутствующему пакету → пропуск
    (tmp / "features" / "hardmiss" / "feature.yaml").write_text(
        "id: hardmiss\ntitle: Miss\n"
        "requires:\n  python_packages:\n    - {name: no_such_pkg_xyz, level: hard}\n",
        encoding="utf-8")

    # фича с миграцией, пишущей маркер
    (tmp / "features" / "mig" / "migrations").mkdir()
    (tmp / "features" / "mig" / "migrations" / "m1.py").write_text(
        "from pathlib import Path\n"
        "def run(state):\n"
        "    Path(state['marker_file']).write_text('migrated')\n",
        encoding="utf-8")
    (tmp / "features" / "mig" / "feature.yaml").write_text(
        "id: mig\ntitle: Mig\nmigrations: migrations/\n", encoding="utf-8")

    try:
        app, loader = make_app_and_loader(tmp / "features", data_dir)
        state = {"marker_file": str(tmp / "migrated.marker")}

        async def mount():
            return await loader.mount(app, state)
        report = asyncio.run(mount())

        by_id = {r["id"]: r for r in report}
        check("битый манифест изолирован (ошибка в отчёте)",
              not by_id["broken"]["ok"])
        check("good смонтирован", by_id["good"]["ok"])
        check("hardmiss пропущен по hard-требованию",
              not by_id["hardmiss"]["ok"] and "no_such_pkg_xyz"
              in by_id["hardmiss"]["error"])
        check("миграция mig отработала",
              (tmp / "migrated.marker").exists())

        client = TestClient(app)
        r = client.get("/api/good")
        check("API-роутер фичи отвечает", r.status_code == 200
              and r.json() == {"ok": True})

        cards = loader.list_for_client()
        good = next(c for c in cards if c["id"] == "good")
        check("карточка вкладки: js_url указывает на /features/<id>/<file>",
              good["js_url"] == "/features/good/ui.js"
              and good["title"] == "Good" and good["icon"] == "✅")
        check("hardmiss не в реестре клиента",
              not any(c["id"] == "hardmiss" for c in cards))

        # повторный монтаж: миграции не выполняются второй раз
        (tmp / "migrated.marker").unlink()
        state2 = {"marker_file": str(tmp / "migrated2.marker")}

        async def mount2():
            return await loader2.mount(app2, state2)
        app2, loader2 = make_app_and_loader(tmp / "features", data_dir)
        asyncio.run(mount2())
        check("миграция не повторяется (маркер applied)",
              not (tmp / "migrated2.marker").exists())
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notes_feature_e2e():
    print("== фича notes: сквозной сценарий (TestClient) ==")
    bus.clear()
    seen: list[dict] = []

    async def h(e):
        seen.append(e)
    bus.subscribe("notes.changed", h)

    notes_dir = ROOT / "features" / "notes"
    app = FastAPI()
    loader = FeatureLoader(base_dir=notes_dir.parent)
    loader.features_dir = notes_dir.parent   # корень features/ (journal + notes)

    async def mount():
        await loader.mount(app, {})
    asyncio.run(mount())

    client = TestClient(app)
    r = client.get("/api/notes")
    check("GET /api/notes отвечает", r.status_code == 200 and "notes" in r.json())

    r = client.post("/api/notes", json={"text": "тестовая заметка"})
    check("POST /api/notes создаёт", r.status_code == 201
          and r.json()["note"]["text"] == "тестовая заметка")
    note_id = r.json()["note"]["id"]
    time.sleep(0.05)
    check("событие notes.changed опубликовано",
          any(e["payload"].get("action") == "add" for e in seen))

    r = client.delete(f"/api/notes/{note_id}")
    check("DELETE /api/notes удаляет", r.status_code == 200)
    bus.clear()


def test_generators():
    print("== скелетонеры: new_agent.py / new_feature.py ==")
    py = sys.executable
    agent_dir = ROOT / "agents" / "zz_test_skel_agent"
    feat_dir = ROOT / "features" / "zz_test_skel_feat"
    try:
        r1 = subprocess.run(
            [py, str(ROOT / "scripts" / "new_agent.py"), "zz_test_skel_agent",
             "--title", "Тестовый", "--keywords", "тест,скелет"],
            capture_output=True, text=True)
        check("new_agent.py создал директорию",
              r1.returncode == 0 and agent_dir.exists())
        check("agent.yaml валиден и читается pyyaml",
              agent_dir.exists()
              and "zz_test_skel_agent" in (agent_dir / "agent.yaml")
              .read_text(encoding="utf-8"))
        r1b = subprocess.run(
            [py, str(ROOT / "scripts" / "new_agent.py"), "zz_test_skel_agent"],
            capture_output=True, text=True)
        check("повторный запуск отклонён", r1b.returncode != 0)

        r2 = subprocess.run(
            [py, str(ROOT / "scripts" / "new_feature.py"), "zz_test_skel_feat",
             "--title", "Тестовая фича", "--with-api", "--with-ui"],
            capture_output=True, text=True)
        check("new_feature.py создал директорию",
              r2.returncode == 0 and feat_dir.exists())
        # фича-заготовка должна проходить FeatureLoader
        loader = FeatureLoader(base_dir=feat_dir.parent)
        loader.features_dir = feat_dir.parent
        found = loader.scan()
        check("манифест заготовки валиден для FeatureLoader",
              any(m.id == "zz_test_skel_feat" for m, _ in found))

        r2b = subprocess.run(
            [py, str(ROOT / "scripts" / "new_feature.py"),
             "НЕВАЛИДНЫЙ.ID"], capture_output=True, text=True)
        check("некорректный id отклонён", r2b.returncode != 0)
    finally:
        shutil.rmtree(agent_dir, ignore_errors=True)
        shutil.rmtree(feat_dir, ignore_errors=True)


def test_ui_contract():
    print("== UI: контракт реестра фич и сессии ==")
    app_js = (ROOT / "src" / "web" / "app.js").read_text(encoding="utf-8")
    check("app.js: loadFeatures + /api/features",
          "loadFeatures" in app_js and "/api/features" in app_js)
    check("app.js: session_restored + hello + session_id",
          "session_restored" in app_js and "hello" in app_js
          and "session_id" in app_js)
    index_html = (ROOT / "src" / "web" / "index.html").read_text(encoding="utf-8")
    check("index.html: нет хардкода фич (реестр — единственный источник)",
          "journal.js" not in index_html and "notes" not in index_html)


# ═══════════════════════════════════════════════════════════
def main() -> int:
    asyncio.run(test_events_bus())
    test_scan_project_features()
    test_mount_and_isolation()
    test_notes_feature_e2e()
    test_generators()
    test_ui_contract()
    print(f"\nИтог: {ok_count} ок, {fail_count} падений")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(main())

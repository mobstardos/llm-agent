#!/usr/bin/env python3
"""Скелетонер подсистемы-feature (Этап 4, ARCHITECTURE-V2 §2.3).

    python scripts/new_feature.py my_feature --title "Моя фича" \
        --with-api --with-ui

Генерирует features/<id>/: feature.yaml (+ api.py, ui.js, README.md
по флагам). Роутер и вкладка монтируются автоматически при следующем
старте сервера — НОЛЬ правок main.py / index.html:

    GET /api/features  →  [{id, title, icon, js_url, ...}]
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
FEATURES_DIR = BASE_DIR / "features"

FEATURE_YAML = """id: {id}
version: "0.1.0"
title: {title}
description: >
  {description}
enabled: true

requires:
  python_packages: []
  # - {{name: aiosqlite, level: soft}}   # hard → фича не включится без пакета

{api_line}{ui_line}ws_events: []
# - {id}.event          # события, которые фича шлёт в /ws через src/events.py

# migrations: migrations/   # *.py прогоняются один раз при первом старте
# permissions: {{read: [user], write: [admin]}}
"""

API_PY = '''"""API фичи «{title}» — контракт: create_router() -> APIRouter.

FeatureLoader монтирует роутер при старте (app.include_router).
"""
from __future__ import annotations

from fastapi import APIRouter


def create_router():
    router = APIRouter(prefix="/api/{id}", tags=["feature:{id}"])

    @router.get("")
    async def index():
        return {{"feature": "{id}", "ok": True}}

    # @router.post("/items")
    # async def add_item(payload: dict): ...

    return router
'''

UI_JS = '''/* {title} — вкладка фичи (self-contained).
   Подключается через реестр: GET /api/features → js_url. */
(function () {{
  "use strict";

  function $(sel, root) {{ return (root || document).querySelector(sel); }}
  function el(tag, cls, text) {{
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }}

  function injectTab() {{
    var modal = $("#settings-modal");
    if (!modal || $("#{id}-tab-btn")) return;
    var tabs = $(".settings-tabs", modal);
    var body = $(".settings-body", modal);
    if (!tabs || !body) return;

    var btn = el("button", "tab-btn", "{icon} {title}");
    btn.id = "{id}-tab-btn";
    btn.setAttribute("data-tab", "{id}");
    tabs.appendChild(btn);

    var panel = el("div", "tab-panel");
    panel.setAttribute("data-panel", "{id}");
    panel.innerHTML =
      '<div class="panel-actions">' +
        '<button id="{id}-refresh" class="secondary">🔄 Обновить</button>' +
        '<span id="{id}-summary" class="summary">—</span>' +
      "</div>" +
      '<div id="{id}-list" class="list"></div>';
    body.appendChild(panel);

    async function load() {{
      try {{
        var r = await fetch("/api/{id}");
        if (!r.ok) throw new Error("HTTP " + r.status);
        var d = await r.json();
        $("#{id}-summary").textContent = "ok";
        $("#{id}-list").textContent = JSON.stringify(d, null, 2);
      }} catch (e) {{
        $("#{id}-summary").textContent = "ошибка: " + e.message;
      }}
    }}
    $("#{id}-refresh").onclick = load;
    btn.addEventListener("click", function once() {{
      btn.removeEventListener("click", once);
      load();
    }});
  }}

  if (document.readyState === "loading") {{
    document.addEventListener("DOMContentLoaded", injectTab);
  }} else {{
    injectTab();
  }}
}})();
'''

README_MD = """# Feature: {id}

{description}

## Структура

```
features/{id}/
├── feature.yaml   # манифест (pydantic-валидация при старте){tree}
```

## Что происходит автоматически

1. FeatureLoader монтирует API-роутер (`{api}`).
2. `GET /api/features` отдаёт карточку вкладки; `app.js` подгружает `ui.js`.
3. События через `src/events.py`: `events.publish("<id>.event", {{...}})` —
   WS-мост доставит их подключённым клиентам.
"""

ICONS = ["🧩", "🧪", "🛠️", "📦", "🗂️", "⚙️"]


def main() -> int:
    ap = argparse.ArgumentParser(description="Генератор скелета фичи")
    ap.add_argument("feature_id", help="id фичи: a-z0-9_- (напр. my_feature)")
    ap.add_argument("--title", default=None)
    ap.add_argument("--description", default=None)
    ap.add_argument("--icon", default="🧩", help="иконка вкладки (эмодзи)")
    ap.add_argument("--with-api", action="store_true",
                    help="сгенерировать api.py с create_router()")
    ap.add_argument("--with-ui", action="store_true",
                    help="сгенерировать ui.js (самодостаточная вкладка)")
    args = ap.parse_args()

    fid = args.feature_id.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", fid):
        print(f"✗ Некорректный id: '{fid}' (разрешено a-z0-9_-)", file=sys.stderr)
        return 2

    target = FEATURES_DIR / fid
    if target.exists():
        print(f"✗ Директория уже существует: {target}", file=sys.stderr)
        return 2

    title = args.title or fid.replace("_", " ").replace("-", " ").capitalize()
    description = args.description or f"Подсистема «{title}» (заготовка)"

    api_line = "api_router: api.py:create_router\n" if args.with_api else ""
    ui_line = (f"ui_tab:\n  file: ui.js\n  title: {title}\n"
               f"  icon: \"{args.icon}\"\n" if args.with_ui else "")

    target.mkdir(parents=True, exist_ok=False)
    (target / "feature.yaml").write_text(
        FEATURE_YAML.format(id=fid, title=title, description=description,
                            api_line=api_line, ui_line=ui_line),
        encoding="utf-8")
    if args.with_api:
        (target / "api.py").write_text(
            API_PY.format(id=fid, title=title), encoding="utf-8")
    if args.with_ui:
        (target / "ui.js").write_text(
            UI_JS.format(id=fid, title=title, icon=args.icon), encoding="utf-8")
    (target / "README.md").write_text(
        README_MD.format(id=fid, title=title, description=description,
                         api="да" if args.with_api else "нет",
                         tree="\n├── api.py         # create_router() -> APIRouter"
                              "\n├── ui.js          # вкладка в настройках"
                              if args.with_api and args.with_ui else ""),
        encoding="utf-8")

    print(f"✓ Фича создана: {target}")
    if args.with_api:
        print("  • API появится на /api/" + fid + " после рестарта сервера")
    if args.with_ui:
        print("  • Вкладка появится в настройках (GET /api/features → js_url)")
    print("  • События: from src import events; await events.publish("
          f"\"{fid}.event\", {{...}})")
    print("  • Проверка: перезапустите сервер и откройте GET /api/features")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

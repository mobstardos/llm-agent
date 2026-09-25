#!/usr/bin/env python3
"""Сборка расширения «LLM Agent Bridge»: раздельные пакеты Chromium/Firefox.

Из исходников extension/ собираются две раскладки + zip:
    extension/dist/bridge-chromium/  → llm-agent-bridge-chromium-<ver>.zip
    extension/dist/bridge-firefox/   → llm-agent-bridge-firefox-<ver>.zip

Ассерты (сборка падает сразу при нарушении):
  * манифесты — валидный JSON с ожидаемыми ключами браузера
    (chromium: service_worker + side_panel; firefox: background.scripts +
    sidebar_action + gecko id), в обоих — commands.wake-agent;
  * каждый путь, упомянутый в манифесте, существует в собранной раскладке;
  * во всех пользовательских JS нет сырых chrome./browser. вне строки
    `const apiX = ...` (кроссбраузерность — только через apiX);
  * chromium/background.js делает importScripts("shared/bridge-core.js");
  * маркеры фич в исходниках: горячая клавиша wake-agent + pendingWake,
    контекст-действие «суммаризировать страницу», подсказка в popup;
  * `node --check` для каждого .js (если node установлен).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXT = ROOT / "extension"
DIST = EXT / "dist"
SHARED = ["shared/bridge-core.js", "popup/popup.html", "popup/popup.js",
          "panel/panel.html", "panel/panel.css", "panel/panel.js"]
ICONS = ["icons/icon16.png", "icons/icon32.png",
         "icons/icon48.png", "icons/icon128.png"]


def fail(msg: str) -> None:
    print(f"[ОШИБКА] {msg}")
    sys.exit(1)


def js_lint(path: Path) -> None:
    """node --check, если node есть; иначе только наличие apiX-маркера."""
    src = path.read_text(encoding="utf-8")
    raw = re.search(r"\b(chrome|browser)\.", src)
    if raw:
        # допустимая единственная точка входа — объявление apiX
        allowed = re.search(
            r"const\s+apiX\s*=\s*\(typeof\s+browser\s*!==\s*"
            r"\"undefined\"\)\s*\?\s*browser\s*:\s*chrome", src)
        uses = [m for m in re.finditer(
            r"(?<![\"\w])(?:chrome|browser)\.\w", src)]
        if not allowed or len(uses) > 1:
            fail(f"{path}: сырой {'chrome' if raw.group(1)=='chrome' else 'browser'}."
                 "* вне apiX — ломает кроссбраузерность")
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(path)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            fail(f"{path}: синтаксис JS: {r.stderr.strip()[:300]}")


def build(target: str) -> tuple[Path, str]:
    src_dir = EXT / target
    manifest_path = src_dir / "manifest.json"
    if not manifest_path.exists():
        fail(f"{manifest_path} не найден")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as e:
        fail(f"{manifest_path}: битый JSON: {e}")

    version = str(manifest.get("version", "0.0.0"))
    out = DIST / f"bridge-{target}"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # 1) манифест
    (out / "manifest.json").write_bytes(manifest_path.read_bytes())

    # 2) общие ресурсы + фон браузера
    for rel in SHARED + ICONS:
        src = EXT / rel
        if not src.exists():
            fail(f"нет файла {src}")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_file():
            shutil.copy2(src, dst)
    for name in ("background.js",):
        src = src_dir / name
        if not src.exists():
            fail(f"нет файла {src}")
        shutil.copy2(src, out / name)
        js_lint(out / name)
    for rel in ("popup/popup.js", "panel/panel.js", "shared/bridge-core.js"):
        js_lint(out / rel)

    # 3) все пути из манифеста существуют в раскладке
    def walk(node, acc):
        if isinstance(node, dict):
            for v in node.values():
                walk(v, acc)
        elif isinstance(node, list):
            for v in node:
                walk(v, acc)
        elif isinstance(node, str) and re.search(
                r"\.(html|js|css|png)$", node):
            acc.append(node)
    refs: list[str] = []
    walk(manifest, refs)
    for rel in refs:
        if not (out / rel).exists():
            fail(f"манифест ссылается на отсутствующий файл: {rel}")

    # 4) браузерные различия
    if target == "chromium":
        bg = manifest.get("background", {})
        if bg.get("service_worker") != "background.js":
            fail("chromium: background.service_worker != background.js")
        if "side_panel" not in manifest:
            fail("chromium: нет side_panel")
        if "sidePanel" not in manifest.get("permissions", []):
            fail("chromium: нет разрешения sidePanel")
        bgjs = (out / "background.js").read_text(encoding="utf-8")
        if 'importScripts("shared/bridge-core.js")' not in bgjs:
            fail("chromium/background.js не подключает bridge-core.js")
    else:
        scripts = manifest.get("background", {}).get("scripts", [])
        if "shared/bridge-core.js" not in scripts:
            fail("firefox: background.scripts без shared/bridge-core.js")
        if "sidebar_action" not in manifest:
            fail("firefox: нет sidebar_action")
        gecko = (manifest.get("browser_specific_settings", {})
                 .get("gecko", {}).get("id"))
        if not gecko:
            fail("firefox: нет browser_specific_settings.gecko.id")
    if manifest.get("omnibox", {}).get("keyword") != "ag":
        fail("omnibox keyword != ag")
    wake = (manifest.get("commands", {}).get("wake-agent") or {}) \
        .get("suggested_key", {}).get("default")
    if not wake:
        fail(f"{target}: commands.wake-agent.suggested_key отсутствует")
    if manifest.get("manifest_version") != 3:
        fail("manifest_version != 3")

    # 5) zip
    zip_path = DIST / f"llm-agent-bridge-{target}-{version}.zip"
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(out).as_posix())
    with zipfile.ZipFile(zip_path) as z:
        bad = z.testzip()
        if bad:
            fail(f"zip повреждён: {bad}")
    print(f"[OK] {target}: {len(refs)} ссылок манифеста, "
          f"{sum(1 for _ in out.rglob('*') if _.is_file())} файлов, "
          f"{zip_path.name}")
    return out, version


def assert_features() -> None:
    """Маркеры новых фич в исходниках — до сборки, один раз."""
    core = (EXT / "shared" / "bridge-core.js").read_text(encoding="utf-8")
    for marker in ("MENU_SUM_PAGE", "Суммаризировать страницу",
                   "wake-agent", "bridge_wake", "pendingWake",
                   "WEB_AUTH", "/api/bridge/cookies", "authFlow",
                   "bridge-auth"):
        if marker not in core:
            fail(f"shared/bridge-core.js: нет маркера {marker!r}")
    panel = (EXT / "panel" / "panel.js").read_text(encoding="utf-8")
    for marker in ("function wake(", "bridge_wake", "pendingWake"):
        if marker not in panel:
            fail(f"panel/panel.js: нет маркера {marker!r}")
    popup = (EXT / "popup" / "popup.html").read_text(encoding="utf-8")
    if "Alt+Shift+B" not in popup:
        fail("popup/popup.html: нет подсказки горячей клавиши")
    # Task 24-a: авторизация веб-чатов (куки) в popup и манифестах
    popup_js = (EXT / "popup" / "popup.js").read_text(encoding="utf-8")
    for marker in ("auth_open", "auth_collect", "auth_status",
                   "auth-deepseek", "auth-qwen", "authFlow"):
        if marker not in popup_js:
            fail(f"popup/popup.js: нет маркера {marker!r}")
    for target in ("chromium", "firefox"):
        m = json.loads((EXT / target / "manifest.json")
                       .read_text(encoding="utf-8"))
        if "cookies" not in m.get("permissions", []):
            fail(f"{target}: manifest.json без permission «cookies»")
        if m.get("version") != "1.2.0":
            fail(f"{target}: manifest version != 1.2.0")


def main() -> None:
    if not EXT.is_dir():
        fail(f"{EXT} не найден")
    DIST.mkdir(parents=True, exist_ok=True)
    assert_features()
    for t in ("chromium", "firefox"):
        build(t)
    print("[OK] сборка расширения завершена:", DIST)


if __name__ == "__main__":
    main()

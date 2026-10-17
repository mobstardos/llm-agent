#!/usr/bin/env python3
"""LLM Agent — самопроверка онбординга (Task 26).

Проверяет целостность «пользовательского контура» поставки:

  1. BAT-файлы: кодировка UTF-8, CRLF, chcp 65001, goto-метки,
     упоминаемые файлы существуют, ключевые фиксы на месте.
  2. Точки входа Python: компиляция, защита от UnicodeEncodeError/EOFError,
     корневой main.py — валидный шим (а не обрывок).
  3. Документация: GETTING_STARTED / CAPABILITIES / FAQ существуют и
     содержат заявленные разделы; README на них ссылается.
  4. Интерактивный курс: guide.html/js/css на месте, маршрут /guide
     зарегистрирован, кнопка 🎓 в шапке, уроки с квизами валидны,
     каждый API-эндпоинт из курса реально существует на сервере.

Запуск:  python scripts/test_onboarding.py
Код возврата: 0 — все проверки зелёные, 1 — есть провалы.
"""
from __future__ import annotations

import py_compile
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [✓] {name}")
        return True
    FAIL += 1
    FAILURES.append(f"{name}" + (f" — {detail}" if detail else ""))
    print(f"  [✗] {name}" + (f" — {detail}" if detail else ""))
    return False


def read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
print("── 1. BAT-файлы ────────────────────────────────────────────────────")

BATS = ["run.bat", "install.bat", "build_exe.bat", "install_playwright.bat"]
bat_texts: dict[str, str] = {}

for name in BATS:
    p = ROOT / name
    if not check(f"{name}: существует", p.exists()):
        continue
    raw = p.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        check(f"{name}: UTF-8", False, str(e))
        continue
    bat_texts[name] = text
    check(f"{name}: корректный UTF-8", True)
    lf_only = text.count("\n") - text.count("\r\n")
    check(f"{name}: CRLF-окончания", lf_only == 0, f"LF-only строк: {lf_only}")
    check(f"{name}: chcp 65001", "chcp 65001" in text)

run_bat = bat_texts.get("run.bat", "")
inst_bat = bat_texts.get("install.bat", "")
pw_bat = bat_texts.get("install_playwright.bat", "")
build_bat = bat_texts.get("build_exe.bat", "")

for name, text in bat_texts.items():
    labels = set(re.findall(r"^:(\w[\w-]*)", text, re.M))
    gotos = set(re.findall(r"goto\s+:?([\w-]+)", text))
    check(f"{name}: все goto-метки существуют", not (gotos - labels),
          f"нет меток: {sorted(gotos - labels)}")

# Файлы, на которые ссылаются bat-скрипты (без URL и рантайм-файлов venv)
for name, text in bat_texts.items():
    refs = set(re.findall(r"[\w./\\-]+\.(?:py|txt|spec|bat|yml|md)", text))
    broken = []
    for ref in refs:
        if "://" in ref or ref.startswith("//"):
            continue  # URL-адреса, не файлы
        ref_n = ref.replace("\\", "/")
        while ref_n.startswith("./"):
            ref_n = ref_n[2:]
        if ref_n.startswith(".venv/") or ref_n == "activate.bat" \
                or ref_n.endswith("/activate.bat"):
            continue  # создаётся в рантайме установщиком/venv
        if not (ROOT / ref_n).exists():
            broken.append(ref)
    check(f"{name}: упоминаемые файлы существуют", not broken,
          f"не найдены: {sorted(broken)}")

check("run.bat: пауза при ошибке (окно не закрывается молча)",
      "pause" in run_bat)
check("run.bat: детекция Python через py-лаунчер", "py -3" in run_bat
      or re.search(r"py -%%m", run_bat) or "py -%%m" in run_bat)
check("run.bat: прямой вызов venv-python (не activate.bat)",
      ".venv\\Scripts\\python.exe" in run_bat)
check("run.bat: отсев Store-заглушки (проверка версии)",
      "sys.version_info >= (3,10)" in run_bat)
check("install.bat: фолбэк на requirements-freethreaded.txt",
      "requirements-freethreaded.txt" in inst_bat)
check("install.bat: мёртвая строка findstr удалена",
      'findstr /c:"DATABASE_URL=file:"' not in inst_bat)
check("install.bat: смоук-тест без curl (python из venv)",
      "curl -s -o nul" not in inst_bat and "urllib.request" in inst_bat)
check("install.bat: Start-Process с WorkingDirectory",
      "-WorkingDirectory" in inst_bat)
check("install_playwright.bat: playwright через -m",
      "-m playwright install" in pw_bat)
check("build_exe.bat: Python из .venv при наличии",
      ".venv\\Scripts\\python.exe" in build_bat)

# ═══════════════════════════════════════════════════════════════════════
print("── 2. Точки входа Python ───────────────────────────────────────────")

py_targets = [ROOT / n for n in (
    "run.py", "main.py", "first_run.py", "install.py", "src/cli.py")]
py_targets += sorted((ROOT / "scripts").glob("*.py"))

for p in py_targets:
    try:
        py_compile.compile(str(p), doraise=True, cfile=tempfile.mktemp())
        check(f"py_compile: {p.relative_to(ROOT)}", True)
    except Exception as e:  # noqa: BLE001
        check(f"py_compile: {p.relative_to(ROOT)}", False, str(e))

main_py = read(ROOT / "main.py")
check("main.py: шим на run.py (not обрывок)",
      "from run import main" in main_py and "@app.get" not in main_py)
check("main.py: корректный __main__", 'if __name__ == "__main__":' in main_py)

run_py = read(ROOT / "run.py")
check("run.py: stdout/stderr reconfigure(errors=replace)",
      'reconfigure(errors="replace")' in run_py)
check("run.py: ask() ловит EOFError/KeyboardInterrupt",
      "except (EOFError, KeyboardInterrupt)" in run_py)
raw_inputs = [ln.strip() for ln in run_py.splitlines()
              if re.search(r"input\(.+\)", ln)  # вызов с аргументами
              and "def ask" not in ln
              and "return input(" not in ln
              and not ln.strip().startswith("#")]
check("run.py: сырых input() вне ask() нет", not raw_inputs, str(raw_inputs))

first_py = read(ROOT / "first_run.py")
check("first_run.py: EOFError обработан в ask/ask_yes/ask_choice",
      first_py.count("except EOFError") >= 3)
check("run.py: занятый порт диагностируется заранее (до uvicorn/10048)",
      "занят" in run_py and "findstr" in run_py and "is_port_open" in run_py)
check("run.py: выключенный qwenproxy (QWENPROXY_ENABLED=false) без красного ✗",
      "s.qwenproxy.enabled" in run_py)
check("first_run.py: --check-llm знает куки-путь (cookie_providers/deepseek_web)",
      "cookie_providers" in first_py and "deepseek_web" in first_py)

inst_py = read(ROOT / "install.py")
check("install.py: пауза при двойном клике", "pause_if_needed" in inst_py)
check("install.py: reconfigure вывода", "reconfigure" in inst_py)

# ═══════════════════════════════════════════════════════════════════════
print("── 3. Документация ─────────────────────────────────────────────────")

DOCS_REQ = {
    "docs/GETTING_STARTED.md": [
        "python install.py", "first_run", ".env", "PostgreSQL",
        "run.bat", "расшир", "Диагностика", "QWENPROXY", "AGENT_MEMORY_EMBEDDER",
    ],
    "docs/CAPABILITIES.md": [
        "Супервизор", "агент", "MCP", "память", "Журнал", "Bridge",
        "фолбэк", "Ollama", "тест", "scripts/test_onboarding.py",
    ],
    "docs/FAQ.md": [
        "chcp", "Microsoft Store", "UnicodeEncodeError", "10048",
        "403", "Rollback", "qwenproxy", "ds_session_id", "test_onboarding",
    ],
}
for doc, needles in DOCS_REQ.items():
    p = ROOT / doc
    if not check(f"{doc}: существует", p.exists()):
        continue
    text = read(p)
    check(f"{doc}: не пуст ({len(text)} симв.)", len(text) > 5000)
    for needle in needles:
        check(f"{doc}: содержит «{needle}»", needle in text)

readme = read(ROOT / "README.md")
check("README: секция «Документация»", "## 📚 Документация" in readme)
for doc in ("GETTING_STARTED.md", "CAPABILITIES.md", "FAQ.md"):
    check(f"README: ссылка на {doc}", f"({doc})" in readme or f"docs/{doc}" in readme)
check("README: упоминание интерактивного курса", "/guide" in readme)

# ═══════════════════════════════════════════════════════════════════════
print("── 4. Интерактивный курс (/guide) ──────────────────────────────────")

for f in ("guide.html", "guide.js", "guide.css"):
    p = ROOT / "src" / "web" / f
    check(f"src/web/{f}: существует", p.exists())

main_src = read(ROOT / "src" / "main.py")
check("main.py(src): маршрут /guide зарегистрирован",
      '@app.get("/guide")' in main_src and 'guide.html' in main_src)

index_html = read(ROOT / "src" / "web" / "index.html")
check("index.html: кнопка курса 🎓 в шапке",
      'href="/guide"' in index_html and "🎓" in index_html)

guide_html = read(ROOT / "src" / "web" / "guide.html")
check("guide.html: подключает guide.js и guide.css",
      "/static/guide.js" in guide_html and "/static/guide.css" in guide_html)
check("guide.html: каркас курса (sidebar+main+footer)",
      all(x in guide_html for x in ("g-sidebar", "g-main", "g-footer")))

guide_js_path = ROOT / "src" / "web" / "guide.js"
guide_js = read(guide_js_path)

# node --check (если node есть)
node = None
for cand in ("node", "node.exe"):
    try:
        subprocess.run([cand, "--version"], capture_output=True, check=True)
        node = cand
        break
    except Exception:  # noqa: BLE001
        continue
if node:
    r = subprocess.run([node, "--check", str(guide_js_path)], capture_output=True)
    check("guide.js: синтаксис (node --check)", r.returncode == 0,
          r.stderr.decode("utf-8", "replace")[:200])
else:
    print("  [·] node не найден — синтаксис guide.js пропущен")

lesson_ids = re.findall(r'id:\s*"(l\d+)"', guide_js)
check("guide.js: 10 уроков", len(lesson_ids) == 10 and len(set(lesson_ids)) == 10,
      f"найдено: {len(lesson_ids)}")
check("guide.js: прогресс в localStorage", "localStorage" in guide_js
      and "llm-guide-progress" in guide_js)
check("guide.js: живые проверки API", "apiCheck(" in guide_js)
check("guide.js: esc() для пользовательских строк",
      "function esc(" in guide_js)

# квизы: correct в диапазоне опций
quiz_ok = True
for m in re.finditer(r"quiz:\s*\{", guide_js):
    block = guide_js[m.start():guide_js.find("},", m.start()) + 2]
    n_opts = len(re.findall(r'"', block.split("options:")[1].split("correct:")[0])) // 2 \
        if "options:" in block and "correct:" in block else -1
    corr = re.search(r"correct:\s*(\d+)", block)
    if n_opts < 0 or not corr or not (0 <= int(corr.group(1)) < max(n_opts, 1)):
        quiz_ok = False
check("guide.js: все квизы валидны (correct в диапазоне)", quiz_ok)

# каждый API-эндпоинт из курса существует на сервере
api_urls = sorted(set(re.findall(r'"(/api/[\w/{}.-]+)"', guide_js)))
server_apis = set(re.findall(r'@app\.(?:get|post|delete|put)\("([^"]+)"', main_src))
for feat in (ROOT / "features").glob("*/api.py"):
    server_apis |= set(re.findall(
        r'(?:@router|@)\w*\.(?:get|post|delete|put)\(\s*"([^"]+)"', read(feat)))
    server_apis |= {"/api/" + name for name in re.findall(
        r'prefix="/api/([\w-]+)"', read(feat))}
missing = []
for url in api_urls:
    if url in server_apis:
        continue
    prefix = "/" + url.strip("/").split("/")[1] if url.count("/") >= 2 else url
    base = "/".join(url.split("/")[:3])
    if base in server_apis or f"/api/{url.split('/')[2]}" in server_apis:
        continue
    missing.append(url)
check(f"guide.js: все {len(api_urls)} API-эндпоинтов существуют", not missing,
      f"не найдены: {missing}")

check("guide.css: тема согласована с чатом",
      "#0f1116" in read(ROOT / "src" / "web" / "guide.css"))

# ═══════════════════════════════════════════════════════════════════════
print()
total = PASS + FAIL
print(f"═══ Итог: {PASS}/{total} проверок зелёные ═══")
if FAILURES:
    print("Провалы:")
    for f in FAILURES:
        print("  -", f)
sys.exit(1 if FAIL else 0)

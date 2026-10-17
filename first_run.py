#!/usr/bin/env python3
# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — мастер первого запуска (интерактивная настройка)
#
# Запускается ДО установки зависимостей — только стандартная библиотека.
#
# Использование:
#   python first_run.py                  — интерактивный мастер (если не настроено)
#   python first_run.py --defaults       — без вопросов: каталоги + базы + .env
#   python first_run.py --reconfigure-ai — заново спросить только настройки AI
#   python first_run.py --reset          — сбросить настройку и пройти мастер снова
#   python first_run.py --check-llm      — проверка связи с LLM-провайдером
#                                          (опционально: --base-url/--api-key/--model)
#
# Что делает:
#   1. Автоматически определяет корень проекта (PROJECT_ROOT) и прописывает
#      его в .env абсолютным путём; все остальные пути — относительные.
#   2. Создаёт структуру каталогов проекта (data/, logs/ и подкаталоги).
#   3. Создаёт базы данных, если их нет, штатной схемой (через классы проекта).
#   4. Интерактивно спрашивает настройки AI-моделей:
#        основной путь — браузер/куки (DeepSeek web chat),
#        второстепенный — OpenAI-совместимый API.
#   5. Пишет .env, создаёт маркер data/.initialized.
# ═══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

# ── Корень проекта определяется автоматически ──────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent
ENV_FILE = PROJECT_ROOT / ".env"
ENV_EXAMPLE = PROJECT_ROOT / ".env.example"
MARKER = PROJECT_ROOT / "data" / ".initialized"

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
DIM = "\033[2m"


def _c(code: str, text: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"{code}{text}{RESET}"


def ok(msg: str) -> None:
    print(f"  {_c(GREEN, '✓')} {msg}")


def fail(msg: str) -> None:
    print(f"  {_c(RED, '✗')} {msg}")


def warn(msg: str) -> None:
    print(f"  {_c(YELLOW, '!')} {msg}")


def info(msg: str) -> None:
    print(f"  {_c(DIM, '·')} {msg}")


def section(title: str) -> None:
    line = "─" * max(0, 62 - len(title) - 4)
    print(f"\n{_c(BOLD + CYAN, '┌─ ' + title + ' ' + line)}")


# ═══════════════════════════════════════════════════════════════════════
# Диалоговые помощники (чистый ввод, без зависимостей)
# ═══════════════════════════════════════════════════════════════════════

def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def ask_yes(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    try:
        val = input(f"  {prompt} [{hint}]: ").strip().lower()
    except EOFError:
        return default
    if not val:
        return default
    return val in ("y", "yes", "д", "да", "1")


def ask_choice(prompt: str, options: list[str], default: int = 1) -> int:
    print(f"  {prompt}")
    for i, opt in enumerate(options, 1):
        marker = " ←" if i == default else ""
        print(f"    {i}) {opt}{marker}")
    try:
        val = input("  Выбор [1-{}]: ".format(len(options))).strip()
    except EOFError:
        return default
    try:
        n = int(val or default)
        if 1 <= n <= len(options):
            return n
    except ValueError:
        pass
    return default


# ═══════════════════════════════════════════════════════════════════════
# Каталоги и базы данных проекта (всё — внутри PROJECT_ROOT)
# ═══════════════════════════════════════════════════════════════════════

# Относительные пути — единое место дислокации баз проекта
DATA_DIRS = [
    "data",                  # корень данных
    "data/telemetry",        # телеметрия петель (дубль-каталог для отчётов)
    "data/harness_runs",     # прогоны harness-сценариев
    "data/db_backups",       # бэкапы БД (backup manager)
    "data/policies_backups", # бэкапы политик
    "data/embedder_cache",   # кэш эмбеддера (bge-m3)
    "data/storage",          # MCP storage (файлы агентов)
    "data/vectors",          # векторные индексы (LanceDB пишет сюда/рядом)
    "logs",                  # журналы
]

# Базы: относительный путь → (описание, инициализатор через класс проекта)
PROJECT_DBS = [
    ("data/cache.sqlite",         "кэш ответов LLM"),
    ("data/policies.sqlite",      "политики и история удалений"),
    ("data/loop_telemetry.sqlite","телеметрия петель"),
    ("data/audit.db",             "журнал аудита"),
    ("data/extraction.sqlite",    "кэш экстракции документов"),
]


def create_dirs() -> list[str]:
    created = []
    for rel in DATA_DIRS:
        p = PROJECT_ROOT / rel
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(rel)
    return created


def init_databases() -> None:
    """Создаёт базы штатной схемой через классы проекта (если файла нет)."""
    sys.path.insert(0, str(PROJECT_ROOT))

    jobs = [
        ("data/cache.sqlite", "ResponseCache",
         lambda p: _from_module("src.cache", "ResponseCache", p)),
        ("data/policies.sqlite", "PolicyStore",
         lambda p: _from_module("src.policies", "PolicyStore", p)),
        ("data/loop_telemetry.sqlite", "LoopTelemetry",
         lambda p: _from_module("src.loop.telemetry", "LoopTelemetry", p)),
        ("data/audit.db", "AuditStore",
         lambda p: _from_module("src.core.audit", "AuditStore", p)),
        ("data/extraction.sqlite", "ExtractionCache",
         lambda p: _from_module("src.extraction.cache", "ExtractionCache", p)),
    ]

    for rel, cls_name, factory in jobs:
        path = PROJECT_ROOT / rel
        if path.exists():
            ok(f"{rel} — уже существует ({cls_name})")
            continue
        try:
            factory(path)
            # контроль: файл создан и читается SQLite
            if path.exists():
                with sqlite3.connect(path) as conn:
                    tables = conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                ok(f"{rel} — создана ({cls_name}), таблиц: {len(tables)}")
            else:
                warn(f"{rel} — класс не создал файл, будет создана при старте")
        except Exception as exc:  # noqa: BLE001 — мастер не должен падать
            warn(f"{rel} — не создана ({type(exc).__name__}: {exc}); "
                 f"система создаст её при первом старте")

    # memory.sqlite / graph.sqlite / vector.lance создаёт подсистема памяти
    # при первом старте (схемы сложные, дублировать их здесь нельзя).
    info("data/memory.sqlite, data/graph.sqlite, data/vector.lance — "
         "создаст подсистема памяти при первом запуске сервера")


def _from_module(module: str, cls: str, path: Path):
    mod = __import__(module, fromlist=[cls])
    factory = getattr(mod, cls)
    try:
        return factory(path)
    except TypeError:
        return factory(str(path))


# ═══════════════════════════════════════════════════════════════════════
# .env: чтение / объединение / запись
# ═══════════════════════════════════════════════════════════════════════

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    """Записывает .env поверх шаблона .env.example, сохраняя комментарии."""
    template = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines() \
        if ENV_EXAMPLE.exists() else ["# LLM Agent configuration"]
    current = read_env(ENV_FILE)
    merged = {**current, **updates}

    seen: set[str] = set()
    out: list[str] = []
    for line in template:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in merged:
                out.append(f"{key}={merged[key]}")
                seen.add(key)
                continue
        out.append(line)
    # Ключи, которых нет в шаблоне, дописываем в конец
    extra = [k for k in merged if k not in seen]
    if extra:
        out.append("")
        out.append("# ── Добавлено мастером настройки ──")
        for k in extra:
            out.append(f"{k}={merged[k]}")
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# Настройки AI-моделей: основной путь — браузер/куки, API — второстепенный
# ═══════════════════════════════════════════════════════════════════════

# Порядок: сначала провайдеры, доступные из РФ/РБ, OpenAI — последним с
# предупреждением: он отвечает 403 unsupported_country_region_territory.
API_PRESETS = [
    ("DeepSeek API (работает в РФ)",     "https://api.deepseek.com/v1",   "deepseek-chat"),
    ("VseGPT — агрегатор (работает в РФ)", "https://api.vsegpt.ru/v1",    "openai/gpt-4o-mini"),
    ("ProxyAPI — агрегатор (работает в РФ)", "https://api.proxyapi.ru/v1", "gpt-4o-mini"),
    ("Ollama (локально, без ключа)",     "http://127.0.0.1:11434/v1",     "qwen2.5:7b"),
    ("OpenAI (из РФ/РБ даст 403 region!)", "https://api.openai.com/v1",   "gpt-4o-mini"),
]


def setup_ai(interactive: bool) -> dict[str, str]:
    """Спрашивает AI-настройки и возвращает обновления для .env."""
    updates: dict[str, str] = {}
    print()
    print(_c(BOLD, "  Режимы работы AI-моделей:"))
    print("   1. ОСНОВНОЙ — браузер/куки (веб-чат DeepSeek без API-ключей)")
    print("   2. ВТОРОСТЕПЕННЫЙ — OpenAI-совместимый API (ключ, фолбэк роутера)")

    # ── Основной: куки DeepSeek ────────────────────────────────────────
    cookies: dict[str, str] = {}
    if interactive and ask_yes(
            "Настроить основной путь — браузер/куки DeepSeek?", True):
        section("Куки DeepSeek (chat.deepseek.com)")
        print(_c(DIM, """
  Как получить куки:
    1. Открой https://chat.deepseek.com и войди в аккаунт
    2. F12 → Application (Приложение) → Cookies → https://chat.deepseek.com
    3. Скопируй значения перечисленных ниже куков
"""))
        cookies["DEEPSEEK_DS_SESSION_ID"] = ask(
            "ds_session_id (обязателен)")
        cookies["DEEPSEEK_SMIDV2"] = ask(
            "smidV2 (можно пропустить, Enter)")
        cookies["DEEPSEEK_THUMBCACHE"] = ask(
            ".thumbcache_6b2e5483... (можно пропустить, Enter)")
        cookies["DEEPSEEK_AUTH_TOKEN"] = ask(
            "userToken/bearer из заголовка Authorization (можно пропустить)")
        if not cookies["DEEPSEEK_DS_SESSION_ID"]:
            warn("ds_session_id пуст — deepseek-агент не сможет работать "
                 "через браузер. Запусти потом: python first_run.py --reconfigure-ai")
        else:
            ok("Куки приняты (основной путь — браузер)")

    # ── Второстепенный: API ────────────────────────────────────────────
    api_configured = False
    if interactive and ask_yes(
            "Настроить второстепенный путь — API-провайдер?", True):
        section("API-провайдер (OpenAI-совместимый)")
        print(_c(DIM,
                 "  Внимание: OpenAI напрямую из РФ/РБ отвечает 403 "
                 "unsupported_country_region_territory — выбирайте "
                 "DeepSeek / агрегаторы / Ollama"))
        custom_idx = len(API_PRESETS) + 1   # «Свой URL»
        skip_idx = len(API_PRESETS) + 2     # «Пропустить»
        preset_names = [f"{n} — {u}" for n, u, _ in API_PRESETS] + \
                       ["Свой URL", "Пропустить"]
        pick = ask_choice("Какой провайдер?", preset_names)
        if pick <= len(API_PRESETS):
            _, base_url, model = API_PRESETS[pick - 1]
            info(f"Профиль: {base_url} (модель {model})")
            updates["LLM_BASE_URL"] = ask("LLM_BASE_URL", base_url)
            updates["LLM_MODEL"] = ask("LLM_MODEL", model)
            updates["LLM_API_KEY"] = ask(
                "LLM_API_KEY (для Ollama — любая строка)", "")
        elif pick == custom_idx:
            updates["LLM_BASE_URL"] = ask("LLM_BASE_URL")
            updates["LLM_MODEL"] = ask("LLM_MODEL")
            updates["LLM_API_KEY"] = ask("LLM_API_KEY")
        elif pick == skip_idx:
            info("API пропущен — роутер будет использовать основной путь")
        else:
            info("API пропущен — роутер будет использовать основной путь")
        api_configured = "LLM_BASE_URL" in updates
        if api_configured and "openai.com" in updates["LLM_BASE_URL"]:
            warn("OpenAI не обслуживает РФ/РБ — почти наверняка получите "
                 "403 unsupported_country_region_territory")

    # ── Проверка связи с выбранным провайдером ─────────────────────────
    if interactive and api_configured and ask_yes(
            "Проверить связь с провайдером сейчас? (рекомендуется)", True):
        ok_check, msg = check_provider(
            updates["LLM_BASE_URL"], updates["LLM_API_KEY"],
            updates["LLM_MODEL"])
        (ok if ok_check else fail)(msg)
        if not ok_check:
            info("Перенастроить можно в любой момент: "
                 "python first_run.py --reconfigure-ai")

    # ── qwenproxy: браузерный путь роутера (Qwen web через Node-прокси) ─
    qp_enabled, qp_auto = None, None
    if interactive:
        section("qwenproxy — браузерный роутер (Qwen web chat)")
        node_found = shutil.which("node") is not None or \
            shutil.which("node.exe") is not None
        if not node_found:
            warn("Node.js не найден — qwenproxy не запустится")
        default_on = node_found and not cookies.get("DEEPSEEK_DS_SESSION_ID")
        qp_enabled = ask_yes(
            "Включить qwenproxy как роутер (браузерный путь)?", default_on)
        qp_auto = qp_enabled and ask_yes(
            "Автозапуск qwenproxy вместе с сервером?", True)
        if qp_enabled and node_found:
            info("Установка: npm install -g qwenproxy-cli; аккаунт: запустите "
                 "qpx → вкладка [5] Accounts → A (email и пароль от "
                 "chat.qwen.ai)")
    else:
        qp_enabled, qp_auto = False, False

    # ── Сборка итоговых значений .env ──────────────────────────────────
    updates["PROJECT_ROOT"] = str(PROJECT_ROOT)
    updates.update(cookies)
    updates.setdefault("LLM_BASE_URL", "http://127.0.0.1:7936/v1")
    updates.setdefault("LLM_MODEL", "qwen3.8-max")
    updates.setdefault("LLM_API_KEY", "")
    updates["QWENPROXY_ENABLED"] = "true" if qp_enabled else "false"
    updates["QWENPROXY_AUTO_START"] = "true" if qp_auto else "false"
    return updates


# ═══════════════════════════════════════════════════════════════════════
# Куки веб-чатов в .env (основной путь «браузер/куки»)
# ═══════════════════════════════════════════════════════════════════════

# Имена совпадают с src/web_chat.py (DEEPSEEK_ENV / QWEN_ENV) — по ним же
# llm_providers решает, добавлять ли модели «(web)» в селект. Держим синхронно.
DEEPSEEK_ENV_KEYS = ("DEEPSEEK_AUTH_TOKEN", "DEEPSEEK_DS_SESSION_ID",
                     "DEEPSEEK_SMIDV2")
QWEN_ENV_KEYS = ("QWEN_WEB_TOKEN", "QWEN_WEB_COOKIES")


def cookie_providers(env: dict[str, str]) -> list[str]:
    """Какие веб-чаты настроены куками (без чтения значений, только факт)."""
    found: list[str] = []
    if any((env.get(k) or "").strip() for k in DEEPSEEK_ENV_KEYS):
        found.append("DeepSeek (deepseek_web)")
    if any((env.get(k) or "").strip() for k in QWEN_ENV_KEYS):
        found.append("Qwen (qwen_web)")
    return found


# ═══════════════════════════════════════════════════════════════════════
# Проверка связи с LLM-провайдером (чистый stdlib, без openai)
# ═══════════════════════════════════════════════════════════════════════

def check_provider(base_url: str, api_key: str, model: str,
                   timeout: int = 20) -> tuple[bool, str]:
    """Минимальный POST /chat/completions к OpenAI-совместимому провайдеру.

    Возвращает (ok, сообщение). При ошибке сообщение содержит человекочитаемую
    подсказку (src/llm_errors.py) — ту же, которую пользователь увидит в чате.
    """
    sys.path.insert(0, str(PROJECT_ROOT))
    from src.llm_errors import describe

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return False, "LLM_BASE_URL пуст — провайдер не настроен"
    url = base + "/chat/completions"
    payload = json.dumps({
        "model": (model or "test").strip(),
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {(api_key or '').strip() or 'llm-agent-local'}",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            has_choices = bool(json.loads(raw).get("choices"))
        except Exception:  # noqa: BLE001 — 200 без JSON тоже считаем связью
            has_choices = False
        if has_choices:
            return True, f"Связь есть: {base} ответил корректным ответом чата"
        return True, (f"Связь есть (HTTP 200), но в ответе нет choices — "
                      f"проверьте имя модели '{model}' у провайдера")
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        hint = describe(e.code, body)
        if hint:
            return False, f"HTTP {e.code}: {hint}"
        return False, f"HTTP {e.code}: провайдер вернул ошибку: {body[:200]}"
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        hint = describe(None, f"connection {reason}")
        return False, f"Нет соединения с {base}: {hint or reason}"
    except Exception as e:  # noqa: BLE001 — диагностика не должна падать
        return False, f"{type(e).__name__}: {e}"


def check_llm_command(base_url: str | None, api_key: str | None,
                      model: str | None) -> int:
    """python first_run.py --check-llm — диагностика связи с провайдером."""
    banner()
    env = read_env(ENV_FILE)
    base = base_url or env.get("LLM_BASE_URL", "")
    key = api_key if api_key is not None else env.get("LLM_API_KEY", "")
    mdl = model or env.get("LLM_MODEL", "")

    section("Проверка связи с LLM-провайдером")
    web = cookie_providers(env)
    if web:
        ok("Основной путь — браузер/куки: " + ", ".join(web))
        info("Живая проверка: запустите сервер (python run.py) и выберите "
             "модель «DeepSeek (web)» в селекте моделей")
    qp_enabled = env.get("QWENPROXY_ENABLED", "").lower() == "true"

    if qp_enabled:
        host = env.get("QWENPROXY_HOST", "127.0.0.1")
        try:
            port = int(env.get("QWENPROXY_PORT", "7936"))
        except ValueError:
            port = 7936
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            port_open = sock.connect_ex((host, port)) == 0
        if port_open:
            ok(f"qwenproxy отвечает на {host}:{port}")
        else:
            fail(f"qwenproxy НЕ запущен на {host}:{port}")
            info("Запуск: qpx start (или python run.py — автозапуск "
                 "сам поднимет его)")
            info("Если не установлен: npm i -g qwenproxy-cli → запустите "
                 "qpx → вкладка [5] Accounts → A (email и пароль от "
                 "chat.qwen.ai)")
            if (not base or "127.0.0.1:3456" in base) and not web:
                return 2

    # ── Авто-миграция устаревшего дефолта порта (3456 → 7936) ─────────
    # qwenproxy-cli начиная с ранних версий слушает 7936; старые релизы
    # проекта писали в .env 3456. Если 7936 жив, а 3456 нет — чиним .env.
    if base and ":3456/" in base:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            old_open = sock.connect_ex(("127.0.0.1", 3456)) == 0
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            new_open = sock.connect_ex(("127.0.0.1", 7936)) == 0
        if new_open and not old_open:
            fixed = base.replace(":3456/", ":7936/")
            write_env({"LLM_BASE_URL": fixed, "QWENPROXY_PORT": "7936"})
            ok(f"LLM_BASE_URL обновлён: {base} → {fixed} "
               f"(старый дефолт 3456, qwenproxy слушает 7936)")
            base = fixed

    ok_flag, msg = check_provider(base, key, mdl)
    # Есть куки? Тогда недоступность API-роутера — не ошибка, а честный
    # статус необязательного второстепенного пути (без красного ✗ и rc=2)
    (ok if ok_flag else (info if web else fail))(msg)
    info(f"Профиль: {base} (модель {mdl or '?'})")
    if not ok_flag:
        if web:
            info("Это НЕ блокер: основной путь — куки, он не использует "
                 f"{base}. Если моделей «(web)» нет в селекте — сервер "
                 "запущен до записи кук в .env: перезапустите его")
            return 0
        print()
        info("Перенастроить AI: python first_run.py --reconfigure-ai")
        return 2
    return 0


def verify() -> None:
    section("Проверка конфигурации")
    env = read_env(ENV_FILE)
    if env.get("PROJECT_ROOT"):
        ok(f"PROJECT_ROOT = {env['PROJECT_ROOT']}")
    else:
        warn("PROJECT_ROOT не прописан в .env")
    for rel, _ in PROJECT_DBS:
        p = PROJECT_ROOT / rel
        (ok if p.exists() else warn)(f"{rel} {'готова' if p.exists() else 'отсутствует'}")
    try:
        import src.config as cfg  # noqa: PLC0415 — осознанный поздний импорт
        s = cfg.get_settings()
        cache_path = Path(getattr(s.cache, "path", ""))
        ok(f"Система видит кэш: {cache_path} "
           f"({'внутри проекта' if str(PROJECT_ROOT) in str(cache_path) or not cache_path.is_absolute() else 'ВНЕ проекта!'})")
    except Exception as exc:  # noqa: BLE001
        info(f"Полная проверка будет после установки зависимостей ({type(exc).__name__})")
    qp = env.get("QWENPROXY_ENABLED", "")
    web = cookie_providers(env)
    if web:
        info("Основной путь: браузер/куки (" + ", ".join(web) + ")")
    info(f"Роутер: {'qwenproxy (браузерный)' if qp == 'true' else 'API (' + env.get('LLM_BASE_URL', '?') + ')'}")


def write_marker(mode_primary: str, mode_secondary: str) -> None:
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(json.dumps({
        "configured_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": str(PROJECT_ROOT),
        "ai_primary": mode_primary,
        "ai_secondary": mode_secondary,
        "version": 1,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════
# Точка входа
# ═══════════════════════════════════════════════════════════════════════

def banner() -> None:
    print(_c(BOLD + CYAN, """
╔══════════════════════════════════════════════════════════╗
║        LLM Agent — первоначальная настройка проекта      ║
╚══════════════════════════════════════════════════════════╝"""))


def run_wizard(interactive: bool, reconfigure_ai: bool = False) -> bool:
    banner()
    section("Проект")
    ok(f"Корень проекта (авто): {_c(BOLD, str(PROJECT_ROOT))}")
    if ENV_FILE.exists():
        ok(".env найден — существующие значения сохраняются")

    updates: dict[str, str] = {"PROJECT_ROOT": str(PROJECT_ROOT)}
    ai_primary, ai_secondary = "browser/cookies", "api"

    if interactive and (reconfigure_ai or ask_yes(
            "Настроить AI-модели сейчас?", True)):
        updates = setup_ai(interactive=True)
        env = read_env(ENV_FILE)
        ai_primary = ("browser/cookies"
                      if updates.get("DEEPSEEK_DS_SESSION_ID") or env.get("DEEPSEEK_DS_SESSION_ID")
                      else "qwenproxy" if updates.get("QWENPROXY_ENABLED") == "true"
                      else "не настроен")
        ai_secondary = "api" if updates.get("LLM_API_KEY") else "пропущен"
    elif not interactive:
        ok("Режим --defaults: AI-настройки не изменяются "
           "(запусти с --reconfigure-ai позже)")

    section("Каталоги проекта")
    created = create_dirs()
    ok(f"Каталогов готово: {len(DATA_DIRS)} (создано новых: {len(created)})")

    section("Базы данных (в папке проекта: data/)")
    init_databases()

    section("Файл .env")
    write_env(updates)
    ok(f".env записан: {ENV_FILE}")

    verify()

    if not MARKER.exists():
        write_marker(ai_primary, ai_secondary)
        ok(f"Маркер настройки: {MARKER}")
    print()
    print(_c(GREEN + BOLD, "  ═══ Настройка завершена ═══"))
    print(f"  Следующий шаг: {_c(BOLD, 'python run.py')}")
    print()
    return True


def ensure_configured() -> bool:
    """Хук для run.py: первый запуск → мастер, иначе — ничего не делать."""
    if MARKER.exists():
        return True
    try:
        run_wizard(interactive=True)
        return True
    except KeyboardInterrupt:
        print()
        warn("Настройка прервана. Повторить: python first_run.py")
        return False


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="first_run", description="LLM Agent — первоначальная настройка")
    parser.add_argument("--defaults", action="store_true",
                        help="без вопросов: каталоги, базы, .env")
    parser.add_argument("--reconfigure-ai", action="store_true",
                        help="заново спросить настройки AI-моделей")
    parser.add_argument("--reset", action="store_true",
                        help="сбросить настройку и пройти мастер заново")
    parser.add_argument("--check-llm", action="store_true",
                        help="проверить связь с LLM-провайдером и выйти")
    parser.add_argument("--base-url", default=None,
                        help="для --check-llm: переопределить LLM_BASE_URL")
    parser.add_argument("--api-key", default=None,
                        help="для --check-llm: переопределить LLM_API_KEY")
    parser.add_argument("--model", default=None,
                        help="для --check-llm: переопределить LLM_MODEL")
    args = parser.parse_args()

    if args.check_llm:
        return check_llm_command(args.base_url, args.api_key, args.model)

    if args.reset and MARKER.exists():
        MARKER.unlink()
        ok("Маркер сброшен")

    if MARKER.exists() and not args.reconfigure_ai and not args.reset:
        print(_c(GREEN, "Проект уже настроен (data/.initialized). "
                        "Для повторной настройки: python first_run.py --reset"))
        return 0

    try:
        run_wizard(
            interactive=not args.defaults,
            reconfigure_ai=args.reconfigure_ai,
        )
    except KeyboardInterrupt:
        print()
        warn("Настройка прервана. Повторить: python first_run.py")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

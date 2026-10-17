"""Общий помощник работы с .env (чтение / точечное обновление).

Используется Task 24-a (куки веб-чатов из расширения) и Task 24-b
(postgreSQL-автодетект): обе подсистемы дописывают ключи в .env
и тут же обновляют os.environ текущего процесса — чтобы подхватили
MCP-серверы (они наследуют окружение при старте, см. MCPManager) и
остальные подсистемы без перезапуска.

Семантика записи — как в first_run.write_env: шаблон .env.example
сохраняется вместе с комментариями, существующий .env читается,
значения объединяются (updates поверх), ключи, которых нет в шаблоне,
дописываются в конец под маркером «добавлено автоматически».

Потокобезопасность: threading.Lock на запись (атомарный tmp+replace).
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_LOCK = threading.Lock()

AUTO_MARKER = "# ── Добавлено автоматически (llm-agent) ──"


def project_root() -> Path:
    """Корень проекта: PROJECT_ROOT из окружения, иначе каталог репозитория."""
    env_root = os.getenv("PROJECT_ROOT", "").strip()
    if env_root:
        p = Path(env_root)
        if p.is_dir():
            return p
    return BASE_DIR


def env_path() -> Path:
    return project_root() / ".env"


def example_path() -> Path:
    return project_root() / ".env.example"


def read_env(path: str | Path | None = None) -> dict[str, str]:
    """Плоский разбор .env: KEY=VALUE, комментарии игнорируются."""
    p = Path(path) if path else env_path()
    values: dict[str, str] = {}
    try:
        raw = p.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return values
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        values[key.strip()] = val.strip().strip('"').strip("'")
    return values


def write_env_updates(updates: dict[str, str],
                      path: str | Path | None = None,
                      template: str | Path | None = None) -> dict:
    """Дописывает/обновляет ключи в .env, сохраняя комментарии шаблона.

    template — источник структуры (по умолчанию .env.example рядом
    с project_root()); при передаче кастомного path в тестах шаблон
    тоже стоит передать явно.
    Возвращает отчёт: {path, updated: [...], added: [...]}.
    Никогда не пишет пустые значения (пустая строка = удалить значение
    из .env не получится — такие ключи просто пропускаются; сброс
    делается вручную или через DELETE-эндпоинты подсистем).
    """
    p = Path(path) if path else env_path()
    tpl = Path(template) if template else example_path()
    clean = {k: str(v).strip() for k, v in (updates or {}).items()
             if str(v).strip() != ""}
    if not clean:
        return {"path": str(p), "updated": [], "added": []}

    template: list[str] = ["# LLM Agent configuration"]
    try:
        template = tpl.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        pass

    with _LOCK:
        current = read_env(p)
        merged = {**current, **clean}

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

        added: list[str] = []
        extra = [k for k in merged if k not in seen]
        if extra:
            out.append("")
            out.append(AUTO_MARKER)
            for k in extra:
                out.append(f"{k}={merged[k]}")
                added.append(k)

        tmp = p.with_suffix(".env.tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, p)

    updated = sorted(set(clean) - set(added))
    return {"path": str(p), "updated": updated, "added": added}


def apply_to_environ(updates: dict[str, str]) -> None:
    """Тот же набор ключей — в os.environ текущего процесса (для MCP и др.)."""
    for k, v in (updates or {}).items():
        if str(v).strip() == "":
            continue
        os.environ[str(k)] = str(v).strip()


def read_and_apply(path: str | Path | None = None) -> dict[str, str]:
    """Читает .env и вливает в os.environ (для старта подсистем)."""
    vals = read_env(path)
    apply_to_environ(vals)
    return vals

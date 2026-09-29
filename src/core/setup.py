"""Структурные отчёты о требованиях модулей — для UI быстрой настройки.

RequirementChecker (src/core/health.py) отдаёт только текстовые сообщения
и используется снапшотом агентов. UI быстрой настройки («Настроить и
запустить» в карточках MCP/агентов и вкладка «Настройка») нуждается в
структурированных данных: какого типа требование не выполнено, имя
переменной/пакета, задана ли переменная окружения, что подсказать
(``pip install ...``). Здесь Requirements декларации превращается в такой
отчёт.

Безопасность: значения env-переменных НАРОЧНО не возвращаются (секреты) —
только факт «задана/не задана».

Статусы отчёта:
  ok             — все hard-требования выполнены (soft могут быть не выполнены)
  not_configured — отсутствует хотя бы одно hard-требование
  degraded       — hard выполнены, но есть невыполненные soft
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from src.core.schema import (
    AgentSchema,
    MCPServerSchema,
    RequirementLevel,
    Requirements,
)

# Допустимое имя переменной окружения (защита эндпоинта /config)
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Быстрый набор проверок (без сети) — для списков и сводок
FAST_KINDS = ("env", "package", "path")
# Полный набор — для карточки настройки одного модуля
FULL_KINDS = FAST_KINDS + ("external",)


def sanitize_env_updates(env: dict | None) -> tuple[dict[str, str], list[str]]:
    """Отбирает корректные непустые K=V для записи в .env.

    Возвращает (чистый словарь, список отброшенных ключей).
    Пустое значение = «не менять» (env_file.write_env_updates так и
    трактует), неверное имя ключа — отбрасывается с попаданием в skipped.
    """
    clean: dict[str, str] = {}
    skipped: list[str] = []
    for key, val in (env or {}).items():
        k = str(key).strip()
        v = str(val).strip()
        if not v:
            skipped.append(k)
            continue
        if not ENV_KEY_RE.match(k):
            skipped.append(k)
            continue
        clean[k] = v
    return clean, skipped


def _item(
    kind: str,
    name: str,
    level: RequirementLevel,
    missing: bool,
    message: str | None = None,
    hint: str | None = None,
    env_ref: str | None = None,
) -> dict:
    return {
        "kind": kind,
        "name": name,
        "level": level.value,
        "missing": missing,
        "message": message,
        "hint": hint,
        "env_ref": env_ref,
    }


def _path_ok(r) -> bool:
    raw = r.path or (os.getenv(r.env, "") if r.env else "")
    if not raw:
        return False
    p = Path(raw).expanduser()
    if not p.exists():
        return False
    if r.type == "dir" and not p.is_dir():
        return False
    if r.type == "file" and not p.is_file():
        return False
    if r.writable and not os.access(p, os.W_OK):
        return False
    return True


def module_requirements(
    schema: AgentSchema | MCPServerSchema,
    checker,
    include_external: bool = False,
) -> dict:
    """Структурный отчёт по требованиям декларации (агент или MCP-сервер).

    checker — src.core.health.RequirementChecker (нужен для проверки
    пакетов с кэшем; external-проверки включаются только для карточки
    одного модуля, чтобы списки не ходили по сети).
    """
    req: Requirements = schema.requires
    items: list[dict] = []

    # ── env ────────────────────────────────────────────────
    for r in req.env_vars:
        present = bool(os.getenv(r.name, "").strip())
        items.append(_item(
            "env", r.name, r.level, not present,
            message=None if present else (
                r.message or f"Переменная '{r.name}' не задана"
            ),
            hint=None if present else (
                f"Задайте переменную {r.name} (сохранится в .env)"
            ),
        ))

    # ── python-пакеты ──────────────────────────────────────
    for r in req.python_packages:
        installed = bool(checker._check_pkg(r.name))  # noqa: SLF001
        items.append(_item(
            "package", r.name, r.level, not installed,
            message=None if installed else (
                r.message or f"Пакет '{r.name}' не установлен"
            ),
            hint=None if installed else f"pip install {r.name}",
        ))

    # ── пути ───────────────────────────────────────────────
    for r in req.paths:
        ok = _path_ok(r)
        env_unset = bool(r.env) and not os.getenv(r.env, "").strip()
        items.append(_item(
            "path", r.name, r.level, not ok,
            message=None if ok else (
                r.message
                or (f"Переменная '{r.env}' не задана (путь '{r.name}')"
                    if env_unset else f"Путь недоступен: {r.path or r.name}")
            ),
            hint=r.env if env_unset else None,
            env_ref=r.env,
        ))

    # ── внешние сервисы (только для карточки одного модуля) ─
    if include_external:
        for r in req.external:
            hard, soft, _health = checker.check_external([r])
            missing = bool(hard or soft)
            items.append(_item(
                "external", f"{r.protocol}://{r.host}:{r.port}{r.path}",
                r.level, missing,
                message=(hard or soft)[0] if missing else None,
            ))

    # ── итоги ──────────────────────────────────────────────
    missing_hard = [
        (i["message"] or i["name"]) for i in items
        if i["missing"] and i["level"] == RequirementLevel.HARD.value
    ]
    missing_soft = [
        (i["message"] or i["name"]) for i in items
        if i["missing"] and i["level"] == RequirementLevel.SOFT.value
    ]
    status = (
        "not_configured" if missing_hard
        else ("degraded" if missing_soft else "ok")
    )

    # Поля, которые можно заполнить в форме: env-переменные (свои +
    # подключённые через path.env_ref)
    editable = sorted({
        i["name"] for i in items if i["kind"] == "env"
    } | {
        i["env_ref"] for i in items
        if i["kind"] == "path" and i["env_ref"]
    })

    return {
        "status": status,
        "items": items,
        "missing_hard": missing_hard,
        "missing_soft": missing_soft,
        "editable_env": editable,
    }

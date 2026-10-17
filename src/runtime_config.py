"""Обёртка над core.runtime_config: get/set PROJECT_ROOT для MCP-серверов.

MCP-серверы не имеют доступа к Registry, поэтому читают PROJECT_ROOT
из data/runtime.json (или .env как fallback).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
RUNTIME_PATH = BASE_DIR / "data" / "runtime.json"


def _read() -> dict:
    if not RUNTIME_PATH.exists():
        return {}
    try:
        return json.loads(RUNTIME_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("Не прочитать runtime.json: %s", e)
        return {}


def _write(data: dict) -> None:
    RUNTIME_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUNTIME_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def get_project_root(default: str = "") -> str:
    data = _read()
    val = str(data.get("project_root", "")).strip()
    if val:
        return val
    env_val = os.getenv("PROJECT_ROOT", "").strip()
    return env_val or default


def set_project_root(path: str) -> str:
    p = str(Path(path).expanduser().resolve())
    data = _read()
    data["project_root"] = p
    _write(data)
    os.environ["PROJECT_ROOT"] = p
    logger.info("PROJECT_ROOT обновлён: %s", p)
    return p

"""Профили настроек: продакшен, разработка, 1С, пользовательские."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


BUILTIN_PROFILES = {
    "default": {
        "title": "По умолчанию",
        "description": "Стандартный профиль без изменений",
        "overrides": {},
    },
    "development": {
        "title": "Разработка",
        "description": "Все агенты включены, максимальные лимиты",
        "overrides": {
            "disabled_agents": [],
            "disabled_mcp_servers": [],
            "agent_params": {
                "file": {"max_steps": 25},
                "deepseek": {"model": "deepseek-reasoner"},
            },
        },
    },
    "production": {
        "title": "Продакшен",
        "description": "Только чтение, опасные операции отключены",
        "overrides": {
            "disabled_agents": ["shell", "git"],
            "disabled_mcp_servers": [],
            "agent_params": {},
        },
    },
    "1c_development": {
        "title": "1С-разработка",
        "description": "1С + файлы + deepseek",
        "overrides": {
            "disabled_agents": ["mysql", "postgres"],
            "agent_params": {
                "file": {"priority": 20},
                "onec": {"priority": 15, "max_steps": 20},
                "deepseek": {"priority": 10},
            },
        },
    },
}


@dataclass
class Profile:
    id: str
    title: str
    description: str = ""
    overrides: dict = field(default_factory=dict)
    is_builtin: bool = False
    created_at: float = 0.0
    updated_at: float = 0.0


class ProfileStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._init_builtins()

    def _init_builtins(self) -> None:
        for pid, cfg in BUILTIN_PROFILES.items():
            f = self.path / f"{pid}.json"
            if not f.exists():
                self._write(Profile(
                    id=pid,
                    title=cfg["title"],
                    description=cfg["description"],
                    overrides=cfg["overrides"],
                    is_builtin=True,
                    created_at=time.time(),
                    updated_at=time.time(),
                ))

    # ═══════════════════════════════════════════════════════
    # CRUD
    # ═══════════════════════════════════════════════════════
    def list(self) -> list[Profile]:
        result: list[Profile] = []
        for f in sorted(self.path.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                result.append(Profile(**data))
            except Exception as e:
                logger.debug("Не прочитать профиль %s: %s", f, e)
        return result

    def get(self, pid: str) -> Profile | None:
        f = self.path / f"{pid}.json"
        if not f.exists():
            return None
        try:
            return Profile(**json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            return None

    def save(self, profile: Profile) -> None:
        if profile.is_builtin:
            return
        profile.updated_at = time.time()
        if not profile.created_at:
            profile.created_at = profile.updated_at
        self._write(profile)

    def _write(self, p: Profile) -> None:
        f = self.path / f"{p.id}.json"
        f.write_text(
            json.dumps(asdict(p), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def delete(self, pid: str) -> bool:
        p = self.get(pid)
        if not p or p.is_builtin:
            return False
        f = self.path / f"{pid}.json"
        try:
            f.unlink()
            return True
        except OSError:
            return False

    # ═══════════════════════════════════════════════════════
    # Импорт / экспорт
    # ═══════════════════════════════════════════════════════
    def export(self, pid: str) -> dict:
        p = self.get(pid)
        if not p:
            raise ValueError(f"Профиль не найден: {pid}")
        return {
            "format": "llm-agent-profile",
            "version": 1,
            "exported_at": time.time(),
            "profile": {
                "id": p.id,
                "title": p.title,
                "description": p.description,
                "overrides": p.overrides,
            },
        }

    def import_data(self, data: dict, new_id: str | None = None) -> Profile:
        if data.get("format") != "llm-agent-profile":
            raise ValueError("Неверный формат файла")
        p_data = data.get("profile", {})
        pid = new_id or p_data.get("id", "imported")
        if self.get(pid) and not new_id:
            pid = f"{pid}_{int(time.time())}"
        p = Profile(
            id=pid,
            title=p_data.get("title", pid),
            description=p_data.get("description", ""),
            overrides=p_data.get("overrides", {}),
            is_builtin=False,
            created_at=time.time(),
            updated_at=time.time(),
        )
        self._write(p)
        return p

    def export_active_as_overrides(self, active_overrides: dict) -> dict:
        return {
            "format": "llm-agent-profile",
            "version": 1,
            "exported_at": time.time(),
            "profile": {
                "id": "snapshot",
                "title": "Текущие настройки",
                "description": "Экспортировано из активного runtime",
                "overrides": active_overrides,
            },
        }

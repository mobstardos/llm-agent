"""runtime.yaml — дельта пользовательских настроек над декларациями.

Единственное место, куда пишутся изменения из UI, health-loop, watcher.
Атомарная запись через os.replace.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = BASE_DIR / "data" / "runtime.yaml"

CURRENT_RUNTIME_VERSION = 1


class RuntimeConfig:
    """Атомарный runtime-конфиг с мержем и версионированием."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else DEFAULT_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._load()

    # ═══════════════════════════════════════════════════════
    # Load / Save
    # ═══════════════════════════════════════════════════════
    def _load(self) -> None:
        if not self.path.exists():
            self._data = self._defaults()
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            self._data = self._merge_with_defaults(loaded)
        except Exception as e:
            logger.exception("Ошибка чтения runtime.yaml: %s", e)
            self._data = self._defaults()

    def _defaults(self) -> dict:
        return {
            "version": CURRENT_RUNTIME_VERSION,
            "written_at": None,
            "written_by": None,
            "user_overrides": {
                "disabled_agents": [],
                "disabled_mcp_servers": [],
                "enabled_agents": [],
                "agent_params": {},
                "mcp_params": {},
                "forced_capabilities": {},
            },
            "health_cache": {},
            "model_preferences": {
                "default": None,
                "per_agent": {},
            },
            "memory_state": {
                "last_index_at": None,
                "last_profile_refresh_at": None,
                "last_graph_rebuild_at": None,
            },
            "last_snapshot_at": None,
        }

    def _merge_with_defaults(self, loaded: dict) -> dict:
        defaults = self._defaults()
        result = {**defaults, **loaded}
        # Мержим вложенные секции
        for section in ("user_overrides", "model_preferences", "memory_state"):
            if section in loaded and isinstance(loaded[section], dict):
                result[section] = {**defaults[section], **loaded[section]}
        return result

    def _write(self, written_by: str = "system") -> None:
        self._data["written_at"] = datetime.now(timezone.utc).isoformat()
        self._data["written_by"] = written_by
        tmp = self.path.with_suffix(".yaml.tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    self._data, f, allow_unicode=True, sort_keys=False,
                )
            os.replace(tmp, self.path)
            self._dirty = False
        except Exception as e:
            logger.exception("Ошибка записи runtime.yaml: %s", e)
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # ═══════════════════════════════════════════════════════
    # Read
    # ═══════════════════════════════════════════════════════
    def get(self) -> dict:
        return self._data

    def get_overrides(self) -> dict:
        return dict(self._data.get("user_overrides", {}))

    def is_agent_disabled(self, agent_id: str) -> bool:
        return agent_id in self.get_overrides().get("disabled_agents", [])

    def is_agent_forced_enabled(self, agent_id: str) -> bool:
        return agent_id in self.get_overrides().get("enabled_agents", [])

    def is_mcp_disabled(self, mcp_id: str) -> bool:
        return mcp_id in self.get_overrides().get("disabled_mcp_servers", [])

    def get_agent_params(self, agent_id: str) -> dict:
        return dict(
            self.get_overrides().get("agent_params", {}).get(agent_id, {})
        )

    def get_mcp_params(self, mcp_id: str) -> dict:
        return dict(
            self.get_overrides().get("mcp_params", {}).get(mcp_id, {})
        )

    def get_forced_capability(self, cap_id: str) -> str | None:
        return self.get_overrides().get("forced_capabilities", {}).get(cap_id)

    def get_model_pref(self, agent_id: str | None = None) -> str | None:
        prefs = self._data.get("model_preferences", {})
        if agent_id:
            return prefs.get("per_agent", {}).get(agent_id)
        return prefs.get("default")

    def get_health(self, key: str) -> dict | None:
        return self._data.get("health_cache", {}).get(key)

    def get_memory_state(self) -> dict:
        return dict(self._data.get("memory_state", {}))

    # ═══════════════════════════════════════════════════════
    # Write
    # ═══════════════════════════════════════════════════════
    def set_agent_enabled(
        self, agent_id: str, enabled: bool, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        disabled = set(o.get("disabled_agents", []))
        forced = set(o.get("enabled_agents", []))
        if enabled:
            disabled.discard(agent_id)
            forced.add(agent_id)
        else:
            forced.discard(agent_id)
            disabled.add(agent_id)
        o["disabled_agents"] = sorted(disabled)
        o["enabled_agents"] = sorted(forced)
        self._write(written_by)

    def set_agent_params(
        self, agent_id: str, params: dict, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        agent_params = o.setdefault("agent_params", {})
        current = agent_params.get(agent_id, {})
        current.update(params)
        agent_params[agent_id] = current
        self._write(written_by)

    def reset_agent_params(
        self, agent_id: str, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        agent_params = o.setdefault("agent_params", {})
        agent_params.pop(agent_id, None)
        self._write(written_by)

    def set_mcp_enabled(
        self, mcp_id: str, enabled: bool, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        disabled = set(o.get("disabled_mcp_servers", []))
        if enabled:
            disabled.discard(mcp_id)
        else:
            disabled.add(mcp_id)
        o["disabled_mcp_servers"] = sorted(disabled)
        self._write(written_by)

    def set_mcp_params(
        self, mcp_id: str, params: dict, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        mcp_params = o.setdefault("mcp_params", {})
        current = mcp_params.get(mcp_id, {})
        current.update(params)
        mcp_params[mcp_id] = current
        self._write(written_by)

    def set_forced_capability(
        self, cap_id: str, provider_id: str | None, written_by: str = "ui",
    ) -> None:
        o = self._data.setdefault("user_overrides", {})
        forced = o.setdefault("forced_capabilities", {})
        if provider_id:
            forced[cap_id] = provider_id
        else:
            forced.pop(cap_id, None)
        self._write(written_by)

    def set_model_pref(
        self, model: str | None, agent_id: str | None = None,
        written_by: str = "ui",
    ) -> None:
        prefs = self._data.setdefault("model_preferences", {})
        if agent_id:
            if model:
                prefs.setdefault("per_agent", {})[agent_id] = model
            else:
                prefs.get("per_agent", {}).pop(agent_id, None)
        else:
            prefs["default"] = model
        self._write(written_by)

    def set_health(
        self, key: str, status: str,
        latency_ms: float | None = None,
        error: str | None = None,
        written_by: str = "health-loop",
    ) -> None:
        cache = self._data.setdefault("health_cache", {})
        cache[key] = {
            "status": status,
            "latency_ms": latency_ms,
            "error": error,
            "checked_at": time.time(),
        }
        self._write(written_by)

    def set_memory_state(
        self, key: str, value: Any, written_by: str = "memory",
    ) -> None:
        state = self._data.setdefault("memory_state", {})
        state[key] = value
        self._write(written_by)

    def set_last_snapshot(self, ts: float | None = None) -> None:
        self._data["last_snapshot_at"] = ts or time.time()
        self._write("snapshot")

    # ═══════════════════════════════════════════════════════
    # Bulk
    # ═══════════════════════════════════════════════════════
    def replace_overrides(
        self, overrides: dict, written_by: str = "ui",
    ) -> None:
        """Заменить все overrides (используется профилями)."""
        self._data["user_overrides"] = {
            **self._defaults()["user_overrides"],
            **overrides,
        }
        self._write(written_by)

    def merge_overrides(
        self, overrides: dict, written_by: str = "ui",
    ) -> None:
        """Слить overrides с текущими."""
        current = self.get_overrides()
        merged = {**current, **overrides}
        self._data["user_overrides"] = merged
        self._write(written_by)

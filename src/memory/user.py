"""User profile."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.memory.config import UserSettings

logger = logging.getLogger(__name__)

DEFAULT_PROFILE = {
    "preferences": {
        "language": "russian",
        "code_style": "краткий, без лишних комментариев",
        "notification_level": "только ошибки",
    },
    "patterns": [],
    "stats": {"approved": 0, "rejected": 0},
    "notes": [],
}


class UserProfile:
    def __init__(self, cfg: UserSettings, base_dir: Path):
        self.cfg = cfg
        self.path = base_dir / cfg.path
        self._data: dict | None = None

    def load(self) -> dict:
        if self._data is not None:
            return self._data
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    self._data = yaml.safe_load(f) or {}
                    return self._data
            except Exception as e:
                logger.warning("Ошибка чтения user profile: %s", e)
        self._data = dict(DEFAULT_PROFILE)
        self._save()
        return self._data

    def _save(self) -> None:
        if self._data is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self._data, f, allow_unicode=True, sort_keys=False)

    def record_approval(self, tool: str, path: str | None = None) -> None:
        if not self.cfg.learn_from_approvals:
            return
        data = self.load()
        data.setdefault("stats", {})["approved"] = (
            data["stats"].get("approved", 0) + 1
        )
        self._maybe_add_pattern(data, tool, path, "approved")
        self._save()

    def record_rejection(self, tool: str, path: str | None = None) -> None:
        if not self.cfg.learn_from_rejections:
            return
        data = self.load()
        data.setdefault("stats", {})["rejected"] = (
            data["stats"].get("rejected", 0) + 1
        )
        self._maybe_add_pattern(data, tool, path, "rejected")
        self._save()

    def _maybe_add_pattern(
        self, data: dict, tool: str, path: str | None, action: str,
    ) -> None:
        ext = ""
        if path:
            from pathlib import Path as P
            ext = P(path).suffix.lower()
        pattern = f"{action}: {tool}"
        if ext:
            pattern += f" ({ext})"

        patterns = data.setdefault("patterns", [])
        for p in patterns:
            if p.get("pattern") == pattern:
                p["count"] = p.get("count", 0) + 1
                return
        patterns.append({"pattern": pattern, "count": 1})

    def set_preference(self, key: str, value) -> None:
        data = self.load()
        data.setdefault("preferences", {})[key] = value
        self._save()

    def add_note(self, note: str) -> None:
        data = self.load()
        data.setdefault("notes", []).append(note)
        self._save()

    def render_for_prompt(self) -> str:
        data = self.load()
        if not data:
            return ""
        lines = []
        prefs = data.get("preferences", {})
        if prefs:
            lines.append("Предпочтения:")
            for k, v in prefs.items():
                lines.append(f"  - {k}: {v}")
        patterns = data.get("patterns", [])
        if patterns:
            lines.append("Паттерны:")
            for p in sorted(patterns, key=lambda x: -x.get("count", 0))[:5]:
                lines.append(f"  - {p['pattern']} (×{p.get('count', 0)})")
        notes = data.get("notes", [])
        if notes:
            lines.append("Заметки:")
            for n in notes[-5:]:
                lines.append(f"  - {n}")
        return "\n".join(lines)

    def stats(self) -> dict:
        return self.load().get("stats", {})

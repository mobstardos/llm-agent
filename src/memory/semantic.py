"""Semantic memory: профиль проекта."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.memory.config import SemanticSettings

logger = logging.getLogger(__name__)

LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".go": "go",
    ".rs": "rust", ".java": "java", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".bsl": "bsl",
    ".sql": "sql", ".sh": "shell", ".md": "markdown",
}

PROFILE_MD_TEMPLATE = """# Профиль проекта

**Корень:** `{root}`
**Тип:** {ptype}
**Обновлён:** {updated_at}

## Языки

{langs}

## Структура

{structure}

## Точки входа

{entrypoints}

## Файлы конфигурации

{configs}
"""


class ProjectProfile:
    def __init__(self, settings: SemanticSettings):
        self.cfg = settings
        self.yaml_path = Path(settings.profile_path)
        self.md_path = Path(settings.profile_md_path)
        self._cache: dict | None = None

    def load(self) -> dict:
        if self._cache is not None:
            return self._cache
        if self.yaml_path.exists():
            try:
                with open(self.yaml_path, "r", encoding="utf-8") as f:
                    self._cache = yaml.safe_load(f) or {}
                    return self._cache
            except Exception as e:
                logger.warning("Ошибка чтения профиля: %s", e)
        return {}

    def save(self, profile: dict) -> None:
        self.yaml_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.yaml_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)
        self._cache = profile

    def generate(self, root: str) -> dict:
        p = Path(root)
        if not p.exists():
            return {}

        langs: dict[str, int] = {}
        configs: list[str] = []
        total_files = 0
        ignore_dirs = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            ".idea", ".vscode", "dist", "build", "data",
        }

        for path in p.rglob("*"):
            if any(part in ignore_dirs for part in path.parts):
                continue
            if not path.is_file():
                continue
            total_files += 1
            ext = path.suffix.lower()
            lang = LANG_BY_EXT.get(ext)
            if lang:
                langs[lang] = langs.get(lang, 0) + 1
            if path.name in {
                "requirements.txt", "pyproject.toml", "package.json",
                "Cargo.toml", "go.mod", "Configuration.xml",
                "docker-compose.yml", "Dockerfile",
            }:
                configs.append(str(path.relative_to(p)))

        return {
            "project": {
                "name": p.name,
                "root": str(p),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "total_files": total_files,
            },
            "languages": dict(sorted(langs.items(), key=lambda x: -x[1])),
            "structure": self._detect_structure(p),
            "entrypoints": self._detect_entrypoints(p),
            "config_files": configs[:30],
        }

    def generate_and_save(self, root: str) -> dict:
        prof = self.generate(root)
        if prof:
            self.save(prof)
            self._write_markdown(prof)
        return prof

    def _detect_structure(self, root: Path) -> dict[str, str]:
        known = {
            "src": "исходники", "tests": "тесты", "docs": "документация",
            "agents": "плагины агентов", "mcp_servers": "MCP-серверы",
            "config": "yaml-конфиги", "data": "runtime",
            "scripts": "скрипты", "loops": "loop-декларации",
            "harness": "тесты-сценарии",
        }
        return {
            d.name: known[d.name]
            for d in root.iterdir()
            if d.is_dir() and d.name in known
        }

    def _detect_entrypoints(self, root: Path) -> dict[str, str]:
        result = {}
        for name in ("run.py", "main.py", "__main__.py", "manage.py", "cli.py"):
            if (root / name).exists():
                result[name] = "точка входа"
        for name in ("src/main.py", "app/main.py"):
            if (root / name).exists():
                result[name] = "точка входа"
        return result

    def _write_markdown(self, prof: dict) -> None:
        try:
            self.md_path.parent.mkdir(parents=True, exist_ok=True)
            langs = "\n".join(
                f"- **{k}**: {v} файлов"
                for k, v in prof.get("languages", {}).items()
            )
            structure = "\n".join(
                f"- `{k}/` — {v}"
                for k, v in prof.get("structure", {}).items()
            )
            entrypoints = "\n".join(
                f"- `{k}` — {v}"
                for k, v in prof.get("entrypoints", {}).items()
            )
            configs = "\n".join(
                f"- `{c}`" for c in prof.get("config_files", [])
            )
            md = PROFILE_MD_TEMPLATE.format(
                root=prof["project"]["root"],
                ptype=prof["project"]["name"],
                updated_at=prof["project"]["updated_at"],
                langs=langs or "—",
                structure=structure or "—",
                entrypoints=entrypoints or "—",
                configs=configs or "—",
            )
            self.md_path.write_text(md, encoding="utf-8")
        except Exception as e:
            logger.warning("Ошибка записи MD: %s", e)

    def render_for_prompt(self) -> str:
        prof = self.load()
        if not prof:
            return ""
        lines = []
        lines.append(f"Проект: {prof.get('project', {}).get('name', '?')}")
        langs = prof.get("languages", {})
        if langs:
            top = ", ".join(f"{k} ({v})" for k, v in list(langs.items())[:5])
            lines.append(f"Языки: {top}")
        struct = prof.get("structure", {})
        if struct:
            lines.append("Структура: " + ", ".join(f"{k}/" for k in struct))
        entry = prof.get("entrypoints", {})
        if entry:
            lines.append("Точки входа: " + ", ".join(entry))
        return "\n".join(lines)

"""Fixtures: подготовка sandbox-окружения для сценария."""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

from src.core.schema import FixtureSpec

logger = logging.getLogger(__name__)


class FixtureRunner:
    """Применяет fixture к sandbox-директории."""

    def __init__(self, sandbox: Path, project_root: Path | None = None):
        self.sandbox = sandbox
        self.source_project_root = project_root

    async def apply(self, spec: FixtureSpec | None) -> Path:
        """Возвращает фактический PROJECT_ROOT для запуска."""
        if spec is None:
            return self.sandbox

        kind = spec.type
        params = spec.params or {}

        if kind == "temp_project":
            return await self._temp_project(params)
        if kind == "git_repo":
            return await self._git_repo(params)
        if kind == "copy_dir":
            return await self._copy_dir(params)
        if kind == "env_vars":
            return await self._env_vars(params)
        if kind == "composite":
            return await self._composite(params)
        logger.warning("Неизвестный fixture: %s", kind)
        return self.sandbox

    # ─── Handlers ─────────────────────────────────────
    async def _temp_project(self, params: dict) -> Path:
        project = self.sandbox / "project"
        project.mkdir(parents=True, exist_ok=True)
        files = params.get("files", {})
        for rel, content in files.items():
            p = project / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        logger.info("Fixture temp_project: %d файлов", len(files))
        return project

    async def _git_repo(self, params: dict) -> Path:
        project = await self._temp_project(params)
        try:
            subprocess.run(
                ["git", "init"],
                cwd=project, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "config", "user.email", "harness@test"],
                cwd=project, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "config", "user.name", "Harness"],
                cwd=project, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "add", "-A"],
                cwd=project, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            subprocess.run(
                ["git", "commit", "-m", "initial", "--allow-empty"],
                cwd=project, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logger.warning("git не найден")
        except subprocess.CalledProcessError as e:
            logger.warning("git init failed: %s", e)
        return project

    async def _copy_dir(self, params: dict) -> Path:
        src = Path(params.get("source", ""))
        if not src.exists():
            logger.warning("Источник не существует: %s", src)
            return await self._temp_project({})
        project = self.sandbox / "project"
        shutil.copytree(src, project, dirs_exist_ok=True)
        return project

    async def _env_vars(self, params: dict) -> Path:
        import os
        for k, v in (params.get("vars") or {}).items():
            os.environ[str(k)] = str(v)
        return self.sandbox

    async def _composite(self, params: dict) -> Path:
        project = self.sandbox / "project"
        project.mkdir(parents=True, exist_ok=True)

        for part in params.get("parts", []):
            spec = FixtureSpec(**part)
            result = await self.apply(spec)
            if result != self.sandbox:
                project = result

        return project

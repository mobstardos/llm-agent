"""Sandbox: изолированная директория для прогона сценария."""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)


class Sandbox:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        self.path = self.base_dir / self.run_id
        self.path.mkdir(parents=True, exist_ok=True)

    def cleanup(self, keep: bool = True) -> None:
        if keep:
            logger.info("Sandbox сохранён: %s", self.path)
            return
        try:
            shutil.rmtree(self.path)
            logger.info("Sandbox удалён: %s", self.path)
        except Exception as e:
            logger.warning("Не удалить sandbox: %s", e)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        pass

"""File-watcher с debounce, cooldown и откатом при поломке YAML."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

logger = logging.getLogger(__name__)

IGNORE_PATTERNS = ("*.swp", "*.tmp", "*~", ".DS_Store", ".#*", "*.bak", "*.pyc")
DEBOUNCE_SECONDS = 0.5
COOLDOWN_SECONDS = 5.0


class _Handler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def _emit(self, event):
        if event.is_directory:
            return
        path = Path(event.src_path)
        for pat in IGNORE_PATTERNS:
            if path.match(pat):
                return
        self.callback(path)

    def on_modified(self, event):
        self._emit(event)

    def on_created(self, event):
        self._emit(event)

    def on_moved(self, event):
        self._emit(event)


class FileWatcher:
    """Следит за декларациями, дебаунсит, откатывает при ошибке."""

    def __init__(self, registry, base_dir: Path):
        self.registry = registry
        self.base_dir = base_dir
        self._observer: Observer | None = None
        self._pending: dict[str, float] = {}
        self._cooldowns: dict[str, float] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._enabled = False

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._enabled:
            return
        self._loop = loop
        handler = _Handler(self._schedule)

        try:
            self._observer = Observer()
            for sub in ("agents", "mcp_servers", "capabilities", "loops", "config"):
                d = self.base_dir / sub
                if d.exists():
                    self._observer.schedule(handler, str(d), recursive=True)
            self._observer.start()
            self._enabled = True
            logger.info("File-watcher запущен на %s", self.base_dir)
        except Exception as e:
            logger.warning("File-watcher не запустился: %s", e)
            self._observer = None
            return

        self._task = loop.create_task(self._debounce_loop())

    def stop(self) -> None:
        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=3)
            except Exception:
                pass
        if self._task:
            self._task.cancel()
        self._enabled = False

    def _schedule(self, path: Path) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._mark_pending, path)

    def _mark_pending(self, path: Path) -> None:
        key = str(path)
        now = time.monotonic()
        last = self._cooldowns.get(key, 0)
        if now - last < COOLDOWN_SECONDS:
            return
        self._pending[key] = now + DEBOUNCE_SECONDS

    async def _debounce_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.2)
                now = time.monotonic()
                ready = [p for p, t in self._pending.items() if t <= now]
                for p in ready:
                    self._pending.pop(p, None)
                    self._cooldowns[p] = time.monotonic()
                    await self._handle_change(Path(p))
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception("Debounce loop error: %s", e)

    async def _handle_change(self, path: Path) -> None:
        try:
            rel = path.relative_to(self.base_dir)
        except ValueError:
            rel = path
        logger.info("Декларация изменилась: %s", rel)

        try:
            await self.registry.reload_all()
            logger.info("Reload успешен после изменения %s", rel)
        except Exception as e:
            logger.exception("Reload упал: %s — откатываю", e)
            await self._rollback()

    async def _rollback(self) -> None:
        try:
            await self.registry.load_declarations()
            await self.registry.build_snapshot(reason="watcher_rollback")
        except Exception:
            logger.exception("Откат тоже упал")

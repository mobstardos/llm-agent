"""File-watcher с debounce, cooldown, ожиданием стабильности файла и откатом при поломке YAML.

Проблема, которую закрывает стабильность: редактор или ИИ-агент пишет
управляемую декларацию (agents/, mcp_servers/, config/ и т.п.) — watchdog
срабатывает на каждый чанк записи. Если прочитать файл посреди записи,
получаем обрезанный YAML → reload падает → ложный откат всей конфигурации.
На Windows хуже: открытый дескриптор редактора даёт sharing violation.

Поэтому перед reload ждём, пока (а) дескриптор освободился (файл читается)
и (б) размер+mtime не меняются в течение STABILITY_WINDOW.
"""
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
STABILITY_WINDOW_SECONDS = 0.3
STABILITY_MAX_WAIT_SECONDS = 5.0
STABILITY_MAX_RETRIES = 3


class _Handler(FileSystemEventHandler):
    def __init__(self, callback):
        self.callback = callback

    def _emit(self, event):
        if event.is_directory:
            return
        # Атомарное сохранение редакторами (write tmp → rename): реальный
        # путь — dest_path, src_path указывает на временный файл
        raw = getattr(event, "dest_path", "") or event.src_path
        path = Path(raw)
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
    """Следит за декларациями, дебаунсит, ждёт стабильности, откатывает при ошибке."""

    def __init__(
        self,
        registry,
        base_dir: Path,
        debounce_seconds: float = DEBOUNCE_SECONDS,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        stability_window: float = STABILITY_WINDOW_SECONDS,
        stability_max_wait: float = STABILITY_MAX_WAIT_SECONDS,
        stability_max_retries: int = STABILITY_MAX_RETRIES,
    ):
        self.registry = registry
        self.base_dir = base_dir
        self._observer: Observer | None = None
        self._pending: dict[str, float] = {}
        self._cooldowns: dict[str, float] = {}
        self._attempts: dict[str, int] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._enabled = False
        self._debounce_seconds = debounce_seconds
        self._cooldown_seconds = cooldown_seconds
        self._stability_window = stability_window
        self._stability_max_wait = stability_max_wait
        self._stability_max_retries = stability_max_retries

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
        if now - last < self._cooldown_seconds:
            return
        self._pending[key] = now + self._debounce_seconds

    # ═══════════════════════════════════════════════════════
    # Стабильность файла
    # ═══════════════════════════════════════════════════════

    @staticmethod
    def _probe(path: Path) -> tuple[bool, int, float] | None:
        """Проба файла: (читается ли, размер, mtime). None — файл исчез.

        Windows: открытый редактором дескриптор даёт PermissionError на
        открытие — это «ещё пишется» (False), а не «файла нет» (None).
        """
        try:
            st = path.stat()
            with open(path, "rb") as f:
                f.read(1)
        except (FileNotFoundError, IsADirectoryError):
            return None
        except OSError:
            return (False, -1, -1.0)
        return (True, st.st_size, st.st_mtime)

    async def _wait_until_stable(self, path: Path) -> bool:
        """Ждёт, пока файл перестанет меняться и дескриптор освободится.

        False — файл исчез (событие устарело) или так и не стабилизировался
        за stability_max_wait: в обоих случаях reload делать нельзя.
        """
        deadline = time.monotonic() + self._stability_max_wait
        poll = max(0.05, self._stability_window / 3)
        last: tuple[bool, int, float] | None = None
        since_same = 0.0
        while time.monotonic() < deadline:
            probe = self._probe(path)
            if probe is None:
                return False
            if probe == last and probe[0]:
                since_same += poll
                if since_same >= self._stability_window:
                    return True
            else:
                since_same = 0.0
            last = probe
            await asyncio.sleep(poll)
        return False

    # ═══════════════════════════════════════════════════════
    # Debounce loop
    # ═══════════════════════════════════════════════════════

    async def _debounce_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.2)
                now = time.monotonic()
                ready = [p for p, t in self._pending.items() if t <= now]
                for p in ready:
                    if not await self._wait_until_stable(Path(p)):
                        attempts = self._attempts.get(p, 0) + 1
                        self._pending.pop(p, None)
                        if attempts < self._stability_max_retries:
                            # Ещё пишется — пробуем снова после debounce
                            self._attempts[p] = attempts
                            self._pending[p] = (
                                time.monotonic() + self._debounce_seconds
                            )
                            logger.info(
                                "Файл ещё пишется, откладываю reload: %s "
                                "(проба %d/%d)",
                                p, attempts, self._stability_max_retries - 1,
                            )
                        else:
                            self._attempts.pop(p, None)
                            logger.warning(
                                "Файл не стабилизировался за %.1fс — "
                                "событие пропущено: %s",
                                self._stability_max_wait, p,
                            )
                        continue
                    self._pending.pop(p, None)
                    self._attempts.pop(p, None)
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
            # load_declarations — синхронный метод; прежний
            # «await self.registry.load_declarations()» поднимал
            # TypeError (await None) и откат никогда не выполнялся
            self.registry.load_declarations()
            await self.registry.build_snapshot(reason="watcher_rollback")
        except Exception:
            logger.exception("Откат тоже упал")

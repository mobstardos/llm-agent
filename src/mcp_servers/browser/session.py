"""Управление Playwright-сессией: browser + contexts + pages."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PageState:
    page: Any
    url: str = ""
    title: str = ""
    created_at: float = field(default_factory=time.time)
    console_logs: list[dict] = field(default_factory=list)
    network_requests: list[dict] = field(default_factory=list)


class BrowserSession:
    """Управляет одним Chromium-контекстом со множеством страниц.

    Ленивая инициализация: браузер запускается при первом использовании.
    """

    def __init__(
        self,
        headless: bool = True,
        browser_type: str = "chromium",
        timeout_ms: int = 30000,
        viewport: tuple[int, int] = (1280, 800),
    ):
        self.headless = headless
        self.browser_type = browser_type
        self.timeout_ms = timeout_ms
        self.viewport = viewport

        self._playwright = None
        self._browser = None
        self._context = None
        self._pages: dict[str, PageState] = {}
        self._active_page_id: str | None = None
        self._lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════
    # Init / shutdown
    # ═══════════════════════════════════════════════════════
    async def _ensure_started(self) -> None:
        if self._browser is not None:
            return
        async with self._lock:
            if self._browser is not None:
                return
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()

            launcher = {
                "chromium": self._playwright.chromium,
                "firefox": self._playwright.firefox,
                "webkit": self._playwright.webkit,
            }.get(self.browser_type)

            if launcher is None:
                raise RuntimeError(
                    f"Неизвестный тип браузера: {self.browser_type}"
                )

            self._browser = await launcher.launch(headless=self.headless)
            self._context = await self._browser.new_context(
                viewport={"width": self.viewport[0],
                          "height": self.viewport[1]},
                ignore_https_errors=True,
            )
            logger.info(
                "Browser started: %s headless=%s",
                self.browser_type, self.headless,
            )

    async def shutdown(self) -> None:
        try:
            if self._context:
                await self._context.close()
            if self._browser:
                await self._browser.close()
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning("Browser shutdown: %s", e)
        finally:
            self._browser = None
            self._context = None
            self._playwright = None
            self._pages.clear()

    # ═══════════════════════════════════════════════════════
    # Pages
    # ═══════════════════════════════════════════════════════
    async def new_page(self, url: str = "about:blank") -> str:
        await self._ensure_started()
        page = await self._context.new_page()
        page.set_default_timeout(self.timeout_ms)

        page_id = f"page_{int(time.time() * 1000)}"
        state = PageState(page=page)
        self._pages[page_id] = state

        # Собираем console + network
        page.on("console", lambda msg: state.console_logs.append({
            "type": msg.type,
            "text": msg.text,
            "ts": time.time(),
        }))
        page.on("pageerror", lambda err: state.console_logs.append({
            "type": "error", "text": str(err), "ts": time.time(),
        }))
        page.on("response", lambda resp: state.network_requests.append({
            "url": resp.url,
            "status": resp.status,
            "method": resp.request.method,
            "ts": time.time(),
        }))

        if url and url != "about:blank":
            try:
                await page.goto(url, wait_until="domcontentloaded")
                state.url = page.url
                state.title = await page.title()
            except Exception as e:
                logger.warning("new_page navigate: %s", e)

        self._active_page_id = page_id
        return page_id

    async def get_active_page(self) -> tuple[str, PageState]:
        await self._ensure_started()
        if self._active_page_id is None or \
                self._active_page_id not in self._pages:
            page_id = await self.new_page()
            return page_id, self._pages[page_id]
        return self._active_page_id, self._pages[self._active_page_id]

    async def close_page(self, page_id: str | None = None) -> None:
        pid = page_id or self._active_page_id
        if pid and pid in self._pages:
            try:
                await self._pages[pid].page.close()
            except Exception:
                pass
            self._pages.pop(pid, None)
            if self._active_page_id == pid:
                self._active_page_id = (
                    list(self._pages.keys())[0] if self._pages else None
                )

    def list_pages(self) -> list[dict]:
        return [
            {
                "id": pid,
                "url": st.url,
                "title": st.title,
                "created_at": st.created_at,
                "active": pid == self._active_page_id,
            }
            for pid, st in self._pages.items()
        ]

    async def select_page(self, page_id: str) -> None:
        if page_id in self._pages:
            self._active_page_id = page_id
        else:
            raise ValueError(f"Страница не найдена: {page_id}")

"""MCP-сервер: браузер через Playwright."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("browser-mcp")

_session = None


def _get_session():
    global _session
    if _session is None:
        from src.mcp_servers.browser.session import BrowserSession
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() == "true"
        browser = os.getenv("BROWSER_TYPE", "chromium")
        _session = BrowserSession(
            headless=headless, browser_type=browser,
        )
    return _session


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _safe_output(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


app = Server("browser")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="navigate",
            description="Открыть URL (создаёт новую страницу).",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "wait_until": {"type": "string", "default": "domcontentloaded"},
                    "timeout_ms": {"type": "integer", "default": 30000},
                },
                "required": ["url"],
            },
        ),
        Tool(name="new_page",
             description="Создать новую вкладку.",
             inputSchema={
                 "type": "object",
                 "properties": {"url": {"type": "string", "default": "about:blank"}},
             }),
        Tool(name="close_page",
             description="Закрыть вкладку.",
             inputSchema={
                 "type": "object",
                 "properties": {"page_id": {"type": "string"}},
             }),
        Tool(name="list_pages",
             description="Список открытых вкладок.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="select_page",
             description="Переключиться на вкладку.",
             inputSchema={
                 "type": "object",
                 "properties": {"page_id": {"type": "string"}},
                 "required": ["page_id"],
             }),
        Tool(name="click",
             description="Клик по селектору.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "timeout_ms": {"type": "integer"},
                 },
                 "required": ["selector"],
             }),
        Tool(name="fill",
             description="Заполнить поле ввода.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "value": {"type": "string"},
                 },
                 "required": ["selector", "value"],
             }),
        Tool(name="select_option",
             description="Выбрать значение в <select>.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "value": {"type": "string"},
                 },
                 "required": ["selector", "value"],
             }),
        Tool(name="press_key",
             description="Нажать клавишу (Enter, Tab, Escape...).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "key": {"type": "string"},
                     "selector": {"type": "string"},
                 },
                 "required": ["key"],
             }),
        Tool(name="hover",
             description="Наведение на элемент.",
             inputSchema={
                 "type": "object",
                 "properties": {"selector": {"type": "string"}},
                 "required": ["selector"],
             }),
        Tool(name="wait_for_selector",
             description="Ждать появления элемента.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "state": {"type": "string", "default": "visible"},
                     "timeout_ms": {"type": "integer", "default": 30000},
                 },
                 "required": ["selector"],
             }),
        Tool(name="wait_for_load_state",
             description="Ждать состояние загрузки (load, networkidle).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "state": {"type": "string", "default": "load"},
                     "timeout_ms": {"type": "integer", "default": 30000},
                 },
             }),
        Tool(name="screenshot",
             description="Скриншот страницы (сохраняется в файл).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "full_page": {"type": "boolean", "default": False},
                 },
             }),
        Tool(name="screenshot_element",
             description="Скриншот элемента.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "path": {"type": "string"},
                 },
                 "required": ["selector"],
             }),
        Tool(name="get_text",
             description="Получить текст элемента.",
             inputSchema={
                 "type": "object",
                 "properties": {"selector": {"type": "string"}},
                 "required": ["selector"],
             }),
        Tool(name="get_html",
             description="Получить HTML элемента или всей страницы.",
             inputSchema={
                 "type": "object",
                 "properties": {"selector": {"type": "string"}},
             }),
        Tool(name="get_attribute",
             description="Получить атрибут элемента.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "selector": {"type": "string"},
                     "name": {"type": "string"},
                 },
                 "required": ["selector", "name"],
             }),
        Tool(name="get_console_logs",
             description="Console-логи страницы.",
             inputSchema={
                 "type": "object",
                 "properties": {"clear": {"type": "boolean", "default": False}},
             }),
        Tool(name="get_network_requests",
             description="Сетевые запросы страницы.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "filter_url": {"type": "string"},
                     "only_errors": {"type": "boolean", "default": False},
                 },
             }),
        Tool(name="evaluate",
             description="Выполнить JS в контексте страницы (осторожно!).",
             inputSchema={
                 "type": "object",
                 "properties": {"script": {"type": "string"}},
                 "required": ["script"],
             }),
        Tool(name="go_back",
             description="Назад.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="go_forward",
             description="Вперёд.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="reload",
             description="Перезагрузить страницу.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_url",
             description="Текущий URL.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="get_title",
             description="Заголовок страницы.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="snapshot_visual",
             description="Сохранить visual snapshot для regression.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "name": {"type": "string"},
                     "full_page": {"type": "boolean", "default": True},
                 },
                 "required": ["name"],
             }),
        Tool(name="compare_visual",
             description="Сравнить с baseline snapshot.",
             inputSchema={
                 "type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        s = _get_session()

        # ─── Pages management ──────────────────────
        if name == "navigate":
            page_id = await s.new_page()
            _, state = await s.get_active_page()
            page = state.page

            try:
                resp = await page.goto(
                    arguments["url"],
                    wait_until=arguments.get("wait_until", "domcontentloaded"),
                    timeout=arguments.get("timeout_ms", 30000),
                )
                state.url = page.url
                state.title = await page.title()
                status = resp.status if resp else 0
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "page_id": page_id,
                        "url": state.url,
                        "title": state.title,
                        "status": status,
                    }, ensure_ascii=False, indent=2),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]

        if name == "new_page":
            pid = await s.new_page(arguments.get("url", "about:blank"))
            return [TextContent(type="text", text=f"page_id={pid}")]

        if name == "close_page":
            await s.close_page(arguments.get("page_id"))
            return [TextContent(type="text", text="Закрыто")]

        if name == "list_pages":
            return [TextContent(
                type="text",
                text=json.dumps(s.list_pages(), ensure_ascii=False, indent=2),
            )]

        if name == "select_page":
            await s.select_page(arguments["page_id"])
            return [TextContent(type="text", text="OK")]

        # ─── Interaction ──────────────────────────
        _, state = await s.get_active_page()
        page = state.page

        if name == "click":
            await page.click(
                arguments["selector"],
                timeout=arguments.get("timeout_ms"),
            )
            return [TextContent(type="text", text="OK")]

        if name == "fill":
            await page.fill(arguments["selector"], arguments["value"])
            return [TextContent(type="text", text="OK")]

        if name == "select_option":
            await page.select_option(
                arguments["selector"], arguments["value"],
            )
            return [TextContent(type="text", text="OK")]

        if name == "press_key":
            if arguments.get("selector"):
                await page.press(arguments["selector"], arguments["key"])
            else:
                await page.keyboard.press(arguments["key"])
            return [TextContent(type="text", text="OK")]

        if name == "hover":
            await page.hover(arguments["selector"])
            return [TextContent(type="text", text="OK")]

        if name == "wait_for_selector":
            await page.wait_for_selector(
                arguments["selector"],
                state=arguments.get("state", "visible"),
                timeout=arguments.get("timeout_ms", 30000),
            )
            return [TextContent(type="text", text="OK")]

        if name == "wait_for_load_state":
            await page.wait_for_load_state(
                arguments.get("state", "load"),
                timeout=arguments.get("timeout_ms", 30000),
            )
            return [TextContent(type="text", text="OK")]

        # ─── Screenshots ──────────────────────────
        if name == "screenshot":
            out = arguments.get("path")
            out_path = _safe_output(out) if out else (
                _get_root() / "screenshots" / f"page_{int(asyncio.get_event_loop().time() * 1000)}.png"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(
                path=str(out_path),
                full_page=arguments.get("full_page", False),
            )
            return [TextContent(type="text", text=f"Сохранено: {out_path}")]

        if name == "screenshot_element":
            out = arguments.get("path")
            out_path = _safe_output(out) if out else (
                _get_root() / "screenshots" / f"element_{int(asyncio.get_event_loop().time() * 1000)}.png"
            )
            out_path.parent.mkdir(parents=True, exist_ok=True)
            el = await page.query_selector(arguments["selector"])
            if not el:
                return [TextContent(
                    type="text",
                    text=f"Элемент не найден: {arguments['selector']}",
                )]
            await el.screenshot(path=str(out_path))
            return [TextContent(type="text", text=f"Сохранено: {out_path}")]

        # ─── Inspection ───────────────────────────
        if name == "get_text":
            text = await page.text_content(arguments["selector"])
            return [TextContent(type="text", text=text or "(пусто)")]

        if name == "get_html":
            if arguments.get("selector"):
                html = await page.inner_html(arguments["selector"])
            else:
                html = await page.content()
            return [TextContent(type="text", text=html[:50000])]

        if name == "get_attribute":
            val = await page.get_attribute(
                arguments["selector"], arguments["name"],
            )
            return [TextContent(type="text", text=val or "(нет)")]

        if name == "get_console_logs":
            logs = list(state.console_logs)
            if arguments.get("clear"):
                state.console_logs.clear()
            return [TextContent(
                type="text",
                text=json.dumps(logs[-200:], ensure_ascii=False, indent=2),
            )]

        if name == "get_network_requests":
            reqs = list(state.network_requests)
            if arguments.get("filter_url"):
                reqs = [r for r in reqs if arguments["filter_url"] in r["url"]]
            if arguments.get("only_errors"):
                reqs = [r for r in reqs if r.get("status", 0) >= 400]
            return [TextContent(
                type="text",
                text=json.dumps(reqs[-200:], ensure_ascii=False, indent=2),
            )]

        if name == "evaluate":
            result = await page.evaluate(arguments["script"])
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, default=str),
            )]

        # ─── Navigation ───────────────────────────
        if name == "go_back":
            await page.go_back()
            state.url = page.url
            return [TextContent(type="text", text=state.url)]

        if name == "go_forward":
            await page.go_forward()
            state.url = page.url
            return [TextContent(type="text", text=state.url)]

        if name == "reload":
            await page.reload()
            return [TextContent(type="text", text="OK")]

        if name == "get_url":
            return [TextContent(type="text", text=page.url)]

        if name == "get_title":
            return [TextContent(type="text", text=await page.title())]

        # ─── Visual regression ────────────────────
        if name == "snapshot_visual":
            name_ = arguments["name"]
            root = _get_root()
            snap_dir = root / ".visual_baselines"
            snap_dir.mkdir(parents=True, exist_ok=True)
            out = snap_dir / f"{name_}.png"
            await page.screenshot(
                path=str(out),
                full_page=arguments.get("full_page", True),
            )
            return [TextContent(type="text", text=f"Baseline: {out}")]

        if name == "compare_visual":
            from PIL import Image
            import numpy as np

            name_ = arguments["name"]
            root = _get_root()
            baseline = root / ".visual_baselines" / f"{name_}.png"
            if not baseline.exists():
                return [TextContent(
                    type="text",
                    text=f"Baseline не найден: {baseline}",
                )]

            current = root / "screenshots" / f"visual_{name_}.png"
            current.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(current), full_page=True)

            with Image.open(baseline) as ba, Image.open(current) as cu:
                ba2 = ba.convert("RGB").resize((1024, 768))
                cu2 = cu.convert("RGB").resize((1024, 768))
                arr_a = np.asarray(ba2, dtype=float)
                arr_b = np.asarray(cu2, dtype=float)

            diff = np.abs(arr_a - arr_b)
            result = {
                "name": name_,
                "mean_diff": round(float(diff.mean()), 3),
                "max_diff": round(float(diff.max()), 1),
                "percent_changed": round(float((diff > 10).mean() * 100), 2),
                "identical": float(diff.mean()) < 1.0,
                "baseline": str(baseline),
                "current": str(current),
            }
            try:
                from skimage.metrics import structural_similarity
                ssim = float(structural_similarity(
                    arr_a.astype(np.uint8),
                    arr_b.astype(np.uint8),
                    channel_axis=2,
                ))
                result["ssim"] = round(ssim, 4)
            except ImportError:
                pass
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Browser tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

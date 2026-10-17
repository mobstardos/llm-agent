"""MCP-сервер: HTTP-клиент."""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("http-mcp")

DEFAULT_TIMEOUT = 30.0

app = Server("http")


def _jsonpath_get(data: Any, path: str) -> Any:
    """Простой JSONPath-подобный get: a.b.c[0].d"""
    if not path:
        return data
    parts = path.replace("[", ".").replace("]", "").split(".")
    cur = data
    for p in parts:
        if not p:
            continue
        if isinstance(cur, dict):
            cur = cur.get(p)
        elif isinstance(cur, list):
            try:
                cur = cur[int(p)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


async def _request(
    method: str, url: str,
    headers: dict | None = None,
    params: dict | None = None,
    body: Any = None,
    json_body: Any = None,
    timeout: float = DEFAULT_TIMEOUT,
    follow_redirects: bool = True,
) -> dict:
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=follow_redirects,
            verify=False,
        ) as client:
            kwargs: dict[str, Any] = {
                "headers": headers or {},
                "params": params or {},
            }
            if json_body is not None:
                kwargs["json"] = json_body
            elif body is not None:
                if isinstance(body, str):
                    kwargs["content"] = body.encode("utf-8")
                else:
                    kwargs["content"] = body

            resp = await client.request(method.upper(), url, **kwargs)
            duration_ms = (time.perf_counter() - t0) * 1000

            # Body
            text = resp.text
            try:
                json_data = resp.json()
            except Exception:
                json_data = None

            return {
                "status": resp.status_code,
                "headers": dict(resp.headers),
                "body": text[:50000],
                "json": json_data,
                "duration_ms": round(duration_ms, 1),
                "url": str(resp.url),
            }
    except httpx.TimeoutException:
        return {
            "error": "timeout",
            "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
        }
    except httpx.ConnectError as e:
        return {"error": f"connect_error: {e}"}
    except Exception as e:
        return {"error": str(e)}


@app.list_tools()
async def list_tools() -> list[Tool]:
    headers_schema = {
        "type": "object",
        "additionalProperties": {"type": "string"},
    }
    return [
        Tool(name="http_request",
             description="Универсальный HTTP-запрос.",
             inputSchema={"type": "object", "properties": {
                 "method": {"type": "string"},
                 "url": {"type": "string"},
                 "headers": headers_schema,
                 "params": {"type": "object"},
                 "json_body": {"type": "object"},
                 "body": {"type": "string"},
                 "timeout": {"type": "number", "default": 30}},
                 "required": ["method", "url"]}),
        Tool(name="http_get",
             description="GET-запрос.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "headers": headers_schema,
                 "params": {"type": "object"}},
                 "required": ["url"]}),
        Tool(name="http_post",
             description="POST-запрос.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "json_body": {"type": "object"},
                 "body": {"type": "string"},
                 "headers": headers_schema},
                 "required": ["url"]}),
        Tool(name="http_put",
             description="PUT-запрос.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "json_body": {"type": "object"},
                 "headers": headers_schema},
                 "required": ["url"]}),
        Tool(name="http_patch",
             description="PATCH-запрос.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "json_body": {"type": "object"},
                 "headers": headers_schema},
                 "required": ["url"]}),
        Tool(name="http_delete",
             description="DELETE-запрос.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "headers": headers_schema},
                 "required": ["url"]}),
        Tool(name="http_assert_status",
             description="Проверить HTTP-статус URL.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "expected": {"type": "integer"},
                 "method": {"type": "string", "default": "GET"}},
                 "required": ["url", "expected"]}),
        Tool(name="http_assert_json",
             description="Проверить значение JSON по jsonpath.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "jsonpath": {"type": "string"},
                 "expected": {},
                 "method": {"type": "string", "default": "GET"}},
                 "required": ["url", "jsonpath", "expected"]}),
        Tool(name="graphql_query",
             description="GraphQL-запрос.",
             inputSchema={"type": "object", "properties": {
                 "endpoint": {"type": "string"},
                 "query": {"type": "string"},
                 "variables": {"type": "object"},
                 "headers": headers_schema},
                 "required": ["endpoint", "query"]}),
        Tool(name="websocket_send_receive",
             description="Отправить сообщение в WebSocket и получить ответ.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "message": {"type": "string"},
                 "wait_seconds": {"type": "number", "default": 5}},
                 "required": ["url", "message"]}),
        Tool(name="load_test",
             description="Простой нагрузочный тест: N запросов, M параллельных.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "method": {"type": "string", "default": "GET"},
                 "requests": {"type": "integer", "default": 100},
                 "concurrency": {"type": "integer", "default": 10}},
                 "required": ["url"]}),
        Tool(name="sse_read",
             description="Прочитать SSE-поток (N секунд).",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "duration": {"type": "number", "default": 10},
                 "max_events": {"type": "integer", "default": 100}},
                 "required": ["url"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # ─── Universal ──────────────────────────────
        if name == "http_request":
            result = await _request(
                method=arguments["method"],
                url=arguments["url"],
                headers=arguments.get("headers"),
                params=arguments.get("params"),
                body=arguments.get("body"),
                json_body=arguments.get("json_body"),
                timeout=arguments.get("timeout", DEFAULT_TIMEOUT),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2,
                                default=str)[:50000],
            )]

        # ─── Shorthands ─────────────────────────────
        if name in ("http_get", "http_post", "http_put",
                    "http_patch", "http_delete"):
            method = name.replace("http_", "").upper()
            result = await _request(
                method=method,
                url=arguments["url"],
                headers=arguments.get("headers"),
                params=arguments.get("params"),
                json_body=arguments.get("json_body"),
                body=arguments.get("body"),
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2,
                                default=str)[:50000],
            )]

        # ─── Assertions ─────────────────────────────
        if name == "http_assert_status":
            result = await _request(
                method=arguments.get("method", "GET"),
                url=arguments["url"],
            )
            if "error" in result:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "passed": False, "error": result["error"],
                    }, ensure_ascii=False, indent=2),
                )]
            passed = result["status"] == arguments["expected"]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "passed": passed,
                    "expected": arguments["expected"],
                    "actual": result["status"],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "http_assert_json":
            result = await _request(
                method=arguments.get("method", "GET"),
                url=arguments["url"],
            )
            if "error" in result or result.get("json") is None:
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "passed": False,
                        "error": result.get("error", "no json"),
                    }, ensure_ascii=False, indent=2),
                )]
            actual = _jsonpath_get(result["json"], arguments["jsonpath"])
            passed = actual == arguments["expected"]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "passed": passed,
                    "jsonpath": arguments["jsonpath"],
                    "expected": arguments["expected"],
                    "actual": actual,
                }, ensure_ascii=False, indent=2, default=str),
            )]

        # ─── GraphQL ────────────────────────────────
        if name == "graphql_query":
            headers = {
                "Content-Type": "application/json",
                **(arguments.get("headers") or {}),
            }
            result = await _request(
                method="POST",
                url=arguments["endpoint"],
                headers=headers,
                json_body={
                    "query": arguments["query"],
                    "variables": arguments.get("variables") or {},
                },
            )
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2,
                                default=str)[:50000],
            )]

        # ─── WebSocket ──────────────────────────────
        if name == "websocket_send_receive":
            try:
                import websockets
            except ImportError:
                return [TextContent(
                    type="text",
                    text="websockets не установлен: pip install websockets",
                )]
            url = arguments["url"]
            msg = arguments["message"]
            wait = arguments.get("wait_seconds", 5)

            received: list[str] = []
            try:
                async with websockets.connect(url) as ws:
                    await ws.send(msg)
                    try:
                        while True:
                            data = await asyncio.wait_for(ws.recv(), timeout=wait)
                            received.append(str(data)[:1000])
                            if len(received) >= 20:
                                break
                    except asyncio.TimeoutError:
                        pass
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)}, ensure_ascii=False),
                )]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "sent": msg,
                    "received_count": len(received),
                    "messages": received,
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Load test ──────────────────────────────
        if name == "load_test":
            url = arguments["url"]
            method = arguments.get("method", "GET").upper()
            total = arguments.get("requests", 100)
            concurrency = arguments.get("concurrency", 10)

            sem = asyncio.Semaphore(concurrency)
            times: list[float] = []
            statuses: dict[int, int] = {}
            errors = 0

            async def one_request():
                nonlocal errors
                async with sem:
                    t0 = time.perf_counter()
                    try:
                        async with httpx.AsyncClient(
                            timeout=DEFAULT_TIMEOUT, verify=False,
                        ) as client:
                            resp = await client.request(method, url)
                        times.append((time.perf_counter() - t0) * 1000)
                        statuses[resp.status_code] = \
                            statuses.get(resp.status_code, 0) + 1
                    except Exception:
                        errors += 1

            t_start = time.perf_counter()
            tasks = [asyncio.create_task(one_request()) for _ in range(total)]
            await asyncio.gather(*tasks, return_exceptions=True)
            total_duration = (time.perf_counter() - t_start) * 1000

            times.sort()
            def pct(p):
                if not times:
                    return 0
                idx = min(int(len(times) * p), len(times) - 1)
                return round(times[idx], 1)

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total_requests": total,
                    "errors": errors,
                    "statuses": statuses,
                    "duration_ms": round(total_duration, 1),
                    "rps": round(total / (total_duration / 1000), 1)
                            if total_duration > 0 else 0,
                    "latency_p50": pct(0.5),
                    "latency_p95": pct(0.95),
                    "latency_p99": pct(0.99),
                }, ensure_ascii=False, indent=2),
            )]

        # ─── SSE ────────────────────────────────────
        if name == "sse_read":
            url = arguments["url"]
            duration = arguments.get("duration", 10)
            max_events = arguments.get("max_events", 100)

            events: list[str] = []
            t0 = time.perf_counter()
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(duration + 1, connect=10),
                    verify=False,
                ) as client:
                    async with client.stream("GET", url) as resp:
                        if resp.status_code != 200:
                            return [TextContent(
                                type="text",
                                text=f"HTTP {resp.status_code}",
                            )]
                        async for line in resp.aiter_lines():
                            if time.perf_counter() - t0 > duration:
                                break
                            if line.strip():
                                events.append(line[:1000])
                            if len(events) >= max_events:
                                break
            except Exception as e:
                return [TextContent(
                    type="text",
                    text=json.dumps({"error": str(e)}, ensure_ascii=False),
                )]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "events_count": len(events),
                    "events": events,
                }, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("HTTP tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

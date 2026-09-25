"""MCP-сервер: DeepSeek через веб-cookies."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("deepseek-mcp")

DEEPSEEK_BASE = "https://chat.deepseek.com"
THUMBCACHE_NAME = ".thumbcache_6b2e5483f9d858d7c661c5e276b6a6ae"

DS_SESSION_ID = os.getenv("DEEPSEEK_DS_SESSION_ID", "").strip()
SMIDV2 = os.getenv("DEEPSEEK_SMIDV2", "").strip()
THUMBCACHE = os.getenv("DEEPSEEK_THUMBCACHE", "").strip()
AUTH_TOKEN = os.getenv("DEEPSEEK_AUTH_TOKEN", "").strip()

app = Server("deepseek")


def _headers() -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "ru-RU,ru;q=0.9,en;q=0.8",
        "content-type": "application/json",
        "origin": DEEPSEEK_BASE,
        "referer": f"{DEEPSEEK_BASE}/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "x-app-version": "20241129.1",
        "x-client-platform": "web",
        "x-client-version": "1.0.0-always",
    }
    cookies: list[str] = []
    if DS_SESSION_ID:
        cookies.append(f"ds_session_id={DS_SESSION_ID}")
    if SMIDV2:
        cookies.append(f"smidV2={SMIDV2}")
    if THUMBCACHE:
        cookies.append(f"{THUMBCACHE_NAME}={THUMBCACHE}")
    if cookies:
        headers["cookie"] = "; ".join(cookies)
    token = AUTH_TOKEN or SMIDV2
    if token:
        headers["authorization"] = f"Bearer {token}"
    return headers


async def _create_session(client: httpx.AsyncClient) -> str:
    r = await client.post(
        f"{DEEPSEEK_BASE}/api/v0/chat_session/create",
        headers=_headers(), json={},
    )
    if r.status_code >= 400:
        raise RuntimeError(
            f"DeepSeek HTTP {r.status_code}: {r.text[:500]}"
        )
    data = r.json()
    if data.get("code") not in (0, None):
        raise RuntimeError(f"DeepSeek API error: {data}")
    biz = data.get("data", {}).get("biz_data", {})
    sid = biz.get("chat_session", {}).get("id")
    if not sid:
        raise RuntimeError(f"Не удалось получить session_id: {data}")
    return sid


def _extract_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if not isinstance(obj, dict):
        return ""
    for key in ("content", "text"):
        v = obj.get(key)
        if isinstance(v, str) and v:
            return v
    v = obj.get("v")
    if isinstance(v, dict):
        for key in ("response", "content", "text", "delta"):
            val = v.get(key)
            if isinstance(val, str) and val:
                return val
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        delta = choices[0].get("delta", {})
        if isinstance(delta, dict):
            c = delta.get("content")
            if isinstance(c, str) and c:
                return c
    return ""


async def _completion(
    client: httpx.AsyncClient, session_id: str,
    prompt: str, thinking: bool = False,
) -> str:
    body = {
        "chat_session_id": session_id,
        "parent_message_id": None,
        "prompt": prompt,
        "ref_file_ids": [],
        "thinking_enabled": bool(thinking),
        "search_enabled": False,
    }
    chunks: list[str] = []
    async with client.stream(
        "POST",
        f"{DEEPSEEK_BASE}/api/v0/chat/completion",
        headers=_headers(), json=body, timeout=180.0,
    ) as resp:
        if resp.status_code >= 400:
            err = await resp.aread()
            raise RuntimeError(
                f"DeepSeek HTTP {resp.status_code}: "
                f"{err.decode('utf-8', 'replace')[:500]}"
            )
        async for line in resp.aiter_lines():
            if not line.startswith("data: "):
                continue
            payload = line[6:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            piece = _extract_text(obj)
            if piece:
                chunks.append(piece)
    return "".join(chunks).strip()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="deepseek_chat",
            description="Быстрый запрос к DeepSeek (chat).",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "system": {"type": "string"},
                    "thinking": {"type": "boolean", "default": False},
                },
                "required": ["prompt"],
            },
        ),
        Tool(
            name="deepseek_reasoner",
            description="Запрос к DeepSeek с рассуждениями (R1).",
            inputSchema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "system": {"type": "string"},
                },
                "required": ["prompt"],
            },
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name not in ("deepseek_chat", "deepseek_reasoner"):
        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]

    prompt = (arguments.get("prompt") or "").strip()
    system = (arguments.get("system") or "").strip()
    thinking = name == "deepseek_reasoner" or bool(arguments.get("thinking"))

    if not prompt:
        return [TextContent(type="text", text="Пустой prompt")]
    if not (AUTH_TOKEN or SMIDV2 or DS_SESSION_ID):
        return [TextContent(
            type="text",
            text="DeepSeek не настроен: задайте DEEPSEEK_SMIDV2.",
        )]
    if system:
        prompt = f"{system}\n\n---\n\n{prompt}"

    try:
        async with httpx.AsyncClient(
            timeout=180.0, follow_redirects=True,
        ) as client:
            sid = await _create_session(client)
            answer = await _completion(client, sid, prompt, thinking)
        if not answer:
            return [TextContent(type="text", text="(пустой ответ)")]
        return [TextContent(type="text", text=answer)]
    except Exception as e:
        logger.exception("DeepSeek call failed")
        return [TextContent(type="text", text=f"Ошибка DeepSeek: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

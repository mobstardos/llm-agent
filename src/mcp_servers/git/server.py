"""MCP-сервер: Git-операции."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("git")


def _root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


async def _git(*args, cwd: Path | None = None, timeout: int = 30) -> str:
    cwd = cwd or _root()
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return "(timeout)"
        text = stdout.decode("utf-8", errors="replace").strip()
        return text or "(пусто)"
    except FileNotFoundError:
        return "git не найден"


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="status", description="git status",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="diff", description="git diff",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "staged": {"type": "boolean", "default": False},
                     "path": {"type": "string"},
                 },
             }),
        Tool(name="log", description="git log",
             inputSchema={
                 "type": "object",
                 "properties": {"limit": {"type": "integer", "default": 20}},
             }),
        Tool(name="branch_list", description="git branch",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="branch_create",
             description="git checkout -b",
             inputSchema={
                 "type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"],
             }),
        Tool(name="checkout",
             description="git checkout <ref>",
             inputSchema={
                 "type": "object",
                 "properties": {"ref": {"type": "string"}},
                 "required": ["ref"],
             }),
        Tool(name="add",
             description="git add",
             inputSchema={
                 "type": "object",
                 "properties": {"paths": {"type": "array",
                                          "items": {"type": "string"}}},
                 "required": ["paths"],
             }),
        Tool(name="commit",
             description="git commit -m <message>",
             inputSchema={
                 "type": "object",
                 "properties": {"message": {"type": "string"}},
                 "required": ["message"],
             }),
        Tool(name="reset",
             description="git reset --<mode> <ref>",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "mode": {"type": "string", "default": "mixed"},
                     "ref": {"type": "string", "default": "HEAD~1"},
                 },
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "status":
        return [TextContent(type="text", text=await _git("status"))]
    if name == "diff":
        args = ["diff"]
        if arguments.get("staged"):
            args.append("--cached")
        if arguments.get("path"):
            args.extend(["--", arguments["path"]])
        return [TextContent(type="text", text=await _git(*args))]
    if name == "log":
        limit = arguments.get("limit", 20)
        return [TextContent(
            type="text",
            text=await _git("log", f"-n{limit}", "--oneline"),
        )]
    if name == "branch_list":
        return [TextContent(type="text", text=await _git("branch", "-a"))]
    if name == "branch_create":
        return [TextContent(
            type="text",
            text=await _git("checkout", "-b", arguments["name"]),
        )]
    if name == "checkout":
        return [TextContent(
            type="text", text=await _git("checkout", arguments["ref"]),
        )]
    if name == "add":
        return [TextContent(
            type="text", text=await _git("add", *arguments["paths"]),
        )]
    if name == "commit":
        return [TextContent(
            type="text",
            text=await _git("commit", "-m", arguments["message"]),
        )]
    if name == "reset":
        mode = arguments.get("mode", "mixed")
        ref = arguments.get("ref", "HEAD~1")
        return [TextContent(
            type="text",
            text=await _git("reset", f"--{mode}", ref),
        )]
    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

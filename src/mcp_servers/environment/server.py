"""MCP-сервер: environment (venv, nvm, pyenv, версии)."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("environment-mcp")


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


async def _run(cmd, cwd=None, timeout=300):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd or _get_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


app = Server("environment")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="env_info",
             description="Информация о доступных менеджерах окружений.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="versions_all",
             description="Версии всех инструментов (python, node, go, rust, java).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="python_venv_list",
             description="Список venv-директорий в проекте.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="python_venv_create",
             description="Создать venv.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": ".venv"},
                 "python": {"type": "string", "default": "python"}},
                 "required": []}),
        Tool(name="python_venv_info",
             description="Информация о venv.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": ".venv"}}}),
        Tool(name="pyenv_versions",
             description="Установленные версии Python через pyenv.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="pyenv_install",
             description="Установить версию Python через pyenv.",
             inputSchema={"type": "object", "properties": {
                 "version": {"type": "string"}},
                 "required": ["version"]}),
        Tool(name="pyenv_local",
             description="Установить локальную версию Python.",
             inputSchema={"type": "object", "properties": {
                 "version": {"type": "string"}},
                 "required": ["version"]}),
        Tool(name="nvm_list",
             description="Установленные версии Node через nvm.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="nvm_install",
             description="Установить версию Node через nvm.",
             inputSchema={"type": "object", "properties": {
                 "version": {"type": "string"}},
                 "required": ["version"]}),
        Tool(name="nvm_use",
             description="Переключиться на версию Node.",
             inputSchema={"type": "object", "properties": {
                 "version": {"type": "string"}},
                 "required": ["version"]}),
        Tool(name="node_versions",
             description="Информация о Node.js.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="go_versions",
             description="Информация о Go.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="rust_versions",
             description="Информация о Rust.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    root = _get_root()
    try:
        if name == "env_info":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "python": shutil.which("python") or shutil.which("python3"),
                    "pip": shutil.which("pip") or shutil.which("pip3"),
                    "node": shutil.which("node"),
                    "npm": shutil.which("npm"),
                    "nvm": shutil.which("nvm") or shutil.which("nvm.sh"),
                    "pyenv": shutil.which("pyenv"),
                    "go": shutil.which("go"),
                    "cargo": shutil.which("cargo"),
                    "rustup": shutil.which("rustup"),
                    "java": shutil.which("java"),
                    "mvn": shutil.which("mvn"),
                    "gradle": shutil.which("gradle"),
                }, ensure_ascii=False, indent=2),
            )]

        if name == "versions_all":
            checks = [
                (["python", "--version"], "python"),
                (["pip", "--version"], "pip"),
                (["node", "--version"], "node"),
                (["npm", "--version"], "npm"),
                (["go", "version"], "go"),
                (["cargo", "--version"], "cargo"),
                (["rustc", "--version"], "rustc"),
                (["java", "-version"], "java"),
                (["git", "--version"], "git"),
                (["docker", "--version"], "docker"),
                (["pyenv", "--version"], "pyenv"),
            ]
            result = {}
            for cmd, label in checks:
                if not shutil.which(cmd[0]):
                    result[label] = "(не установлен)"
                    continue
                rc, out = await _run(cmd, timeout=10)
                result[label] = out.strip().splitlines()[0][:100] if out else "?"
            return [TextContent(
                type="text", text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        if name == "python_venv_list":
            found = []
            for p in root.rglob("pyvenv.cfg"):
                venv_dir = p.parent
                if any(part in {".git", "node_modules", "target"} for part in venv_dir.parts):
                    continue
                try:
                    found.append(str(venv_dir.relative_to(root)))
                except ValueError:
                    found.append(str(venv_dir))
            return [TextContent(
                type="text", text=json.dumps(found, ensure_ascii=False, indent=2),
            )]

        if name == "python_venv_create":
            path = _safe(arguments.get("path", ".venv"))
            python_bin = arguments.get("python", "python")
            if not shutil.which(python_bin):
                return [TextContent(type="text", text=f"{python_bin} не найден")]
            rc, out = await _run(
                [python_bin, "-m", "venv", str(path)], timeout=120,
            )
            return [TextContent(
                type="text",
                text=json.dumps({"exit_code": rc, "path": str(path),
                                 "output": out[-2000:]}, ensure_ascii=False),
            )]

        if name == "python_venv_info":
            path = _safe(arguments.get("path", ".venv"))
            cfg = path / "pyvenv.cfg"
            if not cfg.exists():
                return [TextContent(type="text", text="venv не найден")]
            info = {}
            for line in cfg.read_text(encoding="utf-8").splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    info[k.strip()] = v.strip()
            return [TextContent(
                type="text", text=json.dumps(info, ensure_ascii=False, indent=2),
            )]

        if name == "pyenv_versions":
            if not shutil.which("pyenv"):
                return [TextContent(type="text", text="pyenv не установлен")]
            rc, out = await _run(["pyenv", "versions"], timeout=10)
            return [TextContent(type="text", text=out or "(пусто)")]

        if name == "pyenv_install":
            if not shutil.which("pyenv"):
                return [TextContent(type="text", text="pyenv не установлен")]
            v = arguments["version"]
            rc, out = await _run(["pyenv", "install", v], timeout=1800)
            return [TextContent(
                type="text",
                text=json.dumps({"exit_code": rc, "output": out[-3000:]},
                                ensure_ascii=False),
            )]

        if name == "pyenv_local":
            if not shutil.which("pyenv"):
                return [TextContent(type="text", text="pyenv не установлен")]
            v = arguments["version"]
            rc, out = await _run(["pyenv", "local", v], timeout=30)
            return [TextContent(
                type="text",
                text=json.dumps({"exit_code": rc, "output": out[-1000:]},
                                ensure_ascii=False),
            )]

        if name == "nvm_list":
            # nvm — bash-функция, работает через . nvm.sh
            nvm_sh = os.path.expanduser("~/.nvm/nvm.sh")
            if not os.path.exists(nvm_sh):
                return [TextContent(
                    type="text", text="nvm не найден (~/.nvm/nvm.sh)",
                )]
            rc, out = await _run(
                ["bash", "-c", f". {nvm_sh} && nvm ls"], timeout=30,
            )
            return [TextContent(type="text", text=out)]

        if name == "nvm_install":
            nvm_sh = os.path.expanduser("~/.nvm/nvm.sh")
            if not os.path.exists(nvm_sh):
                return [TextContent(type="text", text="nvm не найден")]
            v = arguments["version"]
            rc, out = await _run(
                ["bash", "-c", f". {nvm_sh} && nvm install {v}"], timeout=600,
            )
            return [TextContent(
                type="text",
                text=json.dumps({"exit_code": rc, "output": out[-3000:]},
                                ensure_ascii=False),
            )]

        if name == "nvm_use":
            nvm_sh = os.path.expanduser("~/.nvm/nvm.sh")
            if not os.path.exists(nvm_sh):
                return [TextContent(type="text", text="nvm не найден")]
            v = arguments["version"]
            rc, out = await _run(
                ["bash", "-c", f". {nvm_sh} && nvm use {v}"], timeout=30,
            )
            return [TextContent(
                type="text",
                text=json.dumps({"exit_code": rc, "output": out[-1000:]},
                                ensure_ascii=False),
            )]

        if name == "node_versions":
            result = {}
            for label, cmd in (
                ("node", ["node", "--version"]),
                ("npm", ["npm", "--version"]),
                ("npx", ["npx", "--version"]),
            ):
                if not shutil.which(cmd[0]):
                    result[label] = "(не установлен)"
                else:
                    rc, out = await _run(cmd, timeout=10)
                    result[label] = out.strip()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "go_versions":
            result = {}
            if shutil.which("go"):
                rc, out = await _run(["go", "version"], timeout=10)
                result["go"] = out.strip()
                rc, out = await _run(["go", "env", "GOPATH"], timeout=10)
                result["GOPATH"] = out.strip()
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "rust_versions":
            result = {}
            for label, cmd in (
                ("rustc", ["rustc", "--version"]),
                ("cargo", ["cargo", "--version"]),
                ("rustup", ["rustup", "--version"]),
            ):
                if shutil.which(cmd[0]):
                    rc, out = await _run(cmd, timeout=10)
                    result[label] = out.strip().splitlines()[0]
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("environment tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

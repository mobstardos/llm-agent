"""MCP-сервер: build & package."""
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
logger = logging.getLogger("build-mcp")


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


async def _run(cmd, cwd=None, timeout=1800):
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


def _detect_project(root: Path) -> dict:
    result = {
        "types": [],
        "build_commands": {},
        "pkg_managers": [],
        "has_dockerfile": (root / "Dockerfile").exists(),
        "has_compose": (root / "docker-compose.yml").exists()
                or (root / "docker-compose.yaml").exists(),
    }

    if (root / "package.json").exists():
        result["types"].append("node")
        result["pkg_managers"].append("npm")
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            for s in scripts:
                result["build_commands"][s] = f"npm run {s}"
        except Exception:
            pass
        if (root / "yarn.lock").exists():
            result["pkg_managers"] = ["yarn"]
        elif (root / "pnpm-lock.yaml").exists():
            result["pkg_managers"] = ["pnpm"]

    if (root / "pyproject.toml").exists() or (root / "setup.py").exists():
        result["types"].append("python")
        result["pkg_managers"].append("pip")
        result["build_commands"]["build"] = "python -m build"

    if (root / "Cargo.toml").exists():
        result["types"].append("rust")
        result["pkg_managers"].append("cargo")
        result["build_commands"]["build"] = "cargo build"
        result["build_commands"]["test"] = "cargo test"
        result["build_commands"]["release"] = "cargo build --release"

    if (root / "go.mod").exists():
        result["types"].append("go")
        result["pkg_managers"].append("go")
        result["build_commands"]["build"] = "go build ./..."
        result["build_commands"]["test"] = "go test ./..."

    if (root / "Makefile").exists():
        result["build_commands"]["make"] = "make"
        result["build_commands"]["make_clean"] = "make clean"

    return result


app = Server("build")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="detect_project",
             description="Определить тип проекта и команды сборки.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="build_run",
             description="Запустить сборку.",
             inputSchema={"type": "object", "properties": {
                 "target": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 900}}}),
        Tool(name="build_clean",
             description="Очистка артефактов сборки.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="pkg_list",
             description="Список зависимостей.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="pkg_add",
             description="Добавить зависимость.",
             inputSchema={"type": "object", "properties": {
                 "package": {"type": "string"},
                 "version": {"type": "string"},
                 "dev": {"type": "boolean", "default": False}},
                 "required": ["package"]}),
        Tool(name="pkg_remove",
             description="Удалить зависимость.",
             inputSchema={"type": "object", "properties": {
                 "package": {"type": "string"}},
                 "required": ["package"]}),
        Tool(name="pkg_update",
             description="Обновить зависимости.",
             inputSchema={"type": "object", "properties": {
                 "package": {"type": "string"}}}),
        Tool(name="pkg_outdated",
             description="Устаревшие пакеты.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="pkg_audit",
             description="Аудит уязвимостей зависимостей.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="docker_build",
             description="Собрать Docker-образ.",
             inputSchema={"type": "object", "properties": {
                 "tag": {"type": "string"},
                 "dockerfile": {"type": "string", "default": "Dockerfile"},
                 "context": {"type": "string", "default": "."}}}),
        Tool(name="docker_run",
             description="Запустить контейнер.",
             inputSchema={"type": "object", "properties": {
                 "image": {"type": "string"},
                 "name": {"type": "string"},
                 "ports": {"type": "array", "items": {"type": "string"}},
                 "env": {"type": "object"},
                 "detach": {"type": "boolean", "default": True}},
                 "required": ["image"]}),
        Tool(name="docker_stop",
             description="Остановить контейнер.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"}},
                 "required": ["name"]}),
        Tool(name="docker_list",
             description="Список контейнеров/образов.",
             inputSchema={"type": "object", "properties": {
                 "type": {"type": "string", "default": "containers"}}}),
        Tool(name="docker_logs",
             description="Логи контейнера.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "tail": {"type": "integer", "default": 100}},
                 "required": ["name"]}),
        Tool(name="docker_compose_up",
             description="Docker compose up.",
             inputSchema={"type": "object", "properties": {
                 "detach": {"type": "boolean", "default": True}}}),
        Tool(name="docker_compose_down",
             description="Docker compose down.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    root = _get_root()

    try:
        if name == "detect_project":
            return [TextContent(
                type="text",
                text=json.dumps(_detect_project(root), ensure_ascii=False, indent=2),
            )]

        # ─── Build ──────────────────────────────────
        if name == "build_run":
            target = arguments.get("target", "build")
            timeout = arguments.get("timeout_seconds", 900)

            cmd: list[str] = []
            if (root / "package.json").exists():
                cmd = ["npm", "run", target]
            elif (root / "Cargo.toml").exists():
                cmd = ["cargo", target]
            elif (root / "go.mod").exists():
                cmd = ["go", target]
            elif (root / "Makefile").exists():
                cmd = ["make", target]
            elif (root / "pyproject.toml").exists():
                cmd = ["python", "-m", "build"]
            else:
                return [TextContent(type="text", text="Команда не определена")]

            rc, out = await _run(cmd, timeout=timeout)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "command": " ".join(cmd),
                    "exit_code": rc,
                    "output": out[-5000:],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "build_clean":
            cleaned = 0
            for pattern in ("dist", "build", "target", "node_modules/.cache", "__pycache__", ".pytest_cache"):
                p = root / pattern
                if p.exists() and p.is_dir():
                    try:
                        shutil.rmtree(p)
                        cleaned += 1
                    except Exception:
                        pass
            return [TextContent(type="text", text=f"Очищено: {cleaned}")]

        # ─── Packages ───────────────────────────────
        if name == "pkg_list":
            if (root / "package.json").exists():
                cmd = ["npm", "list", "--depth=0"]
            elif (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
                cmd = ["python", "-m", "pip", "list"]
            elif (root / "Cargo.toml").exists():
                cmd = ["cargo", "tree", "--depth=1"]
            else:
                return [TextContent(type="text", text="Не определено")]
            rc, out = await _run(cmd, timeout=60)
            return [TextContent(type="text", text=out[:5000])]

        if name == "pkg_add":
            pkg = arguments["package"]
            version = arguments.get("version")
            is_dev = arguments.get("dev", False)

            if (root / "package.json").exists():
                spec = pkg + (f"@{version}" if version else "")
                cmd = ["npm", "install"] + (["-D"] if is_dev else []) + [spec]
            elif (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
                spec = pkg + (f"=={version}" if version else "")
                cmd = ["python", "-m", "pip", "install", spec]
            else:
                return [TextContent(type="text", text="Не определено")]

            rc, out = await _run(cmd, timeout=300)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "package": pkg,
                    "exit_code": rc,
                    "output": out[-2000:],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "pkg_remove":
            pkg = arguments["package"]
            if (root / "package.json").exists():
                cmd = ["npm", "uninstall", pkg]
            else:
                cmd = ["python", "-m", "pip", "uninstall", "-y", pkg]
            rc, out = await _run(cmd, timeout=120)
            return [TextContent(type="text", text=out[-2000:] or f"exit={rc}")]

        if name == "pkg_update":
            pkg = arguments.get("package")
            if (root / "package.json").exists():
                cmd = ["npm", "update"] + ([pkg] if pkg else [])
            else:
                cmd = ["python", "-m", "pip", "install", "--upgrade"] + ([pkg] if pkg else [])
            rc, out = await _run(cmd, timeout=600)
            return [TextContent(type="text", text=out[-3000:] or f"exit={rc}")]

        if name == "pkg_outdated":
            if (root / "package.json").exists():
                cmd = ["npm", "outdated"]
            else:
                cmd = ["python", "-m", "pip", "list", "--outdated"]
            rc, out = await _run(cmd, timeout=180)
            return [TextContent(type="text", text=out[:5000] or "Все актуально")]

        if name == "pkg_audit":
            if (root / "package.json").exists():
                cmd = ["npm", "audit", "--json"]
            else:
                # pip-audit если есть
                if shutil.which("pip-audit"):
                    cmd = ["pip-audit", "--format=json"]
                else:
                    return [TextContent(type="text", text="pip-audit не установлен")]
            rc, out = await _run(cmd, timeout=180)
            return [TextContent(type="text", text=out[:5000])]

        # ─── Docker ─────────────────────────────────
        if not shutil.which("docker"):
            return [TextContent(type="text", text="Docker не установлен")]

        if name == "docker_build":
            tag = arguments.get("tag", "llmagent-built")
            df = arguments.get("dockerfile", "Dockerfile")
            ctx = arguments.get("context", ".")
            cmd = ["docker", "build", "-t", tag, "-f", str(root / df), str(root / ctx)]
            rc, out = await _run(cmd, timeout=1800)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "tag": tag, "exit_code": rc, "output": out[-3000:],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "docker_run":
            cmd = ["docker", "run"]
            if arguments.get("detach", True):
                cmd.append("-d")
            if arguments.get("name"):
                cmd.extend(["--name", arguments["name"]])
            for p in arguments.get("ports", []):
                cmd.extend(["-p", p])
            for k, v in (arguments.get("env") or {}).items():
                cmd.extend(["-e", f"{k}={v}"])
            cmd.append(arguments["image"])
            rc, out = await _run(cmd, timeout=120)
            return [TextContent(type="text", text=out.strip() or f"exit={rc}")]

        if name == "docker_stop":
            rc, out = await _run(["docker", "stop", arguments["name"]], timeout=60)
            return [TextContent(type="text", text=out or "Остановлен")]

        if name == "docker_list":
            t = arguments.get("type", "containers")
            cmd = ["docker", "ps", "-a"] if t == "containers" else ["docker", "images"]
            rc, out = await _run(cmd, timeout=60)
            return [TextContent(type="text", text=out[:5000])]

        if name == "docker_logs":
            tail = arguments.get("tail", 100)
            rc, out = await _run(
                ["docker", "logs", "--tail", str(tail), arguments["name"]],
                timeout=60,
            )
            return [TextContent(type="text", text=out[-5000:])]

        if name == "docker_compose_up":
            cmd = ["docker", "compose", "up"]
            if arguments.get("detach", True):
                cmd.append("-d")
            rc, out = await _run(cmd, timeout=1800)
            return [TextContent(type="text", text=out[-3000:])]

        if name == "docker_compose_down":
            rc, out = await _run(["docker", "compose", "down"], timeout=300)
            return [TextContent(type="text", text=out[-3000:])]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Build tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

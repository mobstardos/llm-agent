"""MCP-сервер: security."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import string
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("security-mcp")

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", "target", "data",
}


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


async def _run(cmd, cwd=None, timeout=600):
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


# Паттерны для секретов
SECRET_PATTERNS = [
    ("AWS Access Key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("AWS Secret", re.compile(r"(?i)aws.{0,20}(secret|key).{0,20}['\"]([A-Za-z0-9/+=]{40})['\"]")),
    ("Google API", re.compile(r"AIza[0-9A-Za-z\-_]{35}")),
    ("GitHub Token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("Slack Token", re.compile(r"xox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("Private Key", re.compile(r"-----BEGIN (RSA |DSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")),
    ("Bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{20,}")),
    ("Generic API Key", re.compile(r"(?i)(api[_-]?key|apikey|secret[_-]?key)\s*[:=]\s*['\"]([A-Za-z0-9_\-]{20,})['\"]")),
    ("Password", re.compile(r"(?i)(password|passwd|pwd)\s*[:=]\s*['\"]([^'\"]{8,})['\"]")),
    ("OpenAI Key", re.compile(r"sk-[A-Za-z0-9]{32,}")),
    ("Anthropic Key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{40,}")),
    ("Telegram Bot", re.compile(r"\d{8,10}:[A-Za-z0-9_\-]{35}")),
]


app = Server("security")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="scan_secrets",
             description="Поиск утечек секретов в коде.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "max_results": {"type": "integer", "default": 200}}}),
        Tool(name="scan_sast",
             description="Статический анализ (bandit/semgrep).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "tool": {"type": "string", "default": "auto"}}}),
        Tool(name="scan_dependencies",
             description="Аудит зависимостей (pip-audit/npm audit/safety).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="scan_licenses",
             description="Лицензии зависимостей (pip-licenses).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="generate_sbom",
             description="Сгенерировать SBOM (CycloneDX).",
             inputSchema={"type": "object", "properties": {
                 "output": {"type": "string", "default": "sbom.json"}}}),
        Tool(name="hash_file",
             description="Хэш файла (md5/sha1/sha256/sha512).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "algorithms": {"type": "array", "items": {"type": "string"}}},
                 "required": ["path"]}),
        Tool(name="hash_string",
             description="Хэш строки.",
             inputSchema={"type": "object", "properties": {
                 "text": {"type": "string"},
                 "algorithm": {"type": "string", "default": "sha256"}},
                 "required": ["text"]}),
        Tool(name="verify_hash",
             description="Проверить, что hash файла совпадает.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "expected": {"type": "string"},
                 "algorithm": {"type": "string", "default": "sha256"}},
                 "required": ["path", "expected"]}),
        Tool(name="generate_password",
             description="Сгенерировать безопасный пароль.",
             inputSchema={"type": "object", "properties": {
                 "length": {"type": "integer", "default": 24},
                 "symbols": {"type": "boolean", "default": True}}}),
        Tool(name="generate_key",
             description="Сгенерировать ключ (hex/base64/urlsafe).",
             inputSchema={"type": "object", "properties": {
                 "bytes": {"type": "integer", "default": 32},
                 "format": {"type": "string", "default": "hex"}}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        root = _get_root()

        # ─── Secrets scan ───────────────────────────
        if name == "scan_secrets":
            base = _safe(arguments.get("path", "."))
            max_results = arguments.get("max_results", 200)

            # Игнорируем бинарные
            text_exts = {
                ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml",
                ".yml", ".toml", ".env", ".sh", ".bash", ".cfg", ".ini",
                ".xml", ".md", ".txt", ".sql", ".bsl", ".go", ".rs",
                ".java", ".rb", ".php", ".cs", ".kt",
            }

            findings: list[dict] = []
            for p in base.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in IGNORE_DIRS for part in p.parts):
                    continue
                if p.suffix.lower() not in text_exts and p.name not in (".env",):
                    continue
                # Пропускаем слишком большие
                try:
                    if p.stat().st_size > 2 * 1024 * 1024:
                        continue
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue

                for label, pattern in SECRET_PATTERNS:
                    for m in pattern.finditer(text):
                        line_num = text[:m.start()].count("\n") + 1
                        snippet = m.group(0)[:100]
                        # Маскируем значение
                        masked = snippet[:20] + "***" if len(snippet) > 20 else "***"
                        try:
                            rel = str(p.relative_to(root))
                        except ValueError:
                            rel = str(p)
                        findings.append({
                            "type": label,
                            "file": rel,
                            "line": line_num,
                            "masked": masked,
                        })
                        if len(findings) >= max_results:
                            break
                    if len(findings) >= max_results:
                        break
                if len(findings) >= max_results:
                    break

            return [TextContent(
                type="text",
                text=json.dumps({
                    "findings_count": len(findings),
                    "findings": findings[:100],
                }, ensure_ascii=False, indent=2),
            )]

        # ─── SAST ──────────────────────────────────
        if name == "scan_sast":
            tool = arguments.get("tool", "auto")
            base = _safe(arguments.get("path", "."))

            if tool in ("bandit", "auto") and shutil.which("bandit"):
                cmd = ["bandit", "-r", str(base), "-f", "json", "-q"]
            elif tool == "semgrep" and shutil.which("semgrep"):
                cmd = ["semgrep", "--config=auto", "--json", str(base)]
            else:
                # Простой fallback: ищем известные паттерны
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "note": "bandit/semgrep не установлен. "
                                "pip install bandit semgrep",
                    }, ensure_ascii=False),
                )]

            rc, out = await _run(cmd, timeout=600)
            return [TextContent(type="text", text=out[:20000])]

        # ─── Dependencies ──────────────────────────
        if name == "scan_dependencies":
            if (root / "package.json").exists():
                cmd = ["npm", "audit", "--json"]
            elif shutil.which("pip-audit"):
                cmd = ["pip-audit", "--format=json"]
            elif shutil.which("safety"):
                cmd = ["safety", "check", "--json"]
            else:
                return [TextContent(
                    type="text",
                    text="Установите: pip install pip-audit safety",
                )]
            rc, out = await _run(cmd, timeout=300)
            return [TextContent(type="text", text=out[:20000])]

        # ─── Licenses ──────────────────────────────
        if name == "scan_licenses":
            if shutil.which("pip-licenses"):
                rc, out = await _run(
                    ["pip-licenses", "--format=json"], timeout=120,
                )
                return [TextContent(type="text", text=out[:20000])]
            return [TextContent(
                type="text",
                text="pip install pip-licenses",
            )]

        # ─── SBOM ──────────────────────────────────
        if name == "generate_sbom":
            if shutil.which("cyclonedx-py"):
                output = _safe(arguments.get("output", "sbom.json"))
                cmd = ["cyclonedx-py", "environment", "-o", str(output)]
                rc, out = await _run(cmd, timeout=120)
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc,
                        "output": str(output),
                    }, ensure_ascii=False),
                )]
            return [TextContent(
                type="text",
                text="pip install cyclonedx-bom",
            )]

        # ─── Hashes ────────────────────────────────
        if name == "hash_file":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            algos = arguments.get("algorithms") or ["sha256"]
            result = {}
            for algo in algos:
                try:
                    h = hashlib.new(algo)
                except ValueError:
                    result[algo] = "не поддерживается"
                    continue
                with open(p, "rb") as f:
                    while chunk := f.read(65536):
                        h.update(chunk)
                result[algo] = h.hexdigest()
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        if name == "hash_string":
            text = arguments["text"]
            algo = arguments.get("algorithm", "sha256")
            try:
                h = hashlib.new(algo, text.encode("utf-8"))
            except ValueError:
                return [TextContent(type="text", text="Алгоритм не поддерживается")]
            return [TextContent(type="text", text=h.hexdigest())]

        if name == "verify_hash":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            algo = arguments.get("algorithm", "sha256")
            h = hashlib.new(algo)
            with open(p, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            actual = h.hexdigest()
            expected = arguments["expected"].lower()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "passed": actual.lower() == expected,
                    "expected": expected,
                    "actual": actual,
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Generators ────────────────────────────
        if name == "generate_password":
            length = arguments.get("length", 24)
            use_symbols = arguments.get("symbols", True)
            alphabet = string.ascii_letters + string.digits
            if use_symbols:
                alphabet += "!@#$%^&*()-_=+"
            password = "".join(secrets.choice(alphabet) for _ in range(length))
            return [TextContent(type="text", text=password)]

        if name == "generate_key":
            n = arguments.get("bytes", 32)
            fmt = arguments.get("format", "hex")
            key = secrets.token_bytes(n)
            if fmt == "hex":
                result = key.hex()
            elif fmt == "base64":
                import base64
                result = base64.b64encode(key).decode()
            elif fmt == "urlsafe":
                import base64
                result = base64.urlsafe_b64encode(key).decode()
            else:
                result = key.hex()
            return [TextContent(type="text", text=result)]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Security tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

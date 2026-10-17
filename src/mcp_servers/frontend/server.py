"""MCP-сервер: frontend."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("frontend-mcp")

IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
               ".idea", ".vscode", "dist", "build", "target"}


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


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


app = Server("frontend")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="fe_detect",
             description="Определить frontend-фреймворк.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="fe_scripts",
             description="npm-скрипты из package.json.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="fe_build",
             description="Запустить npm-скрипт (build/dev).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string", "default": "build"},
                 "timeout_seconds": {"type": "integer", "default": 900}}}),
        Tool(name="fe_bundle_size",
             description="Размер бандла (dist/build).",
             inputSchema={"type": "object", "properties": {
                 "dist_path": {"type": "string", "default": "dist"}}}),
        Tool(name="fe_dependencies_audit",
             description="Аудит зависимостей (npm audit).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="css_analyze",
             description="Анализ CSS-файла.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="css_lint",
             description="Lint CSS (без внешних линтеров).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="tailwind_config_info",
             description="Инфо о Tailwind конфиге.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="a11y_check_url",
             description="Базовая проверка доступности URL.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"}},
                 "required": ["url"]}),
        Tool(name="lighthouse",
             description="Lighthouse-аудит URL (требует lighthouse CLI).",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string"},
                 "categories": {"type": "array",
                                "items": {"type": "string"},
                                "default": ["performance"]}},
                 "required": ["url"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    root = _get_root()

    try:
        if name == "fe_detect":
            pkg_path = root / "package.json"
            if not pkg_path.exists():
                return [TextContent(type="text", text="package.json не найден")]

            try:
                pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            except Exception as e:
                return [TextContent(type="text", text=f"Parse error: {e}")]

            deps = {**pkg.get("dependencies", {}),
                    **pkg.get("devDependencies", {})}

            frameworks = []
            if "next" in deps:
                frameworks.append("Next.js")
            elif "react" in deps:
                frameworks.append("React")
            if "vue" in deps:
                frameworks.append("Vue")
            if "svelte" in deps:
                frameworks.append("Svelte")
            if "astro" in deps:
                frameworks.append("Astro")

            bundlers = []
            if "vite" in deps:
                bundlers.append("Vite")
            if "webpack" in deps:
                bundlers.append("Webpack")
            if "parcel" in deps:
                bundlers.append("Parcel")
            if "esbuild" in deps:
                bundlers.append("esbuild")

            css = []
            if "tailwindcss" in deps:
                css.append("Tailwind")
            if "sass" in deps:
                css.append("Sass")
            if "styled-components" in deps:
                css.append("styled-components")

            test = []
            if "vitest" in deps:
                test.append("Vitest")
            if "jest" in deps:
                test.append("Jest")
            if "playwright" in deps or "@playwright/test" in deps:
                test.append("Playwright")

            return [TextContent(
                type="text",
                text=_json_result({
                    "name": pkg.get("name", ""),
                    "version": pkg.get("version", ""),
                    "frameworks": frameworks,
                    "bundlers": bundlers,
                    "css": css,
                    "test": test,
                    "scripts_count": len(pkg.get("scripts", {})),
                    "dependencies_count": len(pkg.get("dependencies", {})),
                    "devDependencies_count": len(pkg.get("devDependencies", {})),
                }),
            )]

        if name == "fe_scripts":
            pkg_path = root / "package.json"
            if not pkg_path.exists():
                return [TextContent(type="text", text="package.json не найден")]
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            return [TextContent(
                type="text",
                text=_json_result(pkg.get("scripts", {})),
            )]

        if name == "fe_build":
            if not shutil.which("npm"):
                return [TextContent(type="text", text="npm не найден")]
            script = arguments.get("script", "build")
            rc, out = await _run(
                ["npm", "run", script],
                timeout=arguments.get("timeout_seconds", 900),
            )
            return [TextContent(
                type="text",
                text=_json_result({
                    "script": script,
                    "exit_code": rc,
                    "output": out[-5000:],
                }),
            )]

        if name == "fe_bundle_size":
            dist = arguments.get("dist_path", "dist")
            d = _safe(dist)
            if not d.exists():
                return [TextContent(type="text", text=f"{dist} не найден")]
            total = 0
            by_ext: dict[str, dict] = {}
            files: list[tuple[int, str]] = []
            for p in d.rglob("*"):
                if not p.is_file():
                    continue
                size = p.stat().st_size
                total += size
                ext = p.suffix.lower() or "(no ext)"
                by_ext.setdefault(ext, {"count": 0, "size": 0})
                by_ext[ext]["count"] += 1
                by_ext[ext]["size"] += size
                files.append((size, str(p.relative_to(root))))

            files.sort(reverse=True)
            return [TextContent(
                type="text",
                text=_json_result({
                    "total_bytes": total,
                    "total_kb": round(total / 1024, 1),
                    "total_mb": round(total / (1024 * 1024), 2),
                    "by_extension": by_ext,
                    "top_files": [
                        {"path": f, "size_kb": round(s / 1024, 1)}
                        for s, f in files[:20]
                    ],
                }),
            )]

        if name == "fe_dependencies_audit":
            if not (root / "package.json").exists():
                return [TextContent(type="text", text="package.json не найден")]
            rc, out = await _run(
                ["npm", "audit", "--json"], timeout=180,
            )
            return [TextContent(type="text", text=out[:20000])]

        if name == "css_analyze":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")

            selectors = len(re.findall(r"[^{}]+\{", text))
            declarations = text.count(";")
            media_queries = len(re.findall(r"@media\b", text))
            keyframes = len(re.findall(r"@keyframes\b", text))
            imports = len(re.findall(r"@import\b", text))
            variables = len(re.findall(r"--[\w-]+\s*:", text))

            return [TextContent(
                type="text",
                text=_json_result({
                    "file": str(p.relative_to(root)),
                    "lines": len(text.splitlines()),
                    "selectors": selectors,
                    "declarations": declarations,
                    "media_queries": media_queries,
                    "keyframes": keyframes,
                    "imports": imports,
                    "custom_properties": variables,
                }),
            )]

        if name == "css_lint":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            issues = []

            # Проверка баланса скобок
            if text.count("{") != text.count("}"):
                issues.append({
                    "type": "unbalanced_braces",
                    "message": f"{{: {text.count('{')} vs }}: {text.count('}')}",
                })

            # !important
            for i, line in enumerate(text.splitlines(), 1):
                if "!important" in line:
                    issues.append({
                        "type": "important",
                        "line": i,
                        "message": "Использование !important",
                    })

            # Пустые правила
            if re.search(r"\{\s*\}", text):
                issues.append({
                    "type": "empty_rule",
                    "message": "Найдены пустые правила",
                })

            return [TextContent(
                type="text",
                text=_json_result({
                    "total_issues": len(issues),
                    "issues": issues[:100],
                }),
            )]

        if name == "tailwind_config_info":
            for fname in ("tailwind.config.js", "tailwind.config.ts",
                          "tailwind.config.cjs", "tailwind.config.mjs"):
                f = root / fname
                if f.exists():
                    text = f.read_text(encoding="utf-8", errors="replace")
                    return [TextContent(
                        type="text",
                        text=_json_result({
                            "file": fname,
                            "size": len(text),
                            "has_content": "content" in text,
                            "has_theme": "theme" in text,
                            "has_plugins": "plugins" in text,
                        }),
                    )]
            return [TextContent(type="text", text="Tailwind не найден")]

        if name == "a11y_check_url":
            # Базовая проверка: HTML без внешнего движка
            url = arguments["url"]
            try:
                import httpx
                r = httpx.get(url, timeout=15, follow_redirects=True)
                html = r.text
            except Exception as e:
                return [TextContent(type="text", text=f"Fetch error: {e}")]

            issues = []

            # img без alt
            for m in re.finditer(r"<img\b[^>]*>", html, re.IGNORECASE):
                if "alt=" not in m.group(0).lower():
                    issues.append({
                        "type": "img_without_alt",
                        "snippet": m.group(0)[:100],
                    })

            # input без label (упрощённо)
            inputs = len(re.findall(r"<input\b", html, re.IGNORECASE))
            labels = len(re.findall(r"<label\b", html, re.IGNORECASE))
            if inputs > labels:
                issues.append({
                    "type": "inputs_more_than_labels",
                    "message": f"input: {inputs}, label: {labels}",
                })

            # html lang
            if not re.search(r'<html[^>]*\blang=', html, re.IGNORECASE):
                issues.append({"type": "html_missing_lang"})

            # Title
            if not re.search(r"<title\b", html, re.IGNORECASE):
                issues.append({"type": "missing_title"})

            return [TextContent(
                type="text",
                text=_json_result({
                    "url": url,
                    "issues_count": len(issues),
                    "issues": issues[:50],
                    "note": "Базовая проверка. Для полного — axe-core.",
                }),
            )]

        if name == "lighthouse":
            if not shutil.which("lighthouse"):
                return [TextContent(
                    type="text",
                    text="lighthouse не установлен: npm install -g lighthouse",
                )]
            url = arguments["url"]
            cats = ",".join(arguments.get("categories", ["performance"]))
            rc, out = await _run([
                "lighthouse", url,
                "--output=json", "--output-path=stdout",
                f"--only-categories={cats}",
                "--chrome-flags=--headless --no-sandbox",
                "--quiet",
            ], timeout=180)
            # Парсим только scores
            try:
                data = json.loads(out)
                scores = {
                    k: round(v.get("score", 0) * 100, 1)
                    for k, v in (data.get("categories") or {}).items()
                }
                return [TextContent(
                    type="text", text=_json_result(scores),
                )]
            except Exception:
                return [TextContent(
                    type="text", text=out[:5000],
                )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("frontend tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

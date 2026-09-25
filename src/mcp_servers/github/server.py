"""MCP-сервер: GitHub."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("github-mcp")

API_BASE = "https://api.github.com"


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


def _token() -> str:
    return os.getenv("GITHUB_TOKEN", "").strip()


def _repo() -> str:
    """owner/repo из env или из git remote."""
    r = os.getenv("GITHUB_REPO", "").strip()
    if r:
        return r

    # Из git remote
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=str(_get_root()),
            capture_output=True, text=True, timeout=5,
        )
        url = result.stdout.strip()
        if "github.com" in url:
            # git@github.com:owner/repo.git
            # https://github.com/owner/repo.git
            if url.startswith("git@"):
                path = url.split(":")[1]
            else:
                path = url.split("github.com/")[1]
            path = path.rstrip(".git").strip("/")
            return path
    except Exception:
        pass
    return ""


def _headers() -> dict:
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = _token()
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _api(method: str, path: str, **kwargs) -> dict:
    if not _token():
        return {"error": "GITHUB_TOKEN не задан в .env"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, f"{API_BASE}{path}", headers=_headers(), **kwargs,
            )
            try:
                data = resp.json()
            except Exception:
                data = {"text": resp.text[:1000]}
            return {
                "status": resp.status_code,
                "data": data,
            }
    except Exception as e:
        return {"error": str(e)}


app = Server("github")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="gh_status",
             description="Проверить GITHUB_TOKEN и repo.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="gh_repo_info",
             description="Информация о репозитории.",
             inputSchema={"type": "object", "properties": {
                 "repo": {"type": "string"}}}),
        Tool(name="gh_list_prs",
             description="Список PR.",
             inputSchema={"type": "object", "properties": {
                 "state": {"type": "string", "default": "open"},
                 "limit": {"type": "integer", "default": 20},
                 "repo": {"type": "string"}}}),
        Tool(name="gh_get_pr",
             description="Информация о PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_pr_files",
             description="Файлы, изменённые в PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_pr_diff",
             description="Diff PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_create_pr",
             description="Создать PR.",
             inputSchema={"type": "object", "properties": {
                 "title": {"type": "string"},
                 "head": {"type": "string"},
                 "base": {"type": "string", "default": "main"},
                 "body": {"type": "string"},
                 "draft": {"type": "boolean", "default": False},
                 "repo": {"type": "string"}},
                 "required": ["title", "head"]}),
        Tool(name="gh_comment_pr",
             description="Оставить комментарий в PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "body": {"type": "string"},
                 "repo": {"type": "string"}},
                 "required": ["number", "body"]}),
        Tool(name="gh_review_pr",
             description="Review PR (APPROVE/REQUEST_CHANGES/COMMENT).",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "decision": {"type": "string", "default": "APPROVE"},
                 "body": {"type": "string"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_merge_pr",
             description="Слить PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "method": {"type": "string", "default": "merge"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_close_pr",
             description="Закрыть PR.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_list_issues",
             description="Список issues.",
             inputSchema={"type": "object", "properties": {
                 "state": {"type": "string", "default": "open"},
                 "labels": {"type": "string"},
                 "limit": {"type": "integer", "default": 20},
                 "repo": {"type": "string"}}}),
        Tool(name="gh_get_issue",
             description="Информация об issue.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_create_issue",
             description="Создать issue.",
             inputSchema={"type": "object", "properties": {
                 "title": {"type": "string"},
                 "body": {"type": "string"},
                 "labels": {"type": "array", "items": {"type": "string"}},
                 "assignees": {"type": "array", "items": {"type": "string"}},
                 "repo": {"type": "string"}},
                 "required": ["title"]}),
        Tool(name="gh_comment_issue",
             description="Комментарий в issue.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "body": {"type": "string"},
                 "repo": {"type": "string"}},
                 "required": ["number", "body"]}),
        Tool(name="gh_close_issue",
             description="Закрыть issue.",
             inputSchema={"type": "object", "properties": {
                 "number": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["number"]}),
        Tool(name="gh_list_workflows",
             description="Список workflows.",
             inputSchema={"type": "object", "properties": {
                 "repo": {"type": "string"}}}),
        Tool(name="gh_list_runs",
             description="Запуски workflow.",
             inputSchema={"type": "object", "properties": {
                 "workflow": {"type": "string"},
                 "status": {"type": "string"},
                 "limit": {"type": "integer", "default": 20},
                 "repo": {"type": "string"}}}),
        Tool(name="gh_run_logs",
             description="Логи запуска workflow.",
             inputSchema={"type": "object", "properties": {
                 "run_id": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["run_id"]}),
        Tool(name="gh_rerun",
             description="Перезапустить workflow.",
             inputSchema={"type": "object", "properties": {
                 "run_id": {"type": "integer"},
                 "repo": {"type": "string"}},
                 "required": ["run_id"]}),
        Tool(name="gh_create_release",
             description="Создать релиз.",
             inputSchema={"type": "object", "properties": {
                 "tag": {"type": "string"},
                 "name": {"type": "string"},
                 "body": {"type": "string"},
                 "draft": {"type": "boolean", "default": False},
                 "prerelease": {"type": "boolean", "default": False},
                 "repo": {"type": "string"}},
                 "required": ["tag"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "gh_status":
            r = _repo()
            t = _token()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "token_set": bool(t),
                    "token_preview": t[:8] + "..." if t else None,
                    "repo": r or "(не определён)",
                }, ensure_ascii=False, indent=2),
            )]

        repo = arguments.get("repo") or _repo()
        if not repo and name != "gh_status":
            return [TextContent(
                type="text",
                text="repo не определён. Установите GITHUB_REPO в .env "
                     "или запустите из директории с git remote github.com.",
            )]

        # ─── Repo ──────────────────────────────────
        if name == "gh_repo_info":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}"),
                    ensure_ascii=False, indent=2,
                )[:20000],
            )]

        # ─── PRs ───────────────────────────────────
        if name == "gh_list_prs":
            state = arguments.get("state", "open")
            limit = arguments.get("limit", 20)
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}/pulls",
                               params={"state": state, "per_page": limit}),
                    ensure_ascii=False, indent=2,
                )[:30000],
            )]

        if name == "gh_get_pr":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}/pulls/{arguments['number']}"),
                    ensure_ascii=False, indent=2,
                )[:30000],
            )]

        if name == "gh_pr_files":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET",
                               f"/repos/{repo}/pulls/{arguments['number']}/files"),
                    ensure_ascii=False, indent=2,
                )[:30000],
            )]

        if name == "gh_pr_diff":
            if not _token():
                return [TextContent(type="text", text="GITHUB_TOKEN не задан")]
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{API_BASE}/repos/{repo}/pulls/{arguments['number']}",
                    headers={**_headers(),
                             "Accept": "application/vnd.github.v3.diff"},
                )
            return [TextContent(type="text", text=resp.text[:50000])]

        if name == "gh_create_pr":
            body = {
                "title": arguments["title"],
                "head": arguments["head"],
                "base": arguments.get("base", "main"),
                "body": arguments.get("body", ""),
                "draft": arguments.get("draft", False),
            }
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("POST", f"/repos/{repo}/pulls", json=body),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_comment_pr":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "POST",
                        f"/repos/{repo}/issues/{arguments['number']}/comments",
                        json={"body": arguments["body"]},
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_review_pr":
            body = {
                "event": arguments.get("decision", "APPROVE"),
                "body": arguments.get("body", ""),
            }
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "POST",
                        f"/repos/{repo}/pulls/{arguments['number']}/reviews",
                        json=body,
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_merge_pr":
            method = arguments.get("method", "merge")
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "PUT",
                        f"/repos/{repo}/pulls/{arguments['number']}/merge",
                        json={"merge_method": method},
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_close_pr":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "PATCH",
                        f"/repos/{repo}/pulls/{arguments['number']}",
                        json={"state": "closed"},
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        # ─── Issues ────────────────────────────────
        if name == "gh_list_issues":
            params = {
                "state": arguments.get("state", "open"),
                "per_page": arguments.get("limit", 20),
            }
            if arguments.get("labels"):
                params["labels"] = arguments["labels"]
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}/issues", params=params),
                    ensure_ascii=False, indent=2,
                )[:30000],
            )]

        if name == "gh_get_issue":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}/issues/{arguments['number']}"),
                    ensure_ascii=False, indent=2,
                )[:20000],
            )]

        if name == "gh_create_issue":
            body = {
                "title": arguments["title"],
                "body": arguments.get("body", ""),
            }
            if arguments.get("labels"):
                body["labels"] = arguments["labels"]
            if arguments.get("assignees"):
                body["assignees"] = arguments["assignees"]
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("POST", f"/repos/{repo}/issues", json=body),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_comment_issue":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "POST",
                        f"/repos/{repo}/issues/{arguments['number']}/comments",
                        json={"body": arguments["body"]},
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        if name == "gh_close_issue":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "PATCH",
                        f"/repos/{repo}/issues/{arguments['number']}",
                        json={"state": "closed"},
                    ),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        # ─── Actions ───────────────────────────────
        if name == "gh_list_workflows":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", f"/repos/{repo}/actions/workflows"),
                    ensure_ascii=False, indent=2,
                )[:20000],
            )]

        if name == "gh_list_runs":
            params = {"per_page": arguments.get("limit", 20)}
            if arguments.get("status"):
                params["status"] = arguments["status"]
            path = f"/repos/{repo}/actions/runs"
            wf = arguments.get("workflow")
            if wf:
                path = f"/repos/{repo}/actions/workflows/{wf}/runs"
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("GET", path, params=params),
                    ensure_ascii=False, indent=2,
                )[:30000],
            )]

        if name == "gh_run_logs":
            if not _token():
                return [TextContent(type="text", text="GITHUB_TOKEN не задан")]
            async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
                resp = await client.get(
                    f"{API_BASE}/repos/{repo}/actions/runs/{arguments['run_id']}/logs",
                    headers=_headers(),
                )
            # Обычно возвращается zip — отдаём как есть
            return [TextContent(
                type="text",
                text=json.dumps({
                    "status": resp.status_code,
                    "content_type": resp.headers.get("content-type", ""),
                    "size": len(resp.content),
                    "hint": "Логи — архив. Скачайте через UI.",
                }, ensure_ascii=False, indent=2),
            )]

        if name == "gh_rerun":
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api(
                        "POST",
                        f"/repos/{repo}/actions/runs/{arguments['run_id']}/rerun",
                    ),
                    ensure_ascii=False, indent=2,
                )[:5000],
            )]

        if name == "gh_create_release":
            body = {
                "tag_name": arguments["tag"],
                "name": arguments.get("name", arguments["tag"]),
                "body": arguments.get("body", ""),
                "draft": arguments.get("draft", False),
                "prerelease": arguments.get("prerelease", False),
            }
            return [TextContent(
                type="text",
                text=json.dumps(
                    await _api("POST", f"/repos/{repo}/releases", json=body),
                    ensure_ascii=False, indent=2,
                )[:10000],
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("GitHub tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

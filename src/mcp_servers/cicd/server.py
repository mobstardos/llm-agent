"""MCP-сервер: CI/CD (GitLab, Jenkins)."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import sys
from urllib.parse import quote

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("cicd-mcp")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


app = Server("cicd")


# ═════════════════════════════════════════════════════════
# GitLab
# ═════════════════════════════════════════════════════════
async def _gitlab_request(method: str, path: str, **kwargs) -> dict:
    url = os.getenv("GITLAB_URL", "").rstrip("/")
    token = os.getenv("GITLAB_TOKEN", "").strip()
    if not url or not token:
        return {"error": "GITLAB_URL или GITLAB_TOKEN не заданы"}

    headers = {"PRIVATE-TOKEN": token}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method, f"{url}/api/v4{path}", headers=headers, **kwargs,
            )
            try:
                return {"status": resp.status_code, "data": resp.json()}
            except Exception:
                return {"status": resp.status_code, "text": resp.text[:5000]}
    except Exception as e:
        return {"error": str(e)}


def _gitlab_project_id() -> str:
    return os.getenv("GITLAB_PROJECT", "").strip()


# ═════════════════════════════════════════════════════════
# Jenkins
# ═════════════════════════════════════════════════════════
async def _jenkins_request(method: str, path: str, **kwargs) -> dict:
    url = os.getenv("JENKINS_URL", "").rstrip("/")
    user = os.getenv("JENKINS_USER", "")
    token = os.getenv("JENKINS_TOKEN", "").strip()
    if not url or not token:
        return {"error": "JENKINS_URL или JENKINS_TOKEN не заданы"}

    auth = (user, token) if user else None
    try:
        async with httpx.AsyncClient(timeout=30.0, auth=auth) as client:
            resp = await client.request(method, f"{url}{path}", **kwargs)
            try:
                return {"status": resp.status_code, "data": resp.json()}
            except Exception:
                return {"status": resp.status_code, "text": resp.text[:5000]}
    except Exception as e:
        return {"error": str(e)}


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="cicd_info",
             description="Какие CI/CD настроены.",
             inputSchema={"type": "object", "properties": {}}),
        # GitLab
        Tool(name="gitlab_pipelines",
             description="Список pipelines GitLab.",
             inputSchema={"type": "object", "properties": {
                 "ref": {"type": "string"},
                 "status": {"type": "string"},
                 "limit": {"type": "integer", "default": 20}}}),
        Tool(name="gitlab_pipeline",
             description="Информация о pipeline.",
             inputSchema={"type": "object", "properties": {
                 "pipeline_id": {"type": "integer"}},
                 "required": ["pipeline_id"]}),
        Tool(name="gitlab_jobs",
             description="Jobs в pipeline.",
             inputSchema={"type": "object", "properties": {
                 "pipeline_id": {"type": "integer"}},
                 "required": ["pipeline_id"]}),
        Tool(name="gitlab_job_log",
             description="Лог job'а.",
             inputSchema={"type": "object", "properties": {
                 "job_id": {"type": "integer"}},
                 "required": ["job_id"]}),
        Tool(name="gitlab_retry",
             description="Retry job/pipeline.",
             inputSchema={"type": "object", "properties": {
                 "job_id": {"type": "integer"}}}),
        Tool(name="gitlab_cancel",
             description="Cancel pipeline.",
             inputSchema={"type": "object", "properties": {
                 "pipeline_id": {"type": "integer"}},
                 "required": ["pipeline_id"]}),
        Tool(name="gitlab_trigger",
             description="Триггер pipeline.",
             inputSchema={"type": "object", "properties": {
                 "ref": {"type": "string", "default": "main"},
                 "variables": {"type": "object"}}}),
        # Jenkins
        Tool(name="jenkins_jobs",
             description="Список Jenkins job'ов.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="jenkins_job_info",
             description="Информация о job.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"}},
                 "required": ["name"]}),
        Tool(name="jenkins_build",
             description="Запустить build.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "parameters": {"type": "object"}},
                 "required": ["name"]}),
        Tool(name="jenkins_console",
             description="Console output билда.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "number": {"type": "integer"}},
                 "required": ["name", "number"]}),
        Tool(name="jenkins_abort",
             description="Прервать билд.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "number": {"type": "integer"}},
                 "required": ["name", "number"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "cicd_info":
            return [TextContent(
                type="text",
                text=_json_result({
                    "gitlab": {
                        "configured": bool(os.getenv("GITLAB_URL") and os.getenv("GITLAB_TOKEN")),
                        "url": os.getenv("GITLAB_URL", ""),
                        "project": os.getenv("GITLAB_PROJECT", ""),
                    },
                    "jenkins": {
                        "configured": bool(os.getenv("JENKINS_URL") and os.getenv("JENKINS_TOKEN")),
                        "url": os.getenv("JENKINS_URL", ""),
                        "user": os.getenv("JENKINS_USER", ""),
                    },
                }),
            )]

        # ═══════════════════════════════════════════════════════
        # GitLab
        # ═══════════════════════════════════════════════════════
        if name.startswith("gitlab_"):
            project = _gitlab_project_id()
            if not project:
                return [TextContent(type="text", text="GITLAB_PROJECT не задан")]
            proj_enc = quote(project, safe="")

            if name == "gitlab_pipelines":
                params = {"per_page": arguments.get("limit", 20)}
                if arguments.get("ref"):
                    params["ref"] = arguments["ref"]
                if arguments.get("status"):
                    params["status"] = arguments["status"]
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "GET", f"/projects/{proj_enc}/pipelines", params=params,
                    )),
                )]

            if name == "gitlab_pipeline":
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "GET",
                        f"/projects/{proj_enc}/pipelines/{arguments['pipeline_id']}",
                    )),
                )]

            if name == "gitlab_jobs":
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "GET",
                        f"/projects/{proj_enc}/pipelines/{arguments['pipeline_id']}/jobs",
                    )),
                )]

            if name == "gitlab_job_log":
                result = await _gitlab_request(
                    "GET",
                    f"/projects/{proj_enc}/jobs/{arguments['job_id']}/trace",
                )
                if "text" in result:
                    return [TextContent(type="text", text=result["text"][:50000])]
                return [TextContent(type="text", text=_json_result(result))]

            if name == "gitlab_retry":
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "POST",
                        f"/projects/{proj_enc}/jobs/{arguments['job_id']}/retry",
                    )),
                )]

            if name == "gitlab_cancel":
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "POST",
                        f"/projects/{proj_enc}/pipelines/{arguments['pipeline_id']}/cancel",
                    )),
                )]

            if name == "gitlab_trigger":
                body = {"ref": arguments.get("ref", "main")}
                if arguments.get("variables"):
                    body["variables"] = arguments["variables"]
                return [TextContent(
                    type="text",
                    text=_json_result(await _gitlab_request(
                        "POST",
                        f"/projects/{proj_enc}/pipeline",
                        json=body,
                    )),
                )]

        # ═══════════════════════════════════════════════════════
        # Jenkins
        # ═══════════════════════════════════════════════════════
        if name.startswith("jenkins_"):
            if name == "jenkins_jobs":
                return [TextContent(
                    type="text",
                    text=_json_result(await _jenkins_request(
                        "GET", "/api/json?tree=jobs[name,url,color]",
                    )),
                )]

            if name == "jenkins_job_info":
                return [TextContent(
                    type="text",
                    text=_json_result(await _jenkins_request(
                        "GET",
                        f"/job/{arguments['name']}/api/json"
                        "?tree=name,url,builds[number,result,timestamp,duration]",
                    )),
                )]

            if name == "jenkins_build":
                n = arguments["name"]
                params = arguments.get("parameters") or {}
                if params:
                    return [TextContent(
                        type="text",
                        text=_json_result(await _jenkins_request(
                            "POST",
                            f"/job/{n}/buildWithParameters",
                            data=params,
                        )),
                    )]
                return [TextContent(
                    type="text",
                    text=_json_result(await _jenkins_request(
                        "POST", f"/job/{n}/build",
                    )),
                )]

            if name == "jenkins_console":
                return [TextContent(
                    type="text",
                    text=_json_result(await _jenkins_request(
                        "GET",
                        f"/job/{arguments['name']}/{arguments['number']}/consoleText",
                    )),
                )]

            if name == "jenkins_abort":
                return [TextContent(
                    type="text",
                    text=_json_result(await _jenkins_request(
                        "POST",
                        f"/job/{arguments['name']}/{arguments['number']}/stop",
                    )),
                )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("cicd tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

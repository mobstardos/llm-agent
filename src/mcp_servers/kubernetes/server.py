"""MCP-сервер: Kubernetes через kubectl.

Использует CLI kubectl — работает с любым кластером.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("k8s-mcp")


# ═════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════
def _kubectl() -> str | None:
    return shutil.which("kubectl") or shutil.which("kubectl.exe")


def _helm() -> str | None:
    return shutil.which("helm") or shutil.which("helm.exe")


def _kubeconfig_args() -> list[str]:
    """Дополнительные args для kubectl (--kubeconfig, --context)."""
    args: list[str] = []
    kubeconfig = os.getenv("KUBECONFIG", "").strip()
    if kubeconfig:
        args.extend(["--kubeconfig", kubeconfig])
    ctx = os.getenv("K8S_CONTEXT", "").strip()
    if ctx:
        args.extend(["--context", ctx])
    return args


async def _run(cmd: list[str], timeout: int = 60) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=os.environ.copy(),
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, f"timeout after {timeout}s"
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


async def _kubectl_run(
    args: list[str],
    timeout: int = 60,
) -> tuple[int, str]:
    kb = _kubectl()
    if not kb:
        return -1, "kubectl не найден в PATH"
    cmd = [kb] + args + _kubeconfig_args()
    return await _run(cmd, timeout=timeout)


async def _kubectl_json(
    args: list[str],
    timeout: int = 60,
) -> tuple[bool, dict | str]:
    """Запускает kubectl -o json, парсит JSON."""
    rc, out = await _kubectl_run(args + ["-o", "json"], timeout=timeout)
    if rc != 0:
        return False, out
    try:
        return True, json.loads(out)
    except json.JSONDecodeError as e:
        return False, f"JSON parse error: {e}\n{out[:1000]}"


def _json_result(data, limit: int = 50000) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:limit]
    except Exception:
        return str(data)[:limit]


def _compact_list(items: list, fields: list[str]) -> list[dict]:
    """Извлекает нужные поля из списка объектов."""
    result = []
    for item in items:
        row = {}
        for f in fields:
            parts = f.split(".")
            cur = item
            for p in parts:
                if isinstance(cur, dict):
                    cur = cur.get(p)
                else:
                    cur = None
                    break
            row[f.split(".")[-1]] = cur
        result.append(row)
    return result


app = Server("kubernetes")


# ═════════════════════════════════════════════════════════
# Tool definitions
# ═════════════════════════════════════════════════════════
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ─── Context / Cluster ──────────────────────
        Tool(name="k8s_info",
             description="Доступность kubectl/helm, версии, текущий контекст.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_list_contexts",
             description="Список контекстов kubeconfig.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_current_context",
             description="Текущий контекст.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_use_context",
             description="Переключить контекст.",
             inputSchema={"type": "object", "properties": {
                 "context": {"type": "string"}},
                 "required": ["context"]}),
        Tool(name="k8s_cluster_info",
             description="Информация о кластере (kubectl cluster-info).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_list_namespaces",
             description="Список namespace.",
             inputSchema={"type": "object", "properties": {}}),

        # ─── Pods ───────────────────────────────────
        Tool(name="k8s_list_pods",
             description="Список pods.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False},
                 "label_selector": {"type": "string"},
                 "field_selector": {"type": "string"}}}),
        Tool(name="k8s_get_pod",
             description="Информация о pod.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_describe_pod",
             description="kubectl describe pod (события, статусы).",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_pod_logs",
             description="Логи pod.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "container": {"type": "string"},
                 "tail": {"type": "integer", "default": 200},
                 "previous": {"type": "boolean", "default": False},
                 "since": {"type": "string"}},
                 "required": ["name"]}),
        Tool(name="k8s_exec_pod",
             description="Выполнить команду в pod (опасно!).",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "command": {"type": "string"},
                 "container": {"type": "string"}},
                 "required": ["name", "command"]}),
        Tool(name="k8s_delete_pod",
             description="Удалить pod (пересоздастся controller'ом).",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "force": {"type": "boolean", "default": False}},
                 "required": ["name"]}),

        # ─── Deployments ────────────────────────────
        Tool(name="k8s_list_deployments",
             description="Список deployments.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False},
                 "label_selector": {"type": "string"}}}),
        Tool(name="k8s_get_deployment",
             description="Информация о deployment.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_scale_deployment",
             description="Изменить replicas.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "replicas": {"type": "integer"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name", "replicas"]}),
        Tool(name="k8s_restart_deployment",
             description="Rolling restart deployment.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_rollout_status",
             description="Статус rollout.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "kind": {"type": "string", "default": "deployment"}},
                 "required": ["name"]}),
        Tool(name="k8s_rollout_history",
             description="История ревизий.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "kind": {"type": "string", "default": "deployment"}},
                 "required": ["name"]}),
        Tool(name="k8s_rollout_undo",
             description="Откат к ревизии.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "to_revision": {"type": "integer"},
                 "kind": {"type": "string", "default": "deployment"}},
                 "required": ["name"]}),

        # ─── Services / Ingress ─────────────────────
        Tool(name="k8s_list_services",
             description="Список services.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False}}}),
        Tool(name="k8s_get_service",
             description="Информация о service.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_list_ingresses",
             description="Список ingresses.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False}}}),

        # ─── ConfigMaps / Secrets ───────────────────
        Tool(name="k8s_list_configmaps",
             description="Список ConfigMap.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"}}}),
        Tool(name="k8s_get_configmap",
             description="Содержимое ConfigMap.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_list_secrets",
             description="Список Secret (только имена, без значений!).",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"}}}),

        # ─── Nodes ──────────────────────────────────
        Tool(name="k8s_list_nodes",
             description="Список nodes кластера.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_describe_node",
             description="Информация о node.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"}},
                 "required": ["name"]}),
        Tool(name="k8s_top_nodes",
             description="Использование CPU/RAM на nodes (metrics-server).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="k8s_top_pods",
             description="Использование CPU/RAM на pods.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False}}}),

        # ─── Events ─────────────────────────────────
        Tool(name="k8s_events",
             description="События в namespace.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False},
                 "field_selector": {"type": "string"},
                 "limit": {"type": "integer", "default": 50}}}),

        # ─── Apply / Delete ─────────────────────────
        Tool(name="k8s_apply_manifest",
             description="Применить YAML-манифест (kubectl apply).",
             inputSchema={"type": "object", "properties": {
                 "manifest": {"type": "string"},
                 "namespace": {"type": "string"}},
                 "required": ["manifest"]}),
        Tool(name="k8s_delete_manifest",
             description="Удалить ресурс из манифеста.",
             inputSchema={"type": "object", "properties": {
                 "manifest": {"type": "string"},
                 "namespace": {"type": "string"}},
                 "required": ["manifest"]}),
        Tool(name="k8s_apply_file",
             description="Применить YAML-файл из PROJECT_ROOT.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "namespace": {"type": "string"}},
                 "required": ["path"]}),

        # ─── Helm ───────────────────────────────────
        Tool(name="k8s_helm_list",
             description="Список helm releases.",
             inputSchema={"type": "object", "properties": {
                 "namespace": {"type": "string", "default": "default"},
                 "all_namespaces": {"type": "boolean", "default": False}}}),
        Tool(name="k8s_helm_status",
             description="Статус helm release.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_helm_history",
             description="История helm release.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name"]}),
        Tool(name="k8s_helm_rollback",
             description="Rollback helm release.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "revision": {"type": "integer"},
                 "namespace": {"type": "string", "default": "default"}},
                 "required": ["name", "revision"]}),
        Tool(name="k8s_helm_values",
             description="Значения helm release.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "namespace": {"type": "string", "default": "default"},
                 "all": {"type": "boolean", "default": False}},
                 "required": ["name"]}),
    ]


# ═════════════════════════════════════════════════════════
# Call tool
# ═════════════════════════════════════════════════════════
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # ─── Info ───────────────────────────────────
        if name == "k8s_info":
            kb = _kubectl()
            hl = _helm()
            result: dict = {
                "kubectl": kb or "(не установлен)",
                "helm": hl or "(не установлен)",
                "kubeconfig_env": os.getenv("KUBECONFIG", "") or "(default ~/.kube/config)",
            }
            if kb:
                rc, out = await _run([kb, "version", "--client", "-o", "json"], timeout=15)
                if rc == 0:
                    try:
                        result["kubectl_version"] = json.loads(out).get(
                            "clientVersion", {},
                        ).get("gitVersion", "?")
                    except Exception:
                        pass
                rc, out = await _kubectl_run(["config", "current-context"], timeout=10)
                if rc == 0:
                    result["current_context"] = out.strip()
            if hl:
                rc, out = await _run([hl, "version", "--short"], timeout=15)
                if rc == 0:
                    result["helm_version"] = out.strip()
            return [TextContent(type="text", text=_json_result(result))]

        # ─── Context ────────────────────────────────
        if name == "k8s_list_contexts":
            rc, out = await _kubectl_run(["config", "get-contexts"], timeout=15)
            return [TextContent(type="text", text=out[:5000])]

        if name == "k8s_current_context":
            rc, out = await _kubectl_run(["config", "current-context"], timeout=10)
            return [TextContent(type="text", text=out.strip() or "(не задан)")]

        if name == "k8s_use_context":
            ctx = arguments["context"]
            kb = _kubectl()
            if not kb:
                return [TextContent(type="text", text="kubectl не найден")]
            rc, out = await _run(
                [kb, "config", "use-context", ctx], timeout=15,
            )
            return [TextContent(
                type="text",
                text=out or f"Контекст переключён: {ctx}",
            )]

        if name == "k8s_cluster_info":
            rc, out = await _kubectl_run(["cluster-info"], timeout=15)
            return [TextContent(type="text", text=out[:5000])]

        if name == "k8s_list_namespaces":
            ok, data = await _kubectl_json(["get", "namespaces"], timeout=20)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            items = data.get("items", [])
            rows = _compact_list(items, [
                "metadata.name", "status.phase",
            ])
            return [TextContent(type="text", text=_json_result(rows))]

        # ─── Pods ───────────────────────────────────
        if name == "k8s_list_pods":
            args = ["get", "pods"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            if arguments.get("label_selector"):
                args.extend(["-l", arguments["label_selector"]])
            if arguments.get("field_selector"):
                args.extend(["--field-selector", arguments["field_selector"]])
            ok, data = await _kubectl_json(args, timeout=30)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            items = data.get("items", [])
            rows = []
            for p in items:
                statuses = p.get("status", {}).get("containerStatuses", []) or []
                ready = f"{sum(1 for s in statuses if s.get('ready'))}/{len(statuses)}"
                rows.append({
                    "name": p.get("metadata", {}).get("name"),
                    "namespace": p.get("metadata", {}).get("namespace"),
                    "phase": p.get("status", {}).get("phase"),
                    "ready": ready,
                    "restarts": sum(s.get("restartCount", 0) for s in statuses),
                    "node": p.get("spec", {}).get("nodeName"),
                    "age_started": p.get("status", {}).get("startTime"),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        if name == "k8s_get_pod":
            ns = arguments.get("namespace", "default")
            ok, data = await _kubectl_json(
                ["get", "pod", arguments["name"], "-n", ns], timeout=20,
            )
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            return [TextContent(type="text", text=_json_result(data, limit=30000))]

        if name == "k8s_describe_pod":
            ns = arguments.get("namespace", "default")
            rc, out = await _kubectl_run(
                ["describe", "pod", arguments["name"], "-n", ns], timeout=30,
            )
            return [TextContent(type="text", text=out[:20000])]

        if name == "k8s_pod_logs":
            ns = arguments.get("namespace", "default")
            args = ["logs", arguments["name"], "-n", ns,
                    "--tail", str(arguments.get("tail", 200))]
            if arguments.get("container"):
                args.extend(["-c", arguments["container"]])
            if arguments.get("previous"):
                args.append("--previous")
            if arguments.get("since"):
                args.extend(["--since", arguments["since"]])
            rc, out = await _kubectl_run(args, timeout=30)
            return [TextContent(type="text", text=out[:50000])]

        if name == "k8s_exec_pod":
            ns = arguments.get("namespace", "default")
            args = ["exec", arguments["name"], "-n", ns]
            if arguments.get("container"):
                args.extend(["-c", arguments["container"]])
            args.extend(["--", "sh", "-c", arguments["command"]])
            rc, out = await _kubectl_run(args, timeout=60)
            return [TextContent(
                type="text",
                text=f"exit={rc}\n{out[:30000]}",
            )]

        if name == "k8s_delete_pod":
            ns = arguments.get("namespace", "default")
            args = ["delete", "pod", arguments["name"], "-n", ns]
            if arguments.get("force"):
                args.extend(["--force", "--grace-period=0"])
            rc, out = await _kubectl_run(args, timeout=30)
            return [TextContent(type="text", text=out[:5000])]

        # ─── Deployments ────────────────────────────
        if name == "k8s_list_deployments":
            args = ["get", "deployments"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            if arguments.get("label_selector"):
                args.extend(["-l", arguments["label_selector"]])
            ok, data = await _kubectl_json(args, timeout=30)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            for d in data.get("items", []):
                spec = d.get("spec", {})
                status = d.get("status", {})
                rows.append({
                    "name": d.get("metadata", {}).get("name"),
                    "namespace": d.get("metadata", {}).get("namespace"),
                    "replicas": spec.get("replicas", 0),
                    "ready": status.get("readyReplicas", 0),
                    "available": status.get("availableReplicas", 0),
                    "updated": status.get("updatedReplicas", 0),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        if name == "k8s_get_deployment":
            ns = arguments.get("namespace", "default")
            ok, data = await _kubectl_json(
                ["get", "deployment", arguments["name"], "-n", ns], timeout=20,
            )
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            return [TextContent(type="text", text=_json_result(data, limit=30000))]

        if name == "k8s_scale_deployment":
            ns = arguments.get("namespace", "default")
            rc, out = await _kubectl_run(
                ["scale", "deployment", arguments["name"],
                 f"--replicas={arguments['replicas']}", "-n", ns],
                timeout=30,
            )
            return [TextContent(type="text", text=out[:5000])]

        if name == "k8s_restart_deployment":
            ns = arguments.get("namespace", "default")
            rc, out = await _kubectl_run(
                ["rollout", "restart", "deployment",
                 arguments["name"], "-n", ns],
                timeout=30,
            )
            return [TextContent(type="text", text=out[:5000])]

        if name == "k8s_rollout_status":
            ns = arguments.get("namespace", "default")
            kind = arguments.get("kind", "deployment")
            rc, out = await _kubectl_run(
                ["rollout", "status", f"{kind}/{arguments['name']}",
                 "-n", ns, "--timeout=30s"],
                timeout=40,
            )
            return [TextContent(
                type="text", text=f"exit={rc}\n{out[:5000]}",
            )]

        if name == "k8s_rollout_history":
            ns = arguments.get("namespace", "default")
            kind = arguments.get("kind", "deployment")
            rc, out = await _kubectl_run(
                ["rollout", "history", f"{kind}/{arguments['name']}",
                 "-n", ns],
                timeout=30,
            )
            return [TextContent(type="text", text=out[:10000])]

        if name == "k8s_rollout_undo":
            ns = arguments.get("namespace", "default")
            kind = arguments.get("kind", "deployment")
            args = ["rollout", "undo", f"{kind}/{arguments['name']}", "-n", ns]
            if arguments.get("to_revision"):
                args.append(f"--to-revision={arguments['to_revision']}")
            rc, out = await _kubectl_run(args, timeout=30)
            return [TextContent(type="text", text=out[:5000])]

        # ─── Services ───────────────────────────────
        if name == "k8s_list_services":
            args = ["get", "services"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            ok, data = await _kubectl_json(args, timeout=20)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            for s in data.get("items", []):
                spec = s.get("spec", {})
                ports = spec.get("ports", []) or []
                rows.append({
                    "name": s.get("metadata", {}).get("name"),
                    "namespace": s.get("metadata", {}).get("namespace"),
                    "type": spec.get("type"),
                    "cluster_ip": spec.get("clusterIP"),
                    "ports": ", ".join(
                        f"{p.get('port')}/{p.get('protocol', 'TCP')}"
                        for p in ports
                    ),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        if name == "k8s_get_service":
            ns = arguments.get("namespace", "default")
            ok, data = await _kubectl_json(
                ["get", "service", arguments["name"], "-n", ns], timeout=20,
            )
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            return [TextContent(type="text", text=_json_result(data, limit=20000))]

        if name == "k8s_list_ingresses":
            args = ["get", "ingresses"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            ok, data = await _kubectl_json(args, timeout=20)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            for i in data.get("items", []):
                spec = i.get("spec", {})
                rules = spec.get("rules", []) or []
                hosts = ", ".join(r.get("host", "*") for r in rules)
                tls = bool(spec.get("tls"))
                rows.append({
                    "name": i.get("metadata", {}).get("name"),
                    "namespace": i.get("metadata", {}).get("namespace"),
                    "hosts": hosts,
                    "tls": tls,
                    "class": spec.get("ingressClassName"),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        # ─── ConfigMaps / Secrets ───────────────────
        if name == "k8s_list_configmaps":
            ns = arguments.get("namespace", "default")
            rc, out = await _kubectl_run(
                ["get", "configmaps", "-n", ns], timeout=20,
            )
            return [TextContent(type="text", text=out[:10000])]

        if name == "k8s_get_configmap":
            ns = arguments.get("namespace", "default")
            ok, data = await _kubectl_json(
                ["get", "configmap", arguments["name"], "-n", ns], timeout=20,
            )
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            return [TextContent(
                type="text", text=_json_result(data.get("data", {})),
            )]

        if name == "k8s_list_secrets":
            ns = arguments.get("namespace", "default")
            # Только имена — никогда не выводим значения!
            ok, data = await _kubectl_json(
                ["get", "secrets", "-n", ns], timeout=20,
            )
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            for s in data.get("items", []):
                rows.append({
                    "name": s.get("metadata", {}).get("name"),
                    "type": s.get("type"),
                    "keys_count": len(s.get("data") or {}) + len(s.get("stringData") or {}),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        # ─── Nodes ──────────────────────────────────
        if name == "k8s_list_nodes":
            ok, data = await _kubectl_json(["get", "nodes"], timeout=20)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            for n in data.get("items", []):
                status = n.get("status", {})
                conditions = {c.get("type"): c.get("status")
                              for c in status.get("conditions", [])}
                addrs = status.get("addresses", [])
                internal_ip = next(
                    (a.get("address") for a in addrs
                     if a.get("type") == "InternalIP"), None,
                )
                rows.append({
                    "name": n.get("metadata", {}).get("name"),
                    "ready": conditions.get("Ready"),
                    "internal_ip": internal_ip,
                    "kubelet_version": status.get("nodeInfo", {}).get("kubeletVersion"),
                    "os_image": status.get("nodeInfo", {}).get("osImage"),
                    "arch": status.get("nodeInfo", {}).get("architecture"),
                    "labels_count": len(n.get("metadata", {}).get("labels", {})),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        if name == "k8s_describe_node":
            rc, out = await _kubectl_run(
                ["describe", "node", arguments["name"]], timeout=30,
            )
            return [TextContent(type="text", text=out[:20000])]

        if name == "k8s_top_nodes":
            rc, out = await _kubectl_run(["top", "nodes"], timeout=20)
            if rc != 0:
                return [TextContent(
                    type="text",
                    text=f"{out}\n\n(убедитесь, что metrics-server установлен)",
                )]
            return [TextContent(type="text", text=out[:5000])]

        if name == "k8s_top_pods":
            args = ["top", "pods"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            rc, out = await _kubectl_run(args, timeout=20)
            if rc != 0:
                return [TextContent(
                    type="text",
                    text=f"{out}\n\n(убедитесь, что metrics-server установлен)",
                )]
            return [TextContent(type="text", text=out[:5000])]

        # ─── Events ─────────────────────────────────
        if name == "k8s_events":
            args = ["get", "events"]
            ns = arguments.get("namespace", "default")
            if arguments.get("all_namespaces"):
                args.append("-A")
            else:
                args.extend(["-n", ns])
            if arguments.get("field_selector"):
                args.extend(["--field-selector", arguments["field_selector"]])
            args.extend(["--sort-by=.lastTimestamp"])
            ok, data = await _kubectl_json(args, timeout=20)
            if not ok:
                return [TextContent(type="text", text=str(data)[:3000])]
            rows = []
            items = data.get("items", [])[-arguments.get("limit", 50):]
            for e in items:
                rows.append({
                    "type": e.get("type"),
                    "reason": e.get("reason"),
                    "object": f"{e.get('involvedObject', {}).get('kind')}/"
                              f"{e.get('involvedObject', {}).get('name')}",
                    "namespace": e.get("metadata", {}).get("namespace"),
                    "message": (e.get("message") or "")[:200],
                    "count": e.get("count"),
                    "last_timestamp": e.get("lastTimestamp"),
                })
            return [TextContent(type="text", text=_json_result(rows))]

        # ─── Apply / Delete ─────────────────────────
        if name in ("k8s_apply_manifest", "k8s_delete_manifest"):
            manifest = arguments["manifest"]
            # Валидация — хотя бы похоже на YAML
            if "apiVersion" not in manifest or "kind" not in manifest:
                return [TextContent(
                    type="text",
                    text="Манифест должен содержать apiVersion и kind",
                )]

            tmp = Path(tempfile.gettempdir()) / f"k8s_manifest_{os.getpid()}.yaml"
            tmp.write_text(manifest, encoding="utf-8")

            try:
                verb = "apply" if name == "k8s_apply_manifest" else "delete"
                args = [verb, "-f", str(tmp)]
                if arguments.get("namespace"):
                    args.extend(["-n", arguments["namespace"]])
                rc, out = await _kubectl_run(args, timeout=60)
                return [TextContent(
                    type="text",
                    text=f"exit={rc}\n{out[:10000]}",
                )]
            finally:
                try:
                    tmp.unlink()
                except Exception:
                    pass

        if name == "k8s_apply_file":
            p = Path(arguments["path"])
            root = Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()
            path = (root / p).resolve()
            if root not in path.parents and path != root:
                return [TextContent(type="text", text="Путь вне PROJECT_ROOT")]
            if not path.exists():
                return [TextContent(type="text", text="Файл не найден")]

            args = ["apply", "-f", str(path)]
            if arguments.get("namespace"):
                args.extend(["-n", arguments["namespace"]])
            rc, out = await _kubectl_run(args, timeout=120)
            return [TextContent(
                type="text", text=f"exit={rc}\n{out[:10000]}",
            )]

        # ─── Helm ───────────────────────────────────
        if name.startswith("k8s_helm_"):
            hl = _helm()
            if not hl:
                return [TextContent(type="text", text="helm не установлен")]

            base_args = [hl]
            kubeconfig = os.getenv("KUBECONFIG", "").strip()
            if kubeconfig:
                base_args.extend(["--kubeconfig", kubeconfig])

            if name == "k8s_helm_list":
                args = base_args + ["list"]
                ns = arguments.get("namespace", "default")
                if arguments.get("all_namespaces"):
                    args.append("-A")
                else:
                    args.extend(["-n", ns])
                args.extend(["-o", "json"])
                rc, out = await _run(args, timeout=30)
                return [TextContent(type="text", text=out[:20000])]

            if name == "k8s_helm_status":
                args = base_args + [
                    "status", arguments["name"],
                    "-n", arguments.get("namespace", "default"),
                    "-o", "json",
                ]
                rc, out = await _run(args, timeout=30)
                return [TextContent(type="text", text=out[:20000])]

            if name == "k8s_helm_history":
                args = base_args + [
                    "history", arguments["name"],
                    "-n", arguments.get("namespace", "default"),
                    "-o", "json",
                ]
                rc, out = await _run(args, timeout=30)
                return [TextContent(type="text", text=out[:20000])]

            if name == "k8s_helm_rollback":
                args = base_args + [
                    "rollback", arguments["name"], str(arguments["revision"]),
                    "-n", arguments.get("namespace", "default"),
                ]
                rc, out = await _run(args, timeout=120)
                return [TextContent(
                    type="text", text=f"exit={rc}\n{out[:10000]}",
                )]

            if name == "k8s_helm_values":
                args = base_args + [
                    "get", "values", arguments["name"],
                    "-n", arguments.get("namespace", "default"),
                    "-o", "json",
                ]
                if arguments.get("all"):
                    args.append("--all")
                rc, out = await _run(args, timeout=30)
                return [TextContent(type="text", text=out[:30000])]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("K8s tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

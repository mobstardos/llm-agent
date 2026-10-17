# -*- coding: utf-8 -*-
"""Registry (25 эндпоинтов) — вынесены из src/main.py.

Sprint 1.B: snapshot, agents (×5), mcp (×2), capabilities (×2), reload,
prompt, history (×2), profiles (×6), overrides/export, audit, rollback (×2).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import Response

from src.agents.runtime import AgentRuntime
from src.core.registry import Registry
from src.mcp_manager import MCPManager
from src.state import get_registry, state

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/registry", tags=["registry"])


# ─── Sync helpers ───────────────────────────────────────────────────
async def _sync_mcp() -> None:
    """Пересинхронизировать MCP-серверы с текущим snapshot."""
    reg = state.registry
    mcp = state.mcp
    if not reg or not reg.snapshot or not mcp:
        return
    wanted = set(reg.snapshot.enabled_mcp_ids)
    running = set(mcp.sessions.keys())
    to_start: dict[str, dict] = {}
    for mid in wanted - running:
        mschema = reg.mcp_servers.get(mid)
        if mschema:
            to_start[mid] = {
                "command": mschema.command,
                "args": mschema.args,
                "env": mschema.env,
            }
    if to_start:
        await mcp.start(to_start)
    for mid, st in reg.snapshot.mcp_servers.items():
        st.alive = mid in mcp.sessions


async def _sync_runtime() -> None:
    """Перестроить AgentRuntime из snapshot."""
    runtime = state.agent_runtime
    reg = state.registry
    if runtime and reg and reg.snapshot:
        runtime.rebuild(reg.snapshot)


# ─── Snapshot + agents ────────────────────────────────────────────
@router.get("/snapshot")
async def snapshot(reg: Registry = Depends(get_registry)) -> dict:
    if not reg.snapshot:
        raise HTTPException(503, "Registry not ready")
    return reg.snapshot_summary()


@router.get("/agents")
async def agents_list(reg: Registry = Depends(get_registry)) -> dict:
    return {"agents": reg.list_all_agents()}


@router.get("/agents/{agent_id}")
async def agent_info(agent_id: str, reg: Registry = Depends(get_registry)) -> dict:
    info = reg.agent_info(agent_id)
    if not info:
        raise HTTPException(404, "Agent not found")
    return info


@router.post("/agents/{agent_id}/enable")
async def agent_enable(agent_id: str, payload: dict = Body(...),
                       reg: Registry = Depends(get_registry)) -> dict:
    enabled = bool(payload.get("enabled", True))
    await reg.set_agent_enabled(agent_id, enabled)
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "enabled": enabled}


@router.post("/agents/{agent_id}/params")
async def agent_params(agent_id: str, payload: dict = Body(...),
                       reg: Registry = Depends(get_registry)) -> dict:
    await reg.update_agent_params(agent_id, payload)
    await _sync_runtime()
    return {"ok": True}


@router.post("/agents/{agent_id}/reset")
async def agent_reset(agent_id: str, reg: Registry = Depends(get_registry)) -> dict:
    await reg.reset_agent(agent_id)
    await _sync_runtime()
    return {"ok": True}


@router.post("/agents/{agent_id}/check")
async def agent_check(agent_id: str, reg: Registry = Depends(get_registry)) -> dict:
    await reg.build_snapshot(reason="manual_check")
    await _sync_mcp()
    await _sync_runtime()
    return reg.agent_info(agent_id)


# ─── MCP servers ────────────────────────────────────────────────────
@router.get("/mcp")
async def mcp_list(reg: Registry = Depends(get_registry)) -> dict:
    return {"mcp_servers": reg.list_all_mcp()}


@router.post("/mcp/{mcp_id}/enable")
async def mcp_enable(mcp_id: str, payload: dict = Body(...),
                     reg: Registry = Depends(get_registry)) -> dict:
    enabled = bool(payload.get("enabled", True))
    await reg.set_mcp_enabled(mcp_id, enabled)
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "enabled": enabled}


# ─── Capabilities ────────────────────────────────────────────────────
@router.get("/capabilities")
async def capabilities_list(reg: Registry = Depends(get_registry)) -> dict:
    return {"capabilities": reg.list_all_capabilities()}


@router.post("/capabilities/{cap_id}/provider")
async def capability_provider(cap_id: str, payload: dict = Body(...),
                               reg: Registry = Depends(get_registry)) -> dict:
    provider_id = payload.get("provider_id")
    await reg.set_capability_provider(cap_id, provider_id)
    return {"ok": True}


# ─── Reload + prompt + history ──────────────────────────────────────
@router.post("/reload")
async def reload_all(reg: Registry = Depends(get_registry)) -> dict:
    await reg.reload_all()
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True}


@router.get("/prompt")
async def prompt(reg: Registry = Depends(get_registry)) -> dict:
    if not reg.snapshot:
        raise HTTPException(503, "Registry not ready")
    return {
        "prompt": reg.snapshot.orchestrator_prompt,
        "length": len(reg.snapshot.orchestrator_prompt),
        "hash": reg.snapshot.orchestrator_prompt_hash,
    }


@router.get("/history")
async def history(limit: int = 20, reg: Registry = Depends(get_registry)) -> dict:
    items = reg.snapshot_history(limit=limit)
    return {"snapshots": [
        {
            "id": r.id, "built_at": r.built_at, "slot": r.slot,
            "reason": r.reason,
            "agents_count": len(r.agents),
            "mcp_count": len(r.mcp),
            "prompt_length": r.prompt_length,
        } for r in items
    ]}


@router.get("/history/diff")
async def history_diff(from_id: str, to_id: str,
                       reg: Registry = Depends(get_registry)) -> dict:
    d = reg.snapshot_diff(from_id, to_id)
    return d.to_dict()


# ─── Profiles ───────────────────────────────────────────────────────
@router.get("/profiles")
async def profiles_list(reg: Registry = Depends(get_registry)) -> dict:
    return {"profiles": [
        {
            "id": p.id, "title": p.title, "description": p.description,
            "is_builtin": p.is_builtin, "overrides": p.overrides,
        } for p in reg.profiles.list()
    ]}


@router.post("/profiles/{profile_id}/apply")
async def profile_apply(profile_id: str, reg: Registry = Depends(get_registry)) -> dict:
    try:
        p = await reg.apply_profile(profile_id, actor="ui")
    except ValueError as e:
        raise HTTPException(404, str(e))
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True, "profile": p.id}


@router.post("/profiles/{profile_id}/save")
async def profile_save(profile_id: str, payload: dict = Body(...),
                       reg: Registry = Depends(get_registry)) -> dict:
    """Сохраняет текущие overrides как профиль."""
    from src.core.profiles import Profile
    p = Profile(
        id=profile_id,
        title=payload.get("title", profile_id),
        description=payload.get("description", ""),
        overrides=reg.runtime.get_overrides(),
        is_builtin=False,
    )
    reg.profiles.save(p)
    reg.audit.log("ui", "profile_save", profile_id)
    return {"ok": True}


@router.delete("/profiles/{profile_id}")
async def profile_delete(profile_id: str,
                         reg: Registry = Depends(get_registry)) -> dict:
    if not reg.profiles.delete(profile_id):
        raise HTTPException(400, "Нельзя удалить (builtin или не найден)")
    reg.audit.log("ui", "profile_delete", profile_id)
    return {"ok": True}


@router.get("/profiles/{profile_id}/export")
async def profile_export(profile_id: str,
                         reg: Registry = Depends(get_registry)) -> Response:
    try:
        data = reg.profiles.export(profile_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="{profile_id}.json"',
        },
    )


@router.post("/profiles/import")
async def profile_import(request: Request,
                         reg: Registry = Depends(get_registry)) -> dict:
    try:
        data = await request.json()
        p = reg.profiles.import_data(data)
        reg.audit.log("ui", "profile_import", p.id)
        return {"ok": True, "profile": p.id}
    except Exception as e:
        raise HTTPException(400, str(e))


# ─── Overrides + Audit + Rollback ──────────────────────────────────
@router.get("/overrides/export")
async def overrides_export(reg: Registry = Depends(get_registry)) -> Response:
    """Экспорт текущих runtime overrides."""
    data = {
        "overrides": reg.runtime.get_overrides(),
        "exported_at": datetime.now(timezone.utc).isoformat(),
    }
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    return Response(
        content=body, media_type="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="current-overrides.json"',
        },
    )


@router.get("/audit")
async def audit_list(limit: int = 100, target: str | None = None,
                     action: str | None = None,
                     reg: Registry = Depends(get_registry)) -> dict:
    items = reg.audit_list(limit=limit, target=target, action=action)
    return {"events": items}


@router.get("/rollback/{kind}")
async def rollback_list(kind: str, reg: Registry = Depends(get_registry)) -> dict:
    if kind not in ("agents", "mcp_servers", "capabilities"):
        raise HTTPException(400, "kind: agents | mcp_servers | capabilities")
    return {"versions": reg.rollback.list(kind)}


@router.post("/rollback/{kind}/restore")
async def rollback_restore(kind: str, payload: dict = Body(...),
                            reg: Registry = Depends(get_registry)) -> dict:
    version_dir = payload.get("version_dir")
    if not reg.rollback_restore(kind, version_dir):
        raise HTTPException(500, "Не удалось восстановить")
    reg.audit.log("ui", "rollback_restore", kind,
                  details={"version_dir": version_dir})
    await reg.reload_all()
    await _sync_mcp()
    await _sync_runtime()
    return {"ok": True}

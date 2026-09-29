"""Управление MCP-серверами и вызов инструментов с approval-gate.

Журналирование (Этап 5): НЕ здесь. Перехват всех tool calls делает
src/journal/integration.py:instrument_mcp_manager(mcp) — он оборачивает
call_tool (before/after_tool_call, тени файлов, откат). Этот класс остаётся
чистым транспортом MCP + approval — «мягкая» интеграция без жёстких связей.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import AsyncExitStack
from typing import Any, Awaitable, Callable

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

logger = logging.getLogger(__name__)

ApprovalHandler = Callable[[dict], Awaitable[bool]]


class MCPManager:
    """MCP manager: запуск серверов, вызов инструментов, approval gate."""

    def __init__(
        self,
        require_confirmation: set[str] | None = None,
        project_root: str | None = None,
        agent_context: dict | None = None,
    ):
        # Свой exit-stack на каждый сервер — иначе нельзя остановить
        # один процесс, не закрыв остальные (нужно для restart из UI).
        self._stacks: dict[str, AsyncExitStack] = {}
        self.sessions: dict[str, ClientSession] = {}
        self.tools: dict[str, list[Any]] = {}
        # Причина последнего неудачного старта — для карточки в UI
        self.errors: dict[str, str] = {}
        self.require_confirmation = require_confirmation or set()

        self.project_root = project_root or os.getenv("PROJECT_ROOT", "")

        # Контекст текущего вызова (заполняется извне; читается хуками)
        self.agent_context: dict = agent_context or {}

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════
    async def start(self, servers: dict[str, dict]) -> None:
        for name, cfg in servers.items():
            if name in self.sessions:
                continue
            await self._start_server(
                name, cfg["command"], cfg.get("args", []), cfg.get("env"),
            )

    async def start_one(self, name: str, cfg: dict) -> bool:
        """Запустить один сервер. True — если он теперь в sessions."""
        if name in self.sessions:
            return True
        await self._start_server(
            name, cfg["command"], cfg.get("args", []), cfg.get("env"),
        )
        return name in self.sessions

    async def _start_server(
        self, name: str, command: str, args: list[str],
        env: dict | None = None,
    ) -> None:
        if name in self._stacks:
            return

        stack = AsyncExitStack()
        merged_env: dict[str, str] = {**os.environ}
        if env:
            for k, v in env.items():
                if v is not None:
                    merged_env[str(k)] = str(v)

        params = StdioServerParameters(
            command=command, args=args, env=merged_env,
        )
        try:
            read, write = await stack.enter_async_context(
                stdio_client(params)
            )
            session = await stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            tools_resp = await session.list_tools()
            self._stacks[name] = stack
            self.sessions[name] = session
            self.tools[name] = list(tools_resp.tools)
            self.errors.pop(name, None)
            logger.info(
                "MCP '%s' запущен, %d инструментов",
                name, len(self.tools[name]),
            )
        except Exception as e:
            # Причина сохраняется для UI (карточка «не настроен/ошибка»)
            self.errors[name] = str(e)
            logger.exception("Failed to start MCP '%s': %s", name, e)
            try:
                await stack.aclose()
            except Exception:
                pass

    async def stop_one(self, name: str) -> bool:
        """Остановить один сервер (закрыть его стек). True — был запущен."""
        stack = self._stacks.pop(name, None)
        self.sessions.pop(name, None)
        self.tools.pop(name, None)
        if stack is None:
            return False
        try:
            await stack.aclose()
        except Exception:
            pass
        return True

    async def restart_one(self, name: str, cfg: dict) -> bool:
        """Перезапустить один сервер (например, после смены env в UI)."""
        await self.stop_one(name)
        return await self.start_one(name, cfg)

    # ═══════════════════════════════════════════════════════
    # Tools
    # ═══════════════════════════════════════════════════════
    def get_openai_tools(self, server_names: list[str]) -> list[dict]:
        result: list[dict] = []
        for srv in server_names:
            for tool in self.tools.get(srv, []):
                result.append({
                    "type": "function",
                    "function": {
                        "name": f"{srv}__{tool.name}",
                        "description": tool.description or "",
                        "parameters": tool.inputSchema
                                      or {"type": "object", "properties": {}},
                    },
                })
        return result

    # ═══════════════════════════════════════════════════════
    # Call tool (с journal + approval)
    # ═══════════════════════════════════════════════════════
    async def call_tool(
        self,
        qualified_name: str,
        arguments: dict,
        approval_handler: ApprovalHandler | None = None,
        agent: str | None = None,
    ) -> str:
        if "__" not in qualified_name:
            return f"Ошибка: неверное имя '{qualified_name}'"

        server, tool = qualified_name.split("__", 1)

        # ─── Approval gate ─────────────────────────────
        requires = (
            tool in self.require_confirmation
            or qualified_name in self.require_confirmation
        )
        if requires:
            if approval_handler is None:
                return f"⛔ '{qualified_name}' требует подтверждения."

            try:
                approved = await approval_handler({
                    "tool": qualified_name,
                    "arguments": arguments,
                    "reason": f"Операция '{tool}' изменяет данные.",
                })
            except Exception as e:
                return f"⛔ Ошибка approval: {e}"
            if not approved:
                # Статус «denied» журнал увидит сам — перехват в
                # integration.instrument_mcp_manager пишет после-манифест
                return f"⛔ Отклонено: {qualified_name}"

        # ─── Actual call ───────────────────────────────
        session = self.sessions.get(server)
        if not session:
            error = f"Ошибка: MCP '{server}' не запущен"
            return error

        t0 = time.perf_counter()
        try:
            result = await session.call_tool(tool, arguments or {})
            parts: list[str] = []
            for c in result.content:
                text = getattr(c, "text", None)
                if text:
                    parts.append(text)
            output = "\n".join(parts) if parts else "(пусто)"
            duration_ms = (time.perf_counter() - t0) * 1000
            return output

        except Exception as e:
            duration_ms = (time.perf_counter() - t0) * 1000
            error_text = f"Ошибка вызова {qualified_name}: {e}"
            logger.exception("Tool call failed: %s", qualified_name)
            return error_text

    # ═══════════════════════════════════════════════════════
    # Context management
    # ═══════════════════════════════════════════════════════
    def set_context(
        self,
        *,
        agent: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        parent_id: str | None = None,
    ) -> None:
        """Установить контекст для последующих вызовов (для хуков)."""
        if agent is not None:
            self.agent_context["agent"] = agent
        if trace_id is not None:
            self.agent_context["trace_id"] = trace_id
        if session_id is not None:
            self.agent_context["session_id"] = session_id
        if parent_id is not None:
            self.agent_context["parent_id"] = parent_id

    async def stop(self) -> None:
        for name in list(self._stacks):
            await self.stop_one(name)
        self.errors.clear()

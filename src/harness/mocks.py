"""Mock LLM и MCP с записью/воспроизведением fixtures."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════
# Fixture storage
# ═════════════════════════════════════════════════════════
class LlmFixtureStore:
    """Хранилище записанных ответов LLM.

    Формат файла: harness/fixtures/llm/<scenario_id>.json
    {
      "scenario_id": "file_read_basic",
      "model": "qwen3.8-max",
      "recorded_at": 1757945000,
      "exchanges": [
        {
          "seq": 0,
          "messages_hash": "abc123",
          "response": {...}
        }
      ]
    }
    """

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, scenario_id: str) -> Path:
        return self.base_dir / f"{scenario_id}.json"

    def load(self, scenario_id: str) -> dict | None:
        p = self.path_for(scenario_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Fixture load failed %s: %s", p, e)
            return None

    def save(self, scenario_id: str, model: str, exchanges: list[dict]) -> Path:
        p = self.path_for(scenario_id)
        import time
        data = {
            "scenario_id": scenario_id,
            "model": model,
            "recorded_at": time.time(),
            "exchange_count": len(exchanges),
            "exchanges": exchanges,
        }
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("Fixtures сохранены: %s (%d exchanges)", p, len(exchanges))
        return p


# ═════════════════════════════════════════════════════════
# Mock LLM
# ═════════════════════════════════════════════════════════
class MockLLMClient:
    """Mock-LLM: воспроизводит записанные ответы по sequence number."""

    def __init__(
        self,
        exchanges: list[dict] | None = None,
        strict: bool = False,
    ):
        self.exchanges = exchanges or []
        self.strict = strict
        self.call_count = 0
        self.calls: list[dict] = []

    @staticmethod
    def hash_messages(messages: list[dict]) -> str:
        simplified = [
            {"role": m.get("role"), "content": m.get("content") or ""}
            for m in messages
        ]
        raw = json.dumps(simplified, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        on_token: Any = None,
        state_hash: str | None = None,
        **kwargs,
    ) -> dict:
        seq = self.call_count
        self.call_count += 1

        # Ищем exchange по sequence
        exchange = None
        for ex in self.exchanges:
            if ex.get("seq") == seq:
                exchange = ex
                break

        # Fallback: по хэшу messages
        if exchange is None:
            h = self.hash_messages(messages)
            for ex in self.exchanges:
                if ex.get("messages_hash") == h:
                    exchange = ex
                    break

        self.calls.append({
            "seq": seq,
            "messages_hash": self.hash_messages(messages),
            "found": exchange is not None,
        })

        if exchange is None:
            msg = f"MockLLM: нет fixture для seq={seq}"
            logger.warning(msg)
            if self.strict:
                raise RuntimeError(msg)
            return {
                "role": "assistant",
                "content": f"(mock: no response for seq={seq})",
                "tool_calls": None,
            }

        response = dict(exchange["response"])

        # Симулируем streaming
        content = response.get("content") or ""
        if on_token and content:
            try:
                await on_token(content)
            except Exception:
                pass

        return response


# ═════════════════════════════════════════════════════════
# Recording LLM (обёртка над реальным)
# ═════════════════════════════════════════════════════════
class RecordingLLMClient:
    """Обёртка над реальным LLMClient: записывает все ответы."""

    def __init__(self, real_client: Any):
        self.real = real_client
        self.exchanges: list[dict] = []

    @staticmethod
    def hash_messages(messages: list[dict]) -> str:
        return MockLLMClient.hash_messages(messages)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        on_token: Any = None,
        state_hash: str | None = None,
        **kwargs,
    ) -> dict:
        response = await self.real.chat(
            messages, tools=tools, model=model,
            on_token=on_token, state_hash=state_hash, **kwargs,
        )

        # Убираем chunks из записи (не воспроизводимы)
        recorded = {k: v for k, v in response.items() if k != "chunks"}

        self.exchanges.append({
            "seq": len(self.exchanges),
            "messages_hash": self.hash_messages(messages),
            "response": recorded,
        })

        return response


# ═════════════════════════════════════════════════════════
# Mock MCP
# ═════════════════════════════════════════════════════════
class MockMCPManager:
    """Mock-MCP: возвращает фиксированные ответы для tools."""

    def __init__(self, responses: dict[str, Any] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, dict]] = []
        self.sessions: dict[str, Any] = {}   # для совместимости с AgentRuntime

    def get_openai_tools(self, servers: list[str]) -> list[dict]:
        """Возвращает список tools для указанных серверов.

        Если MCP-мок получил описание tools из фикстуры — использует их.
        Иначе — генерирует фиктивные.
        """
        declared = self.responses.get("__tools__")
        if declared:
            # Из fixtures/mcp/tools.json
            result = []
            for srv in servers:
                for tool in declared.get(srv, []):
                    result.append({
                        "type": "function",
                        "function": tool,
                    })
            return result

        # Fallback: фиктивные tools
        return [
            {
                "type": "function",
                "function": {
                    "name": f"{srv}__mock",
                    "description": f"Mock tool for {srv}",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for srv in servers
        ]

    async def call_tool(
        self, name: str, arguments: dict,
        approval_handler: Any = None,
    ) -> str:
        self.calls.append((name, arguments))

        # Точное совпадение (name + args)
        key = f"{name}:{json.dumps(arguments, sort_keys=True)}"
        if key in self.responses:
            return str(self.responses[key])

        # По имени инструмента
        if name in self.responses:
            val = self.responses[name]
            if isinstance(val, list):
                # Список ответов — берём по порядку
                idx = sum(1 for n, _ in self.calls if n == name) - 1
                if idx < len(val):
                    return str(val[idx])
            return str(val)

        # Default
        if "default" in self.responses:
            return str(self.responses["default"])

        return f"(mock result for {name})"


# ═════════════════════════════════════════════════════════
# Recording MCP (обёртка над реальным)
# ═════════════════════════════════════════════════════════
class RecordingMCPManager:
    """Обёртка над реальным MCPManager: записывает tool calls и ответы."""

    def __init__(self, real_manager: Any):
        self.real = real_manager
        self.calls: list[dict] = []
        self.sessions = real_manager.sessions

    def get_openai_tools(self, servers: list[str]) -> list[dict]:
        return self.real.get_openai_tools(servers)

    async def call_tool(
        self, name: str, arguments: dict,
        approval_handler: Any = None,
    ) -> str:
        result = await self.real.call_tool(
            name, arguments, approval_handler=approval_handler,
        )
        self.calls.append({
            "name": name,
            "arguments": arguments,
            "result": result,
        })
        return result

    def get_recorded_responses(self) -> dict[str, Any]:
        """Формат для MockMCPManager."""
        responses: dict[str, Any] = {}
        for c in self.calls:
            responses[c["name"]] = c["result"]
        return responses

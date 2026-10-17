"""Загрузка деклараций агентов, MCP-серверов и capabilities из файлов.

Каждая декларация — директория (агенты, MCP) или файл (capabilities).
Ошибка в одной декларации НЕ ломает загрузку остальных.
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.core.schema import (
    AgentSchema,
    CapabilitySchema,
    MCPServerSchema,
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class DeclarationLoader:
    """Сканирует директории и превращает YAML в Pydantic-модели."""

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or BASE_DIR
        self.errors: dict[str, str] = {}   # id → сообщение об ошибке

    # ═══════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════
    def load_all(self) -> dict:
        self.errors.clear()
        return {
            "agents": self.load_agents(),
            "mcp_servers": self.load_mcp_servers(),
            "capabilities": self.load_capabilities(),
        }

    def load_agents(self) -> dict[str, AgentSchema]:
        return self._load_agents(self.base_dir / "agents")

    def load_mcp_servers(self) -> dict[str, MCPServerSchema]:
        return self._load_mcp(self.base_dir / "mcp_servers")

    def load_capabilities(self) -> dict[str, CapabilitySchema]:
        return self._load_cap(self.base_dir / "capabilities")

    # ═══════════════════════════════════════════════════════
    # Agents
    # ═══════════════════════════════════════════════════════
    def _load_agents(self, root: Path) -> dict[str, AgentSchema]:
        result: dict[str, AgentSchema] = {}
        if not root.exists():
            logger.debug("Директория агентов не найдена: %s", root)
            return result

        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            f = d / "agent.yaml"
            if not f.exists():
                logger.debug("Нет agent.yaml в %s", d)
                continue
            try:
                schema = self._parse_agent(f, d)
                result[schema.id] = schema
                logger.info(
                    "Agent декларация загружена: %s (v%s, schema %s)",
                    schema.id, schema.version, schema.schema_version,
                )
            except Exception as e:
                self.errors[d.name] = str(e)
                logger.error("Ошибка загрузки %s: %s", f, e)
        return result

    def _parse_agent(self, path: Path, dir_: Path) -> AgentSchema:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        try:
            schema = AgentSchema(**data)
        except ValidationError as e:
            raise ValueError(f"Schema validation failed: {e}") from e

        schema.source_dir = str(dir_)

        # Промпт
        prompt_path = dir_ / schema.prompt
        if prompt_path.exists():
            schema.prompt_text = prompt_path.read_text(encoding="utf-8")
        else:
            logger.warning("Prompt не найден: %s", prompt_path)

        # User template
        user_path = dir_ / schema.user_template
        if user_path.exists():
            schema.user_template_text = user_path.read_text(encoding="utf-8")

        return schema

    # ═══════════════════════════════════════════════════════
    # MCP Servers
    # ═══════════════════════════════════════════════════════
    def _load_mcp(self, root: Path) -> dict[str, MCPServerSchema]:
        result: dict[str, MCPServerSchema] = {}
        if not root.exists():
            logger.debug("Директория MCP не найдена: %s", root)
            return result

        for d in sorted(root.iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue
            f = d / "server.yaml"
            if not f.exists():
                logger.debug("Нет server.yaml в %s", d)
                continue
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                schema = MCPServerSchema(**data)
                schema.source_dir = str(d)
                result[schema.id] = schema
                logger.info("MCP декларация загружена: %s", schema.id)
            except Exception as e:
                self.errors[f"mcp/{d.name}"] = str(e)
                logger.error("Ошибка MCP %s: %s", f, e)
        return result

    # ═══════════════════════════════════════════════════════
    # Capabilities
    # ═══════════════════════════════════════════════════════
    def _load_cap(self, root: Path) -> dict[str, CapabilitySchema]:
        result: dict[str, CapabilitySchema] = {}
        if not root.exists():
            logger.debug("Директория capabilities не найдена: %s", root)
            return result

        for f in sorted(root.glob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                schema = CapabilitySchema(**data)
                schema.source_path = str(f)
                result[schema.id] = schema
                logger.info(
                    "Capability загружена: %s (%d провайдеров)",
                    schema.id, len(schema.providers),
                )
            except Exception as e:
                self.errors[f"cap/{f.name}"] = str(e)
                logger.error("Ошибка capability %s: %s", f, e)
        return result

"""Агенты — обёртки над LoopController с декларацией из agents/<id>/agent.yaml."""
from src.agents.base import BaseAgent
from src.agents.runtime import AgentRuntime

__all__ = ["BaseAgent", "AgentRuntime"]

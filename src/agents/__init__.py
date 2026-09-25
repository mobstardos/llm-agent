"""Агенты системы (Этап 5): живёт только декларативная модель.

Рабочие компоненты:
- base.py     — BaseAgent: контракт запуска агента (используется рантаймом)
- runtime.py  — AgentRuntime: сборка агентов из деклараций agents/*/agent.yaml

Классы-агенты старого поколения (FileAgent, MySQLAgent, OneCAgent, …)
удалены из пакета и перенесены в attic/agents-legacy/ — нигде не
использовались, кроме импорта в этом __init__.py. Система строится на
декларациях agents/*/agent.yaml + BaseAgent (ARCHITECTURE-V2 §2).
"""

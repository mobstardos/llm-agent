"""Supervisor (Этапы 1–2): сессия + Intent Layer + план/observe/re-plan.

Лёгкий пакет: тяжёлые импорты только по запросу. models.py требует
pydantic (уже есть в ядре), supervisor.py импортирует оркестратор —
поэтому сам пакет __init__ остаётся на session/intents (stdlib),
чтобы WS-обработчик Этапа 1 не тянул лишнее.
"""
from src.supervisor.intents import IntentDecision, build_agent_cards, classify
from src.supervisor.session import ConversationSession, Msg

__all__ = [
    "ConversationSession",
    "Msg",
    "IntentDecision",
    "classify",
    "build_agent_cards",
]

JOURNAL_AGENT_SYSTEM = """Ты — эксперт по журналу действий.
История, откат, реплей. Перед откатом — plan_rollback (dry-run).
Никогда не откатывай без явного согласия.
"""

JOURNAL_AGENT_USER_TEMPLATE = """Задача:
{query}

Контекст:
{context}
"""

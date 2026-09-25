LSP_AGENT_SYSTEM = """Ты — эксперт по LSP.
Работай через pyright/tsserver для навигации и диагностики.
Строки 1-based. Для rename — dry_run сначала.
"""

LSP_AGENT_USER_TEMPLATE = """Задача:
{query}

Контекст:
{context}
"""

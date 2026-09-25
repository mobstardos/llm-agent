GITHUB_AGENT_SYSTEM = """Ты — эксперт по GitHub.
PR, issues, actions, releases.
Merge только после approve. Не создавай релизы без явного запроса.
"""

GITHUB_AGENT_USER_TEMPLATE = """Задача:
{query}

Контекст:
{context}
"""

#!/usr/bin/env python3
"""Скелетонер агента (Этап 4, ARCHITECTURE-V2 §2.3).

    python scripts/new_agent.py my_agent --title "Мой агент" \
        --keywords "моё,задача" --template basic

Генерирует agents/<id>/: agent.yaml (с комментариями по каждому полю),
prompt.md, user.md — затем подсказывает прогнать валидатор:
    python -m src.cli validate
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
AGENTS_DIR = BASE_DIR / "agents"

AGENT_YAML = """id: {id}
schema_version: "1.5.0"
title: {title}
version: "0.1.0"
description: {description}

# ── Требования окружения (проверяются реестром при старте) ──
requires:
  python_packages: []
  # - {{name: mcp, level: hard}}
  env_vars: []
  # - {{name: PROJECT_ROOT, level: hard}}

# ── MCP-серверы из mcp_servers/<id>/server.yaml ──────────────
mcp_servers: []
# mcp_servers: [filesystem]

depends_on: {{hard: [], soft: []}}

priority: 100
routing_hints:
  keywords: [{keywords}]
  negative_keywords: []
  # description_for_router попадает в промпт роутера/планировщика:
  description_for_router: |
    {description}

# read | write | destructive (destructive → апрув действий)
mode: read
dangerous_tools: []

prompt: prompt.md
user_template: user.md

runtime:
  max_steps: 10
  max_result_chars: 30000
  timeout_seconds: 300
"""

PROMPT_MD = """# Агент: {title}

Ты — {title_lower}. Работаешь внутри мультиагентной системы LLM Agent.

## Правила
1. Выполняй задачу из запроса пользователя строго по существу.
2. Отвечай по-русски, кратко и конкретно; факты не выдумывай.
3. Если данных не хватает — задай один уточняющий вопрос.

## Формат ответа
Короткий текстовый ответ с результатом; детали — списком при необходимости.
"""

USER_MD = """Запрос: {{query}}

{context}
"""

TEMPLATES = {
    "basic": "",
    "reasoning": (
        "\n# Сценарий рассуждения (loops/*.yaml)\n"
        "loop: reasoning\n"
        "# см. loops/reasoning.yaml: hypothesis → check → answer\n"
    ),
}


def main() -> int:
    ap = argparse.ArgumentParser(description="Генератор скелета агента")
    ap.add_argument("agent_id", help="id агента: a-z0-9_- (напр. my_agent)")
    ap.add_argument("--title", default=None, help="человекочитаемое название")
    ap.add_argument("--description", default=None,
                    help="описание для роутера (одно предложение)")
    ap.add_argument("--keywords", default="",
                    help="ключевые слова через запятую для routing_hints")
    ap.add_argument("--template", choices=list(TEMPLATES), default="basic",
                    help="шаблон: basic | reasoning")
    args = ap.parse_args()

    agent_id = args.agent_id.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", agent_id):
        print(f"✗ Некорректный id: '{agent_id}' (разрешено a-z0-9_-)",
              file=sys.stderr)
        return 2

    target = AGENTS_DIR / agent_id
    if target.exists():
        print(f"✗ Директория уже существует: {target}", file=sys.stderr)
        return 2

    title = args.title or agent_id.replace("_", " ").replace("-", " ").capitalize()
    description = args.description or f"Агент «{title}» (заготовка — опишите задачу)"
    keywords = ", ".join(k.strip() for k in args.keywords.split(",") if k.strip())

    target.mkdir(parents=True, exist_ok=False)
    (target / "agent.yaml").write_text(
        AGENT_YAML.format(id=agent_id, title=title, description=description,
                          keywords=keywords) + TEMPLATES[args.template],
        encoding="utf-8")
    (target / "prompt.md").write_text(
        PROMPT_MD.format(title=title, title_lower=title.lower()),
        encoding="utf-8")
    (target / "user.md").write_text(
        USER_MD.format(context="{context_text}"), encoding="utf-8")

    print(f"✓ Агент создан: {target}")
    print("  1. Заполните prompt.md и description_for_router в agent.yaml")
    print("  2. Проверка декларации:  python -m src.cli validate")
    print(f"  3. Обратиться в чате:    @{agent_id} <задача>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

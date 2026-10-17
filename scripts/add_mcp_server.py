#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""add_mcp_server.py — утилита скаффолдинга нового MCP-сервера + агента.

Создаёт 5 файлов:
  - mcp_servers/<id>/server.yaml
  - agents/<id>/agent.yaml
  - agents/<id>/prompt.md
  - agents/<id>/user.md
  - src/mcp_servers/<id>/{__init__.py,server.py}  (если --native)

Запуск:
  python scripts/add_mcp_server.py \\
      --id my_new_mcp \\
      --title "My New MCP" \\
      --description "Does X, Y, Z" \\
      --category database \\
      --command npx \\
      --args '["-y", "my-package"]' \\
      --env-vars 'MY_API_KEY:hard' \\
      --mode read \\
      --tools 'list_items:read, create_item:write, delete_item:destructive' \\
      --keywords 'keyword1, keyword2, keyword3' \\
      --priority 10 \\
      --icon 🎯 \\
      --color '#3b82f6' \\
      --github https://github.com/user/repo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def gen_server_yaml(args) -> str:
    tools_yaml = '\n'.join(
        f'  - {{name: {t.split(":")[0]}, danger: {t.split(":")[1]}}}'
        for t in args.tools.split(',')
    ) if args.tools else '  # (no tools)'
    if args.env_vars:
        env_block = '  env_vars:\n' + '\n'.join(
            f'    - {{name: {ev.split(":")[0]}, level: {ev.split(":")[1]}}}'
            for ev in args.env_vars.split(',')
        )
    else:
        env_block = '  env_vars: []'
    args_list = json.loads(args.args) if args.args else []
    args_yaml = json.dumps(args_list)
    return f'''id: {args.id}
schema_version: "1.5.0"
title: {args.title!r}
description: {args.description!r}

command: {args.command}
args: {args_yaml}

requires:
  python_packages:
    - {{name: mcp, level: hard}}
{env_block}

mode: {args.mode}

tools:
{tools_yaml}

process:
  startup_timeout_seconds: 30
  restart_policy: on_failure
  max_restarts: 3
  backoff_seconds: [1, 2, 5, 15]

# GitHub: {args.github}
'''


def gen_agent_yaml(args) -> str:
    tools = [t.split(':')[0] for t in args.tools.split(',')] if args.tools else []
    dangerous = [t.split(':')[0] for t in args.tools.split(',')
                 if t.split(':')[1] in ('destructive', 'write')] if args.tools else []
    keywords = ', '.join(repr(k.strip()) for k in args.keywords.split(',')) if args.keywords else '[]'
    if args.env_vars:
        env_block = '  env_vars:\n' + '\n'.join(
            f'    - {{name: {ev.split(":")[0]}, level: {ev.split(":")[1]}}}'
            for ev in args.env_vars.split(',')
        )
    else:
        env_block = '  env_vars: []'
    return f'''id: {args.id}
schema_version: "1.5.0"
title: {args.title.replace(" MCP", " агент")!r}
version: "1.0.0"
description: {args.description!r}

requires:
  python_packages:
    - {{name: mcp, level: hard}}
{env_block}

mcp_servers: [{args.id}]
depends_on: {{hard: [], soft: []}}

priority: {args.priority}
routing_hints:
  keywords: [{keywords}]
  negative_keywords: []
  description_for_router: |
    {args.description}

mode: {args.mode}
{'dangerous_tools: ' + str(dangerous) if dangerous else 'dangerous_tools: []'}

prompt: prompt.md
user_template: user.md

runtime: {{max_steps: 15, max_result_chars: 30000, timeout_seconds: 600}}
ui: {{icon: "{args.icon}", color: "{args.color}", category: {args.category}}}

# GitHub: {args.github}
'''


def gen_prompt_md(args) -> str:
    tools_list = '\n'.join(
        f'- **{t.split(":")[0]}** ({t.split(":")[1]}): операция'
        for t in args.tools.split(',')
    ) if args.tools else '- (no tools)'
    return f'''Ты — эксперт-агент по работе с **{args.title}**.

## Доступные инструменты (MCP-сервер `{args.id}`):

{tools_list}

## Правила:

1. Прежде чем выполнять деструктивные операции — спроси подтверждение.
2. Для write-операций показывай, что именно будет изменено.
3. Возвращай результат в структурированном виде: JSON или Markdown-таблица.
4. Если API возвращает ошибку — объясни её понятным языком.

## Контекст:

- MCP: `{args.id}` ({args.title})
- Режим: `{args.mode}`
- Категория: `{args.category}`
- GitHub: {args.github}
'''


def gen_user_md(args) -> str:
    return f'''Задача от пользователя:
{{query}}

Контекст (опционально):
{{context}}

---
*Агент: {args.id} ({args.title})*
'''


def gen_native_server_py(args) -> str:
    """Шаблон нативного Python MCP-сервера (mcp 1.x контракт)."""
    return f'''# -*- coding: utf-8 -*-
"""{args.title} — native Python MCP server.

Создано через scripts/add_mcp_server.py.

Tools:
{chr(10).join(f"  - {t.split(':')[0]}" for t in args.tools.split(",")) if args.tools else "  - (no tools)"}
"""
from __future__ import annotations

import logging
import os

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

app = Server("{args.id}-mcp")


@app.list_tools()
async def list_tools() -> list:
    """Список инструментов (mcp 1.x контракт)."""
    from mcp.types import Tool
    return [
        # TODO: добавить Tool() для каждого инструмента
        Tool(
            name="placeholder_tool",
            description="Replace with real tool",
            inputSchema={{"type": "object", "properties": {{}}}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    """Выполнение инструмента (mcp 1.x контракт)."""
    from mcp.types import TextContent
    import json
    logger.info("call_tool %s args=%s", name, arguments)
    # TODO: реализовать логику инструмента
    return [TextContent(type="text", text=json.dumps({{"ok": True, "tool": name, "args": arguments}}))]


async def main():
    """Точка входа MCP-сервера (stdio)."""
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="{args.id}-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
'''


def main() -> int:
    p = argparse.ArgumentParser(description="Добавить новый MCP-сервер + агент")
    p.add_argument("--id", required=True, help="Уникальный ID (lowercase, no spaces)")
    p.add_argument("--title", required=True, help="Человекочитаемое название")
    p.add_argument("--description", required=True, help="Краткое описание")
    p.add_argument("--category", required=True,
                   help="Категория: database, cloud, comms, pm, monitoring, ...")
    p.add_argument("--command", default="python",
                   help="Команда запуска (python/npx/uvx)")
    p.add_argument("--args", default='[]', help="JSON-список аргументов")
    p.add_argument("--env-vars", default="",
                   help="Переменные окружения: NAME:level,NAME2:level2")
    p.add_argument("--mode", default="read",
                   choices=["read", "write", "destructive"])
    p.add_argument("--tools", default="",
                   help="Инструменты: name1:danger1,name2:danger2 (danger: read/write/destructive)")
    p.add_argument("--keywords", default="",
                   help="Ключевые слова для роутинга (через запятую)")
    p.add_argument("--priority", type=int, default=10, help="Приоритет агента 1-20")
    p.add_argument("--icon", default="🎯", help="Emoji-иконка")
    p.add_argument("--color", default="#3b82f6", help="Hex цвет")
    p.add_argument("--github", default="", help="GitHub URL репозитория")
    p.add_argument("--native", action="store_true",
                   help="Создать native Python server.py (mcp 1.x)")
    p.add_argument("--root", default=".",
                   help="Корень проекта (по умолчанию текущий каталог)")
    args = p.parse_args()

    root = Path(args.root).resolve()
    if not (root / "README.md").is_file():
        print(f"❌ README.md не найден в {root} — запустите из корня llm-agent", file=sys.stderr)
        return 1

    # mcp_servers/<id>/server.yaml
    mcp_dir = root / "mcp_servers" / args.id
    mcp_dir.mkdir(parents=True, exist_ok=True)
    (mcp_dir / "server.yaml").write_text(gen_server_yaml(args), encoding="utf-8")
    print(f"✓ {mcp_dir}/server.yaml")

    # agents/<id>/{agent.yaml, prompt.md, user.md}
    agent_dir = root / "agents" / args.id
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.yaml").write_text(gen_agent_yaml(args), encoding="utf-8")
    (agent_dir / "prompt.md").write_text(gen_prompt_md(args), encoding="utf-8")
    (agent_dir / "user.md").write_text(gen_user_md(args), encoding="utf-8")
    print(f"✓ {agent_dir}/agent.yaml")
    print(f"✓ {agent_dir}/prompt.md")
    print(f"✓ {agent_dir}/user.md")

    # src/mcp_servers/<id>/{__init__.py, server.py} если --native
    if args.native:
        native_dir = root / "src" / "mcp_servers" / args.id
        native_dir.mkdir(parents=True, exist_ok=True)
        (native_dir / "__init__.py").write_text("", encoding="utf-8")
        (native_dir / "server.py").write_text(gen_native_server_py(args), encoding="utf-8")
        print(f"✓ {native_dir}/server.py (native Python)")

    print()
    print("🎉 MCP-сервер добавлен!")
    print()
    print("Следующие шаги:")
    print(f"  1. Заполните {mcp_dir}/server.yaml если нужно")
    print(f"  2. Включите MCP в config/settings.yaml (mcp_servers.{args.id}: ...)")
    print(f"  3. Заполните env vars в .env (см. mcp_servers/{args.id}/server.yaml)")
    if args.native:
        print(f"  4. Реализуйте инструменты в src/mcp_servers/{args.id}/server.py")
        print(f"  5. Прогоните контракт-тест: pytest tests/test_mcp_1x_contract.py -v -k {args.id}")
    print(f"  6. Перезапустите сервер: python run.py")
    print(f"  7. Проверьте: curl http://127.0.0.1:8000/api/registry/mcp | jq '.[] | select(.id==\"{args.id}\")'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

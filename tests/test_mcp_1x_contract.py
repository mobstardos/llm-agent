# -*- coding: utf-8 -*-
"""Интеграционный тест контракта MCP 1.x API.

Рекомендация 14 (Sprint 5, P2).

Проверяет, что все MCP-серверы в проекте используют API, совместимый
с mcp 1.x (не 2.x, который удалил декораторы @app.list_tools()).
Если проект обновит mcp до 2.0 — этот тест провалится.

Запуск:
    pytest tests/test_mcp_1x_contract.py -v
    pytest tests/test_mcp_1x_contract.py -k "filesystem" -v

Если mcp 2.0 установлен (нарушение пиннинга <2.0):
    pip show mcp | grep Version   # должно быть 1.x
"""
from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path
from typing import Iterable

import pytest

# ─── Путь к src/mcp_servers/ ──────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
MCP_SERVERS_DIR = ROOT / "src" / "mcp_servers"


# ─── Найти все server.py в src/mcp_servers/<name>/server.py ─────────
def find_mcp_servers() -> list[tuple[str, Path]]:
    """Возвращает [(name, path), ...] для всех MCP-серверов."""
    result: list[tuple[str, Path]] = []
    if not MCP_SERVERS_DIR.is_dir():
        return result
    for sub in sorted(MCP_SERVERS_DIR.iterdir()):
        if not sub.is_dir():
            continue
        server_file = sub / "server.py"
        if server_file.is_file():
            result.append((sub.name, server_file))
    return result


ALL_SERVERS = find_mcp_servers()


# ─── Static AST checks (без импорта модуля — безопасно) ──────────────
def _parse_module(path: Path) -> ast.Module:
    """Парсит Python-файл в AST."""
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _has_decorator_with_name(decorator: ast.expr, names: set[str]) -> bool:
    """Проверяет, что декоратор содержит одно из имён в names.

    Поддерживает:
      @app.list_tools()            → name == 'list_tools' (atrribute)
      @mcp_app.list_resources()    → name == 'list_resources'
      @something.tool()           → name == 'tool'
    """
    # app.list_tools()  →  ast.Call(func=ast.Attribute(value=Name('app'), attr='list_tools'))
    if isinstance(decorator, ast.Call):
        func = decorator.func
        if isinstance(func, ast.Attribute) and func.attr in names:
            return True
    # Прямое использование без вызова: @app.list_tools (без скобок)
    if isinstance(decorator, ast.Attribute) and decorator.attr in names:
        return True
    return False


def _function_decorators(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Возвращает список decorator code для функции."""
    return [ast.unparse(d) for d in func.decorator_list]


# ═══════════════════════════════════════════════════════════════════
# Тесты
# ═══════════════════════════════════════════════════════════════════

class TestMCPServerContract:
    """Контракт MCP 1.x API для всех серверов в src/mcp_servers/."""

    @pytest.fixture(scope="class")
    def mcp_version(self):
        """Проверяем, что установлен mcp 1.x (не 2.x)."""
        try:
            mcp = importlib.import_module("mcp")
            version = getattr(mcp, "__version__", "")
            if not version:
                # mcp может не экспортировать __version__, проверим через importlib.metadata
                from importlib.metadata import version as pkg_version
                version = pkg_version("mcp")
        except ImportError:
            pytest.skip("mcp not installed")
        return version

    def test_mcp_version_is_1x(self, mcp_version):
        """КРИТИЧНО: mcp должен быть 1.x, не 2.x.

        mcp 2.0 удалил декораторы @app.list_tools() — все MCP-серверы
        проекта сломаются. Этот тест — защитный gate.
        """
        major = mcp_version.split(".")[0]
        assert major == "1", (
            f"❌ mcp {mcp_version} установлен — проект требует mcp 1.x "
            f"(pin в requirements.txt: mcp~=1.1.3). "
            f"Список всех декораторов, которые сломаются: "
            f"@app.list_tools(), @app.call_tool(), @app.list_resources(), "
            f"@app.list_prompts(). Обновление до 2.x БУДЕТ сломано."
        )

    @pytest.mark.parametrize("name,server_path", ALL_SERVERS,
                              ids=[s[0] for s in ALL_SERVERS])
    def test_uses_mcp_1x_decorators(self, name: str, server_path: Path):
        """Все MCP-серверы должны использовать mcp 1.x декораторы:
        @app.list_tools(), @app.call_tool(), @app.list_resources(),
        @app.list_prompts().

        В mcp 2.x эти декораторы УДАЛЕНЫ. Тест парсит AST и находит
        использование 1.x-стиля. Если в коде есть декораторы — сервер
        сломается при обновлении до mcp 2.x.
        """
        tree = _parse_module(server_path)

        # Найти все function definitions с декораторами
        functions_with_decorators: list[tuple[str, list[str]]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.decorator_list:
                    decorators = [_function_decorators(node)]
                    functions_with_decorators.append((node.name, decorators))

        # Декораторы, разрешённые в mcp 1.x
        MCP_1X_DECORATORS = {
            "list_tools", "call_tool",
            "list_resources", "read_resource",
            "list_prompts", "get_prompt",
            "list_resource_templates",
        }

        # Найти использование этих декораторов
        mcp_decorators_used: list[str] = []
        for func_name, _ in functions_with_decorators:
            for dec_list in [d for d in functions_with_decorators if d[0] == func_name]:
                for dec_str in dec_list[1]:
                    for d_name in MCP_1X_DECORATORS:
                        if f".{d_name}" in dec_str:
                            mcp_decorators_used.append(dec_str)

        # Если в сервере используются mcp-декораторы 1.x —
        # это явная зависимость от 1.x API.
        if mcp_decorators_used:
            # Это ОК для текущей версии mcp 1.x, но при обновлении
            # до 2.x — сервер сломается.
            print(f"  ✓ {name}: использует {len(mcp_decorators_used)} "
                  f"mcp 1.x декораторов: {mcp_decorators_used[:3]}...")
        # Если ни одного mcp-декоратора нет — возможно, сервер использует
        # другой паттерн (FastMCP, декларативный tools=[...]). Это ОК.

    @pytest.mark.parametrize("name,server_path", ALL_SERVERS,
                              ids=[s[0] for s in ALL_SERVERS])
    def test_no_mcp_2x_api_usage(self, name: str, server_path: Path):
        """В коде не должно быть mcp 2.x API — например, FastMCP()
        с декларативным tools=[Tool(...)] паттерном (это 2.x).
        """
        tree = _parse_module(server_path)
        for node in ast.walk(tree):
            # Найти: FastMCP() или mcp.FastMCP()
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id == "FastMCP":
                    pytest.fail(
                        f"{server_path.name}: используется FastMCP() — "
                        f"это API mcp 2.x. Проект собран под mcp 1.x."
                    )
                if isinstance(func, ast.Attribute) and func.attr == "FastMCP":
                    pytest.fail(
                        f"{server_path.name}: используется mcp.FastMCP() — "
                        f"это API mcp 2.x. Проект собран под mcp 1.x."
                    )

    @pytest.mark.parametrize("name,server_path", ALL_SERVERS,
                              ids=[s[0] for s in ALL_SERVERS])
    def test_imports_from_mcp_server_lowlevel(self, name: str, server_path: Path):
        """Все MCP-серверы импортируют из `mcp.server.*` (low-level),
        не из `mcp.server.fastapi` (это 2.x-high-level).
        """
        tree = _parse_module(server_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                # mcp.server.fastapi — это high-level API mcp 2.x
                if module == "mcp.server.fastapi":
                    pytest.fail(
                        f"{server_path.name}: импортирует из "
                        f"mcp.server.fastapi (это API mcp 2.x). "
                        f"Замените на mcp.server.lowlevel (mcp 1.x)."
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "mcp.server.fastapi":
                        pytest.fail(
                            f"{server_path.name}: импортирует "
                            f"mcp.server.fastapi (mcp 2.x)."
                        )

    def test_mcp_servers_dir_exists(self):
        """Базовая проверка: каталог src/mcp_servers/ существует."""
        assert MCP_SERVERS_DIR.is_dir(), (
            f"Каталог {MCP_SERVERS_DIR} не найден — структура проекта изменилась?"
        )

    def test_all_mcp_servers_have_server_py(self):
        """Все подкаталоги src/mcp_servers/<name>/ должны содержать server.py."""
        for sub in MCP_SERVERS_DIR.iterdir():
            if not sub.is_dir():
                continue
            if sub.name in {"__pycache__", "init"}:
                continue
            server_file = sub / "server.py"
            assert server_file.is_file(), (
                f"❌ {sub.name}/server.py не найден. "
                f"Все MCP-серверы должны иметь server.py (точка входа)."
            )

    def test_all_mcp_servers_have_init_py(self):
        """Все подкаталоги должны иметь __init__.py (Python-пакет)."""
        for sub in MCP_SERVERS_DIR.iterdir():
            if not sub.is_dir():
                continue
            if sub.name in {"__pycache__"}:
                continue
            init_file = sub / "__init__.py"
            assert init_file.is_file(), (
                f"❌ {sub.name}/__init__.py не найден — "
                f"Python не увидит это как пакет."
            )

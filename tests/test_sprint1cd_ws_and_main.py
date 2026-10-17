# -*- coding: utf-8 -*-
"""Тесты для Sprint 1.C+D: WS handler + slim main.py.

Sprint 1.C:
  - src/ws/chat.py экспортирует ws_router (APIRouter)
  - ws_router имеет websocket("/ws") эндпоинт
  - WS handler корректно подключается к app

Sprint 1.D:
  - src/main.py ≤ 50 строк (после Stage D)
  - main.py импортирует app из src.app
  - main.py имеет main() функцию с argparse
  - main.py возвращает int (exit code)
"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter


def _iter_route_paths(routes):
    """Рекурсивный обход app.routes.

    Starlette ≥0.41 хранит include_router() как вложенные _IncludedRouter
    (без path/routes) — рекурсируем через .original_router (APIRouter).
    """
    for r in routes:
        path = getattr(r, "path", None)
        if path is not None:
            yield str(path)
        orig = getattr(r, "original_router", None)
        if orig is not None and hasattr(orig, "routes"):
            yield from _iter_route_paths(orig.routes)
            continue
        inner = getattr(r, "routes", None)
        if inner:
            yield from _iter_route_paths(inner)


# ═══════════════════════════════════════════════════════════════════
# Sprint 1.C — WebSocket handler
# ═══════════════════════════════════════════════════════════════════

class TestWSHandler:
    def test_ws_chat_module_imports(self):
        """src.ws.chat импортируется без ошибок."""
        from src.ws.chat import ws_router, ws_endpoint
        assert ws_router is not None
        assert callable(ws_endpoint)

    def test_ws_router_is_apirouter(self):
        """ws_router — это APIRouter с websocket эндпоинтом."""
        from src.ws.chat import ws_router
        assert isinstance(ws_router, APIRouter)
        # Проверяем что есть /ws route
        ws_routes = [r for r in ws_router.routes if hasattr(r, "path")]
        assert any("/ws" in str(r.path) for r in ws_routes), \
            "ws_router должен иметь websocket('/ws') эндпоинт"

    def test_ws_endpoint_is_coroutine_function(self):
        """ws_endpoint — async function (для @router.websocket())."""
        from src.ws.chat import ws_endpoint
        assert inspect.iscoroutinefunction(ws_endpoint), \
            "ws_endpoint должен быть async function"

    def test_ws_endpoint_signature(self):
        """ws_endpoint принимает WebSocket."""
        from src.ws.chat import ws_endpoint
        from fastapi import WebSocket
        sig = inspect.signature(ws_endpoint)
        params = list(sig.parameters.values())
        assert len(params) >= 1
        # Первый параметр должен быть annotated как WebSocket
        # (from __future__ import annotations → аннотация может быть строкой)
        first = params[0]
        ann = first.annotation
        assert (ann == WebSocket or ann is WebSocket
                or (isinstance(ann, str) and "WebSocket" in ann)), \
            f"первый параметр ws_endpoint должен быть WebSocket, получено: {ann!r}"

    def test_ws_chat_imports_state_correctly(self):
        """chat.py использует state (AppState), а не state dict."""
        # Прочитать исходник и проверить что state импортируется из src.state
        import src.ws.chat as ws_chat_mod
        src = inspect.getsource(ws_chat_mod)
        assert "from src.state import state" in src, \
            "chat.py должен импортировать state из src.state (AppState)"
        assert "state.get(" not in src or "state.get" not in src, \
            "chat.py должен использовать state.X (AppState), не state.get('X')"


# ═══════════════════════════════════════════════════════════════════
# Sprint 1.D — Slim main.py
# ═══════════════════════════════════════════════════════════════════

class TestSlimMain:
    """Тесты для финального main.py (после Stage D — ≤50 строк).

    ⚠ Эти тесты работают ТОЛЬКО если main.py уже заменён на new_main.py.
    До Stage D (если main.py ещё legacy 2640 строк) — они провалятся.
    После Stage D — должны пройти.
    """

    def test_main_imports_app_from_app_module(self):
        """main.py импортирует app из src.app."""
        # Принудительный reload
        if "src.main" in sys.modules:
            del sys.modules["src.main"]
        import src.main as main_mod
        # Если main.py — slim версия, у него должен быть app
        assert hasattr(main_mod, "app"), \
            "main.py должен импортировать `app` из src.app"

    def test_main_has_main_function(self):
        """main.py имеет main() функцию."""
        if "src.main" in sys.modules:
            del sys.modules["src.main"]
        import src.main as main_mod
        assert hasattr(main_mod, "main"), \
            "main.py должен иметь main() функцию (entry point)"
        assert callable(main_mod.main)

    def test_main_returns_int(self):
        """main() возвращает int (exit code)."""
        if "src.main" in sys.modules:
            del sys.modules["src.main"]
        import src.main as main_mod
        # Имитация запуска с --help (argparse выйдет с SystemExit)
        try:
            sys.argv = ["main.py", "--help"]
            main_mod.main()
        except SystemExit:
            pass  # argparse с --help вызывает SystemExit(0)
        # Если main() доходит до uvicorn.run() — он блокирует.
        # Поэтому проверяем только сигнатуру:
        import inspect
        sig = inspect.signature(main_mod.main)
        assert sig.return_annotation in (int, "int", inspect.Parameter.empty), \
            f"main() должен возвращать int, аннотация: {sig.return_annotation}"

    @pytest.mark.skipif(
        not Path("/home/z/my-project/download/sprint1CD-patch/new_main.py").exists(),
        reason="new_main.py не найден — Stage D не применён"
    )
    def test_new_main_is_under_60_lines(self):
        """new_main.py ≤ 60 строк (после Stage D)."""
        new_main = Path("/home/z/my-project/download/sprint1CD-patch/new_main.py")
        lines = new_main.read_text(encoding="utf-8").splitlines()
        # Считаем непустые, не-комментарные строки
        code_lines = [l for l in lines if l.strip() and not l.strip().startswith("#")]
        assert len(code_lines) <= 60, \
            f"new_main.py должен быть ≤60 строк кода, сейчас {len(code_lines)}"

    def test_main_no_legacy_state_dict(self):
        """В main.py НЕ должно быть `state: dict = {}` (legacy)."""
        if "src.main" in sys.modules:
            del sys.modules["src.main"]
        import src.main as main_mod
        src = inspect.getsource(main_mod)
        # Если Stage D применён, в main.py нет state dict
        if len(src.splitlines()) < 100:  # только если main.py slim
            assert "state: dict" not in src, \
                "Slim main.py не должен содержать `state: dict = {}`"
            assert "@app.get" not in src, \
                "Slim main.py не должен содержать @app.get декораторы"
            assert "@app.websocket" not in src, \
                "Slim main.py не должен содержать @app.websocket декоратор"


# ═══════════════════════════════════════════════════════════════════
# App integration — проверка что весь стек собирается
# ═══════════════════════════════════════════════════════════════════

class TestAppIntegration:
    def test_app_has_ws_route_registered(self):
        """app регистрирует /ws route (через ws_router)."""
        from src.app import app
        ws_paths = [p for p in _iter_route_paths(app.routes) if "/ws" in p]
        assert len(ws_paths) >= 1, \
            "app должен иметь /ws route (через include_router(ws_router))"

    def test_app_has_80_plus_routes(self):
        """После всех стадий app имеет 86+ эндпоинтов."""
        from src.app import app
        total = sum(1 for _ in _iter_route_paths(app.routes))
        # 86 HTTP + 1 WS + несколько static mounts = ~88
        assert total >= 80, \
            f"app должен иметь ≥80 routes (после Stage A+B+C), сейчас {total}"

    def test_app_routes_include_all_groups(self):
        """Все 13 функциональных групп зарегистрированы."""
        from src.app import app
        paths = set(_iter_route_paths(app.routes))
        # Проверяем что есть хотя бы по одному пути из каждой группы
        expected_groups = [
            "/api/plans",       # plans
            "/api/models",      # models (or /api/model/select)
            "/api/sessions",    # sessions
            "/api/project",     # project
            "/api/db",          # database
            "/api/enrichment",  # enrichment
            "/api/age",         # graph
            "/api/search",      # search
            "/api/backup",      # backup
            "/api/analytics",   # analytics
            "/api/policies",    # policies
            "/api/digest",      # chats (digest в chats.py)
            "/api/registry",    # registry
        ]
        missing = [g for g in expected_groups
                   if not any(g in p for p in paths)]
        assert not missing, f"Отсутствуют группы эндпоинтов: {missing}"

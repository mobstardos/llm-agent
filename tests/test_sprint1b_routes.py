# -*- coding: utf-8 -*-
"""Smoke-тесты для 13 роутеров из Sprint 1.B.

Каждый тест проверяет:
  - роутер экспортирует `router` (APIRouter)
  - роутер имеет правильный prefix
  - минимальный набор эндпоинтов зарегистрирован
  - GET-эндпоинт возвращает 200 (с state initialized) или 503 (без state)

Запуск:
    pytest tests/test_sprint1b_routes.py -v
"""
from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from src.state import state


# ═══════════════════════════════════════════════════════════════════
# Базовая проверка — все роутеры импортируются и имеют router
# ═══════════════════════════════════════════════════════════════════

ROUTERS = [
    ("src.routes.plans", "router"),
    ("src.routes.models", "router"),
    ("src.routes.sessions", "router"),
    ("src.routes.project", "router"),
    ("src.routes.database", "router"),
    ("src.routes.enrichment", "router"),
    ("src.routes.graph", "router"),
    ("src.routes.search", "router"),
    ("src.routes.backup", "router"),
    ("src.routes.analytics", "router"),
    ("src.routes.policies", "router"),
    ("src.routes.chats", "router"),
    ("src.routes.registry", "router"),
]


@pytest.mark.parametrize("module_name,attr", ROUTERS,
                          ids=[m.split(".")[-1] for m, _ in ROUTERS])
def test_router_module_exports_router(module_name: str, attr: str):
    """Каждый модуль экспортирует `router` (APIRouter)."""
    import importlib
    mod = importlib.import_module(module_name)
    assert hasattr(mod, attr), f"{module_name} не имеет атрибута {attr}"
    router = getattr(mod, attr)
    assert isinstance(router, APIRouter), f"{module_name}.router не APIRouter"
    assert len(router.routes) > 0, f"{module_name}.router не имеет эндпоинтов"


# ═══════════════════════════════════════════════════════════════════
# Проверка prefix и tagов
# ═══════════════════════════════════════════════════════════════════

EXPECTED_PREFIXES = {
    "plans": "/api/plans",
    "models": "/api/model",
    "sessions": "/api/sessions",
    "project": "/api/project",
    "database": "/api/db",
    "enrichment": "/api/enrichment",
    "graph": "/api",
    "search": "/api",
    "backup": "/api/backup",
    "analytics": "/api/analytics",
    "policies": "/api/policies",
    "chats": "/api",
    "registry": "/api/registry",
}


@pytest.mark.parametrize("module_short,expected_prefix",
                          list(EXPECTED_PREFIXES.items()),
                          ids=list(EXPECTED_PREFIXES.keys()))
def test_router_has_correct_prefix(module_short: str, expected_prefix: str):
    """Каждый роутер имеет правильный prefix."""
    import importlib
    mod = importlib.import_module(f"src.routes.{module_short}")
    assert mod.router.prefix == expected_prefix, (
        f"{module_short}.router.prefix={mod.router.prefix!r}, "
        f"ожидался {expected_prefix!r}"
    )


# ═══════════════════════════════════════════════════════════════════
# End-to-end smoke — все роутеры регистрируются в app без ошибок
# ═══════════════════════════════════════════════════════════════════

def test_app_with_all_routers_starts_without_errors():
    """FastAPI app с 16 роутерами инициализируется без ImportError/circular."""
    from src.app import app
    assert app is not None
    # Подсчитать количество эндпоинтов
    total_routes = len(app.routes)
    # Минимум: 13 роутеров × 2 эндпоинта в среднем + static (5) + cache (1) + features (1) = ~46
    # Реально: 86 эндпоинтов + WS = ~87
    assert total_routes > 40, f"Слишком мало routes: {total_routes}"


# ═══════════════════════════════════════════════════════════════════
# Functional smoke — каждый роутер отвечает на базовый запрос
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def client():
    """TestClient с подключёнными всеми роутерами Sprint 1.B."""
    from src.app import app
    return TestClient(app)


def test_plans_returns_list_when_uninitialized(client):
    """GET /api/plans возвращает {"plans": []} если plans=None."""
    original = state.plans
    state.plans = None
    try:
        r = client.get("/api/plans")
        assert r.status_code == 200
        assert r.json() == {"plans": []}
    finally:
        state.plans = original


def test_models_returns_default_fallback(client):
    """GET /api/models возвращает список моделей (возможно fallback)."""
    r = client.get("/api/models")
    # 200 даже если провайдеры недоступны (fallback в коде)
    assert r.status_code in (200, 503)
    if r.status_code == 200:
        data = r.json()
        assert "models" in data
        assert "providers" in data


def test_sessions_returns_list(client):
    """GET /api/sessions возвращает {"sessions": [...]}."""
    r = client.get("/api/sessions")
    assert r.status_code in (200, 503)


def test_database_health_when_pg_unconfigured(client):
    """GET /api/db/health возвращает {"enabled": False} без PG."""
    original_memory = state.memory
    state.memory = None
    try:
        r = client.get("/api/db/health")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}
    finally:
        state.memory = original_memory


def test_enrichment_when_pg_unconfigured(client):
    """GET /api/enrichment/status возвращает PG-not-configured."""
    original_memory = state.memory
    state.memory = None
    try:
        r = client.get("/api/enrichment/status")
        assert r.status_code == 200
        assert r.json() == {"enabled": False, "reason": "PostgreSQL not configured"}
    finally:
        state.memory = original_memory


def test_graph_age_when_disabled(client):
    """GET /api/age/stats возвращает {"enabled": False} без AGE."""
    original = state.age_store
    state.age_store = None
    try:
        r = client.get("/api/age/stats")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}
    finally:
        state.age_store = original


def test_search_events_when_journal_unconfigured(client):
    """GET /api/search/events возвращает {"events": [], "enabled": False}."""
    original = state.journal
    state.journal = None
    try:
        r = client.get("/api/search/events?q=test")
        assert r.status_code == 200
        assert r.json() == {"events": [], "enabled": False}
    finally:
        state.journal = original


def test_backup_when_manager_unconfigured(client):
    """GET /api/backup/list возвращает {"backups": [], "enabled": False}."""
    original = state.backup_manager
    state.backup_manager = None
    try:
        r = client.get("/api/backup/list")
        assert r.status_code == 200
        assert r.json() == {"backups": [], "enabled": False}
    finally:
        state.backup_manager = original


def test_analytics_when_pg_unconfigured(client):
    """GET /api/analytics/overview возвращает {"enabled": False} без PG."""
    original = state.memory
    state.memory = None
    try:
        r = client.get("/api/analytics/overview")
        assert r.status_code == 200
        assert r.json() == {"enabled": False}
    finally:
        state.memory = original


def test_policies_returns_503_when_uninitialized(client):
    """GET /api/policies возвращает 503 если policies=None."""
    original = state.policies
    state.policies = None
    try:
        r = client.get("/api/policies")
        assert r.status_code == 503
    finally:
        state.policies = original


def test_chats_digest_when_micro_worker_none(client):
    """GET /api/digest/latest возвращает enabled=False если micro_worker=None."""
    original = state.micro_worker
    state.micro_worker = None
    try:
        r = client.get("/api/digest/latest")
        assert r.status_code == 200
        data = r.json()
        assert data["enabled"] is False
    finally:
        state.micro_worker = original


def test_registry_returns_503_when_uninitialized(client):
    """GET /api/registry/snapshot возвращает 503 если registry=None."""
    original = state.registry
    state.registry = None
    try:
        r = client.get("/api/registry/snapshot")
        assert r.status_code == 503
    finally:
        state.registry = original

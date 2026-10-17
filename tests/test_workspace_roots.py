# -*- coding: utf-8 -*-
"""Фаза 3: whitelist рабочих папок WORKSPACE_ROOTS.

Юнит: src/workspace.py (разбор env, is_allowed, ensure_workspace_dirs).
API: GET /api/project (workspace_roots), POST /api/project/set (403 вне
белого списка), POST /api/project/select-dialog (options).
"""
from __future__ import annotations

from pathlib import Path

import pytest


# ═══════════════════════════════════════════════════════════════════
# Юнит: src/workspace.py
# ═══════════════════════════════════════════════════════════════════

def test_roots_default_only(monkeypatch, tmp_path):
    """Без WORKSPACE_ROOTS — только дефолтный корень."""
    from src.workspace import workspace_roots
    monkeypatch.delenv("WORKSPACE_ROOTS", raising=False)
    roots = workspace_roots(str(tmp_path))
    assert roots == [tmp_path.resolve()]


def test_roots_multiple(monkeypatch, tmp_path):
    """WORKSPACE_ROOTS: два корня через ';' + запятые."""
    from src.workspace import workspace_roots
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("WORKSPACE_ROOTS", f"{a}; {b}")
    roots = workspace_roots(str(tmp_path))
    assert a.resolve() in roots and b.resolve() in roots
    assert tmp_path.resolve() in roots  # дефолт всегда включается


def test_roots_missing_dir_skipped(monkeypatch, tmp_path, caplog):
    """Несуществующая папка из env пропускается с warning."""
    from src.workspace import workspace_roots
    missing = tmp_path / "nope"
    monkeypatch.setenv("WORKSPACE_ROOTS", str(missing))
    roots = workspace_roots(str(tmp_path))
    assert missing.resolve() not in roots
    assert roots == [tmp_path.resolve()]


def test_is_allowed_inside_and_outside(monkeypatch, tmp_path):
    """Подпапка внутри корня — allowed; чужая — нет."""
    from src.workspace import is_allowed
    monkeypatch.delenv("WORKSPACE_ROOTS", raising=False)
    inside = tmp_path / "sub" / "deep"
    inside.mkdir(parents=True)
    assert is_allowed(inside, str(tmp_path)) is True
    assert is_allowed(tmp_path, str(tmp_path)) is True
    # снаружи — но несуществующий путь (resolve работает, relative_to нет)
    assert is_allowed(Path("C:\\definitely_not_here_xyz\\x"),
                      str(tmp_path)) is False


def test_is_allowed_symlink_escape_blocked(monkeypatch, tmp_path):
    """Симлинк наружу — resolve() разворачивает, проверка не пропускает."""
    from src.workspace import is_allowed
    monkeypatch.delenv("WORKSPACE_ROOTS", raising=False)
    outside = tmp_path.parent / f"{tmp_path.name}_outside"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("нет прав на создание symlink")
    assert is_allowed(link, str(tmp_path)) is False


def test_ensure_workspace_dirs_creates(tmp_path):
    """ensure_workspace_dirs создаёт .inbox/ и outputs/."""
    from src.workspace import INBOX_DIR, OUTPUTS_DIR, ensure_workspace_dirs
    made = ensure_workspace_dirs(tmp_path)
    assert (tmp_path / INBOX_DIR).is_dir()
    assert (tmp_path / OUTPUTS_DIR).is_dir()
    assert len(made) == 2


def test_prompt_line(monkeypatch, tmp_path):
    """workspace_prompt_line содержит путь и подпапки."""
    from src.workspace import workspace_prompt_line
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    line = workspace_prompt_line()
    assert str(tmp_path.resolve()) in line
    assert ".inbox" in line and "outputs" in line


# ═══════════════════════════════════════════════════════════════════
# API: /api/project + whitelist
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def client(monkeypatch, tmp_path):
    """TestClient с роутером project (без lifespan)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # сначала полная инициализация src.app (иначе цикл import через
    # app.include_router(routes_project.router))
    import src.app  # noqa: F401
    from src.routes import project as project_routes
    from src import runtime_config

    inside = tmp_path / "ws_inside"
    inside.mkdir()
    monkeypatch.setenv("PROJECT_ROOT", str(inside))
    monkeypatch.setenv("WORKSPACE_ROOTS", str(inside))
    # set_project_root пишет data/runtime.json — перенаправляем в tmp
    monkeypatch.setattr(runtime_config, "RUNTIME_PATH",
                        tmp_path / "runtime.json")
    # get_settings() под lru_cache — подменяем на фейк с нашим root
    class _S:
        project_root = str(inside)
    monkeypatch.setattr(project_routes, "get_settings", lambda: _S())
    app = FastAPI()
    app.include_router(project_routes.router)
    return TestClient(app)


def test_get_includes_workspace_roots(client):
    r = client.get("/api/project")
    assert r.status_code == 200
    d = r.json()
    assert "workspace_roots" in d
    assert len(d["workspace_roots"]) >= 1


def test_set_inside_allowed(client, tmp_path):
    inside = tmp_path / "ws_inside" / "sub"
    inside.mkdir(parents=True)
    r = client.post("/api/project/set", json={"path": str(inside)})
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_set_outside_rejected_403(client, tmp_path):
    outside = tmp_path / "other_project"
    outside.mkdir()
    r = client.post("/api/project/set", json={"path": str(outside)})
    assert r.status_code == 403
    assert "WORKSPACE_ROOTS" in r.json()["detail"]


def test_set_empty_path_400(client):
    r = client.post("/api/project/set", json={"path": ""})
    assert r.status_code == 400


def test_set_missing_dir_400(client, tmp_path):
    r = client.post("/api/project/set",
                    json={"path": str(tmp_path / "no_such_dir")})
    assert r.status_code == 400


def test_select_dialog_options(client, tmp_path):
    r = client.post("/api/project/select-dialog")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert str((tmp_path / "ws_inside").resolve()) in d["options"]

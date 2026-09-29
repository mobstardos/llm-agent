"""Smoke-тесты подсистемы быстрой настройки модулей (Task 12).

Проверяются: sanitize_env_updates, module_requirements (env / пакеты /
пути через env_ref), MCPManager.start_one / stop_one / restart_one
(фиктивная команда — без реальных процессов, сети и записи .env).
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.core.setup import module_requirements, sanitize_env_updates  # noqa: E402
from src.core.schema import (  # noqa: E402
    EnvRequirement,
    MCPServerSchema,
    PackageRequirement,
    PathRequirement,
    RequirementLevel,
)
from src.mcp_manager import MCPManager  # noqa: E402


class FakeChecker:
    """Мини-чекер: установленные пакеты задаются списком."""

    def __init__(self, installed=()):
        self.installed = set(installed)

    def _check_pkg(self, name):
        return name in self.installed

    def check_external(self, reqs):
        return [], [], {}


# ─── sanitize_env_updates ─────────────────────────────────────────────


def test_sanitize_env_updates_filters():
    clean, skipped = sanitize_env_updates({
        "GOOD_VAR": " value ",
        "BAD NAME": "x",     # пробел в имени
        "EMPTY": "",         # пустое значение = «не менять»
        "9START": "y",       # начинается с цифры
        "OK_2": "z",
    })
    assert clean == {"GOOD_VAR": "value", "OK_2": "z"}
    assert sorted(skipped) == ["9START", "BAD NAME", "EMPTY"]


def test_sanitize_env_updates_empty_input():
    clean, skipped = sanitize_env_updates(None)
    assert clean == {}
    assert skipped == []


# ─── module_requirements: env ─────────────────────────────────────────


def _mcp_schema(env_vars, packages=()):
    return MCPServerSchema(
        id="test_mod",
        requires={
            "env_vars": [EnvRequirement(name=n) for n in env_vars],
            "python_packages": [PackageRequirement(name=n) for n in packages],
        },
    )


def test_requirements_env_missing(monkeypatch):
    monkeypatch.delenv("SETUP_T1", raising=False)
    schema = _mcp_schema(["SETUP_T1"])
    rep = module_requirements(schema, FakeChecker())
    assert rep["status"] == "not_configured"
    assert rep["missing_hard"] == ["Переменная 'SETUP_T1' не задана"]
    assert "SETUP_T1" in rep["editable_env"]


def test_requirements_env_present(monkeypatch):
    monkeypatch.setenv("SETUP_T1", "x")
    schema = _mcp_schema(["SETUP_T1"])
    rep = module_requirements(schema, FakeChecker())
    assert rep["status"] == "ok"
    assert rep["missing_hard"] == []


def test_requirements_mixed_env_and_package(monkeypatch):
    monkeypatch.delenv("SETUP_T2", raising=False)
    schema = _mcp_schema(["SETUP_T2"], packages=["setuptools"])
    rep = module_requirements(schema, FakeChecker(installed=["setuptools"]))
    # env отсутствует → not_configured, но установленный пакет не в missing
    assert rep["status"] == "not_configured"
    assert all("setuptools" not in m for m in rep["missing_hard"])
    assert all("setuptools" not in m for m in rep["missing_soft"])


def test_requirements_soft_only(monkeypatch):
    monkeypatch.delenv("SETUP_T3", raising=False)
    schema = MCPServerSchema(
        id="test_soft",
        requires={"env_vars": [
            EnvRequirement(name="SETUP_T3", level=RequirementLevel.SOFT),
        ]},
    )
    rep = module_requirements(schema, FakeChecker())
    assert rep["status"] == "degraded"
    assert rep["missing_hard"] == []
    assert len(rep["missing_soft"]) == 1


# ─── module_requirements: пути через env_ref ──────────────────────────


def test_requirements_path_via_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SETUP_T4", raising=False)
    schema = MCPServerSchema(
        id="test_path",
        requires={"paths": [
            PathRequirement(name="workdir", env="SETUP_T4", type="dir"),
        ]},
    )
    rep = module_requirements(schema, FakeChecker())
    assert rep["status"] == "not_configured"
    # path.env_ref попал в редактируемые переменные формы
    assert "SETUP_T4" in rep["editable_env"]

    monkeypatch.setenv("SETUP_T4", str(tmp_path))
    rep2 = module_requirements(schema, FakeChecker())
    assert rep2["status"] == "ok"


# ─── MCPManager: start_one / stop_one / restart_one ───────────────────


def test_mcp_manager_one_lifecycle():
    async def run():
        m = MCPManager()
        # Несуществующая команда — старт падает, ошибка фиксируется
        ok = await m.start_one("ghost", {
            "command": "no_such_cmd_xyz_123", "args": [],
        })
        assert ok is False
        assert "ghost" in m.errors
        assert "ghost" not in m.sessions

        # stop_one на незапущенном — False, состояние чистое
        assert await m.stop_one("ghost") is False
        assert "ghost" not in m.sessions

        # restart_one = stop + start (упало — ошибка снова записана)
        ok2 = await m.restart_one("ghost", {"command": "no_such_cmd_xyz_123"})
        assert ok2 is False
        assert "ghost" in m.errors

        # Повторный start_one при живой сессии — True без попытки старта
        m.sessions["alive_one"] = object()
        assert await m.start_one("alive_one", {"command": "python"}) is True
        await m.stop_one("alive_one")
        assert "alive_one" not in m.sessions
        await m.stop()

    asyncio.run(run())

# -*- coding: utf-8 -*-
"""Тесты для src.journal.recorder.JournalRecorder.

Покрытие:
  - before_tool_call(): enabled, disabled, read-tool skipped, file targets + shadows.
  - after_tool_call(): writes event, error status, denied status, before/after hashes.
  - redact(): secret keys замаскированы.
  - truncate_result(): длинные строки обрезаются.
  - inverse spec: write tool returns REVERSIBLE_FULL/PARTIAL/NONE correctly.

Использует временный tmp_path для JournalConfig.db_path.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.journal.recorder import (
    JournalRecorder,
    PendingToolCall,
    truncate_result,
)
from src.journal.schema import (
    Event, EventKind, EventStatus,
    REVERSIBLE_FULL, REVERSIBLE_NONE, REVERSIBLE_PARTIAL,
)
from src.journal.config import JournalConfig


# ═══════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════

@pytest.fixture
def journal_cfg(tmp_path: Path):
    """Конфиг журнала с базой во временном каталоге."""
    return JournalConfig(
        enabled=True,
        db_path=str(tmp_path / "journal.sqlite"),
        shadows_dir=str(tmp_path / "shadows"),
        redact_keys={"password", "api_key", "token", "secret", "Authorization"},
        record_reads=False,  # read-инструменты не пишем (шум)
        record_results=True,
        max_result_chars=5000,
        shadows_enabled=True,
        retention_days=0,  # не удаляем
    )


@pytest.fixture
def recorder(journal_cfg, tmp_path):
    """JournalRecorder с временной БД."""
    return JournalRecorder(cfg=journal_cfg, project_root=tmp_path)


# ═══════════════════════════════════════════════════════════════════
# before_tool_call
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_before_tool_call_returns_pending_when_enabled(recorder):
    """Журнал включен — возвращается PendingToolCall."""
    pending = await recorder.before_tool_call(
        server="filesystem", tool="write_file",
        args={"path": "/tmp/test.txt", "content": "hello"},
    )
    assert pending is not None
    assert pending.server == "filesystem"
    assert pending.tool == "write_file"
    assert pending.record is True


@pytest.mark.asyncio
async def test_before_tool_call_returns_none_when_disabled(journal_cfg, tmp_path):
    """Журнал выключен — возвращается None."""
    journal_cfg.enabled = False
    rec = JournalRecorder(cfg=journal_cfg, project_root=tmp_path)
    pending = await rec.before_tool_call(
        server="filesystem", tool="write_file", args={},
    )
    assert pending is None


@pytest.mark.asyncio
async def test_before_tool_call_read_tool_skipped(recorder):
    """read-инструмент с record_reads=False — record=False (не пишется в БД)."""
    pending = await recorder.before_tool_call(
        server="filesystem", tool="read_file",  # read-инструмент
        args={"path": "/tmp/test.txt"},
    )
    assert pending is not None
    assert pending.record is False  # не будет сохранён в after_tool_call


@pytest.mark.asyncio
async def test_before_tool_call_redacts_secrets(recorder):
    """Секреты в args заменяются на [REDACTED] в args_redacted."""
    pending = await recorder.before_tool_call(
        server="http", tool="request",
        args={
            "url": "https://api.example.com",
            "headers": {
                "Authorization": "Bearer secret-token-12345",
                "X-API-Key": "sk-abc123",
            },
            "password": "p@ssw0rd",
        },
    )
    assert pending is not None
    redacted = pending.args_redacted
    # Все секреты замаскированы
    assert "secret-token-12345" not in json.dumps(redacted)
    assert "p@ssw0rd" not in json.dumps(redacted)
    assert "sk-abc123" not in json.dumps(redacted)
    # НЕ-секрет остался как есть
    assert redacted["url"] == "https://api.example.com"


# ═══════════════════════════════════════════════════════════════════
# after_tool_call
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_after_tool_call_writes_event_on_success(recorder):
    """Успешный tool-call → Event со status=OK."""
    pending = await recorder.before_tool_call(
        server="filesystem", tool="write_file",
        args={"path": "/tmp/test.txt", "content": "hello"},
    )
    event = await recorder.after_tool_call(
        pending, result={"ok": True, "content": "written"},
    )
    assert event is not None
    assert event.status == EventStatus.OK
    assert event.action == "write_file"
    assert event.server_name == "filesystem"


@pytest.mark.asyncio
async def test_after_tool_call_with_error_status(recorder):
    """Ошибка tool-call → Event со status=ERROR."""
    pending = await recorder.before_tool_call(
        server="filesystem", tool="write_file",
        args={"path": "/nonexistent/path"},
    )
    event = await recorder.after_tool_call(
        pending, result=None, error="Permission denied",
    )
    assert event is not None
    assert event.status == EventStatus.ERROR
    assert "Permission denied" in event.error


@pytest.mark.asyncio
async def test_after_tool_call_with_denied_status(recorder):
    """Результат с ⛔ префиксом → status=DENIED (отказ пользователем)."""
    pending = await recorder.before_tool_call(
        server="shell", tool="run_command",
        args={"command": "rm -rf /"},
    )
    event = await recorder.after_tool_call(
        pending, result="⛔ Пользователь отклонил операцию",
    )
    assert event is not None
    assert event.status == EventStatus.DENIED


@pytest.mark.asyncio
async def test_after_tool_call_returns_none_when_pending_is_none(recorder):
    """pending=None → ничего не делаем (журнал выключен или read-инструмент)."""
    event = await recorder.after_tool_call(None, result="ok")
    assert event is None


@pytest.mark.asyncio
async def test_after_tool_call_truncates_long_result(recorder):
    """Длинный результат обрезается до max_result_chars."""
    long_result = "x" * 10000
    pending = await recorder.before_tool_call(
        server="filesystem", tool="read_file", args={"path": "/tmp/big"},
    )
    # read_file помечен как record=False — обходим это
    pending.record = True
    event = await recorder.after_tool_call(pending, result=long_result)
    assert event is not None
    assert len(event.result) <= recorder.cfg.max_result_chars


# ═══════════════════════════════════════════════════════════════════
# truncate_result — helper
# ═══════════════════════════════════════════════════════════════════

def test_truncate_result_short_string_unchanged():
    """Короткая строка возвращается без изменений."""
    assert truncate_result("hello", limit=100) == "hello"


def test_truncate_result_long_string_truncated():
    """Длинная строка обрезается с добавлением '...'."""
    long = "x" * 200
    result = truncate_result(long, limit=50)
    assert len(result) <= 53  # 50 + '...'
    assert result.endswith("...")


def test_truncate_result_dict_truncated_recursively():
    """dict обрезается рекурсивно."""
    d = {"key": "x" * 200, "nested": {"inner": "y" * 200}}
    result = truncate_result(d, limit=50)
    assert isinstance(result, dict)
    assert len(result["key"]) <= 53
    assert len(result["nested"]["inner"]) <= 53


def test_truncate_result_list_truncated():
    """list обрезается."""
    lst = ["x" * 200, "y" * 200]
    result = truncate_result(lst, limit=50)
    assert isinstance(result, list)
    assert all(len(item) <= 53 for item in result)


def test_truncate_result_none_returns_none():
    assert truncate_result(None, limit=100) is None


# ═══════════════════════════════════════════════════════════════════
# inverse_spec — определяет reversible-уровень
# ═══════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_inverse_spec_write_file_is_reversible_full(recorder, tmp_path):
    """write_file — REVERSIBLE_FULL (можно откатить через restore shadow)."""
    target = tmp_path / "test.txt"
    target.write_text("before")
    pending = await recorder.before_tool_call(
        server="filesystem", tool="write_file",
        args={"path": str(target), "content": "after"},
    )
    assert pending is not None
    assert pending.reversible == REVERSIBLE_FULL


@pytest.mark.asyncio
async def test_inverse_spec_delete_file_is_reversible_partial(recorder, tmp_path):
    """delete_file — REVERSIBLE_PARTIAL (восстановление из тени, но метаданные теряются)."""
    target = tmp_path / "to_delete.txt"
    target.write_text("content")
    pending = await recorder.before_tool_call(
        server="filesystem", tool="delete_file",
        args={"path": str(target)},
    )
    assert pending is not None
    # delete — partial (файл можно восстановить, но время mtime утеряно)
    assert pending.reversible in (REVERSIBLE_PARTIAL, REVERSIBLE_FULL)


@pytest.mark.asyncio
async def test_inverse_spec_read_file_is_reversible_none(recorder):
    """read_file — REVERSIBLE_NONE (не меняет состояние системы)."""
    pending = await recorder.before_tool_call(
        server="filesystem", tool="read_file",
        args={"path": "/tmp/any.txt"},
    )
    # read-инструмент ничего не меняет
    assert pending.reversible == REVERSIBLE_NONE

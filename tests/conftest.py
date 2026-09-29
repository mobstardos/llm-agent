"""Общие фикстуры тестов.

Фикстуры `tmp` и `monkey_src` использовались journal/cookies/autodetect
тестами, а `rec` — journal-тестами, но conftest.py не был закоммичен
вместе с ними — теперь он здесь.
"""
import asyncio
from pathlib import Path

import pytest

from src.journal.config import JournalConfig
from src.journal.recorder import JournalRecorder


@pytest.fixture
def tmp(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def monkey_src(monkeypatch):
    """Алиас monkeypatch (использовался тестами 24-a/24-b)."""
    return monkeypatch


@pytest.fixture
def rec(tmp: Path) -> JournalRecorder:
    """JournalRecorder с записанной сессией/задачей/tool_call
    (тот же поток, что test_recorder в tests/test_journal.py)."""
    cfg = JournalConfig.from_env(base_dir=tmp / "data" / "journal")
    cfg.min_free_gb = 0.001
    project = tmp / "proj2"
    (project / "src").mkdir(parents=True, exist_ok=True)
    r = JournalRecorder(cfg, project_root=project)

    async def run() -> JournalRecorder:
        r.start_session("sess1", meta={"user": "test"})
        with r.context(session_id="sess1"):
            r.start_task("task1", trace_id="tr1", query="исправь модуль",
                         agents=["file"])
            p = await r.before_tool_call(
                "filesystem", "write_file",
                {"path": "src/main.py", "content": "code",
                 "password": "abc123"})
            (project / "src" / "main.py").write_text("x = 1", encoding="utf-8")
            await r.after_tool_call(p, result="ok", task_id="task1",
                                    session_id="sess1", agent_id="file")
            r.end_task("task1")
        r.end_session("sess1")
        return r

    rec = asyncio.run(run())
    yield rec
    try:
        rec.shutdown()
    except Exception:
        pass

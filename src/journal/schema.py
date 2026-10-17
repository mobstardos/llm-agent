"""Схема данных журнала: события, типы, статусы.

Журнал — append-only: события никогда не изменяются и не удаляются
(удаление возможно только через RetentionManager, и только объёмных
носителей — тени/архивы, но не индексных записей).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any


# ─── Виды событий ─────────────────────────────────────────────
class EventKind:
    SESSION = "session"          # начало/конец сессии пользователя (WS-подключение)
    TASK = "task"                # задача оркестратора (query → маршрут → агенты)
    AGENT_RUN = "agent_run"      # запуск/завершение агента внутри задачи
    TOOL_CALL = "tool_call"      # вызов MCP-инструмента (главный тип!)
    FILE_CHANGE = "file_change"  # внешнее изменение файла (watcher)
    LOOP_RUN = "loop_run"        # запуск/завершение loop'а
    SNAPSHOT = "snapshot"        # snapshot реестра (ссылка на SnapshotHistory)
    ROLLBACK = "rollback"        # откат (аудит откатов тоже в журнале!)
    REPLAY = "replay"            # воспроизведение
    SYSTEM = "system"            # системное (старт/стоп, retention, ошибки журнала)

    ALL = (SESSION, TASK, AGENT_RUN, TOOL_CALL, FILE_CHANGE,
           LOOP_RUN, SNAPSHOT, ROLLBACK, REPLAY, SYSTEM)


# ─── Статусы ──────────────────────────────────────────────────
class EventStatus:
    OK = "ok"
    ERROR = "error"
    DENIED = "denied"        # отклонено approval gate
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"
    CONFLICT = "conflict"    # конфликт при откате/воспроизведении


# ─── Обратимость операций ─────────────────────────────────────
# Какая обратимость записана в событии (поле `reversible`):
#   0 — нет (только чтение или необратимо)
#   1 — полная (есть тень/инверсная операция)
#   2 — частичная (например SQL UPDATE без before-значений)
REVERSIBLE_NONE = 0
REVERSIBLE_FULL = 1
REVERSIBLE_PARTIAL = 2


def new_event_id() -> str:
    return uuid.uuid4().hex


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


@dataclass
class Event:
    """Единая запись журнала. Хранится в SQLite (индекс) и JSONL (полный вид)."""
    # Идентификация
    event_id: str = field(default_factory=new_event_id)
    seq: int = 0                      # глобальный монотонный номер
    ts: float = field(default_factory=time.time)
    iso_time: str = field(default_factory=now_iso)
    kind: str = EventKind.SYSTEM
    action: str = ""                  # конкретная операция: write_file, mysql_query, ...
    status: str = EventStatus.OK

    # Контекст (кто, зачем, в рамках чего)
    session_id: str = ""              # сессия WS-подключения
    task_id: str = ""                 # задача оркестратора
    trace_id: str = ""                # трассировка оркестратора
    agent_id: str = ""                # агент-исполнитель
    loop_id: str = ""                 # декларация loop'а
    iteration: int = -1               # номер итерации loop'а

    # Что именно произошло
    server_name: str = ""             # имя MCP-сервера ("filesystem")
    tool_name: str = ""               # имя инструмента ("write_file")
    targets: list[str] = field(default_factory=list)   # нормализованные пути/таблицы
    args: dict[str, Any] = field(default_factory=dict) # аргументы (с секретами, для JSONL)
    result: Any = None                # результат (усечённый)
    error: str = ""

    # Целостность и откат
    before_hash: str = ""             # sha256 содержимого до
    after_hash: str = ""              # sha256 содержимого после
    shadow_dir: str = ""              # относительный путь к теням события
    reversible: int = REVERSIBLE_NONE
    inverse: dict[str, Any] = field(default_factory=dict)  # спека обратной операции

    # Связи (граф зависимостей)
    parent_id: str = ""               # предыдущее событие этой задачи
    depends_on: list[str] = field(default_factory=list)    # явные зависимости

    # Метрики
    duration_ms: float = 0.0
    bytes_before: int = -1
    bytes_after: int = -1

    # Прочее
    meta: dict[str, Any] = field(default_factory=dict)
    payload_path: str = ""            # файл с полным payload (если усечено)
    args_digest: str = ""             # sha256(args) — быстрое сравнение

    # ─── Сериализация ────────────────────────────────────────
    def to_row(self) -> dict:
        """Строка для SQLite (args дублируются усечённо — для быстрого get)."""
        import json
        args_json = ""
        try:
            args_json = json.dumps(self.args, ensure_ascii=False, default=str)[:200_000]
        except Exception:
            pass
        return {
            "event_id": self.event_id,
            "seq": self.seq,
            "ts": self.ts,
            "iso_time": self.iso_time,
            "kind": self.kind,
            "action": self.action,
            "status": self.status,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "agent_id": self.agent_id,
            "loop_id": self.loop_id,
            "iteration": self.iteration,
            "server_name": self.server_name,
            "tool_name": self.tool_name,
            "targets": json.dumps(self.targets, ensure_ascii=False),
            "args_json": args_json,
            "args_digest": self.args_digest,
            "before_hash": self.before_hash,
            "after_hash": self.after_hash,
            "shadow_dir": self.shadow_dir,
            "reversible": self.reversible,
            "inverse": json.dumps(self.inverse, ensure_ascii=False),
            "parent_id": self.parent_id,
            "depends_on": json.dumps(self.depends_on, ensure_ascii=False),
            "duration_ms": self.duration_ms,
            "bytes_before": self.bytes_before,
            "bytes_after": self.bytes_after,
            "error": self.error[:2000],
            "meta": json.dumps(self.meta, ensure_ascii=False),
            "payload_path": self.payload_path,
        }

    def to_json(self, include_args: bool = True) -> dict:
        """Полный JSON (для JSONL и API)."""
        d = asdict(self)
        if not include_args:
            d.pop("args", None)
            d.pop("result", None)
        return d

    @classmethod
    def from_row(cls, row: dict, payload: dict | None = None) -> "Event":
        import json
        ev = cls(
            event_id=row["event_id"],
            seq=row["seq"],
            ts=row["ts"],
            iso_time=row["iso_time"],
            kind=row["kind"],
            action=row["action"],
            status=row["status"],
            session_id=row["session_id"] or "",
            task_id=row["task_id"] or "",
            trace_id=row["trace_id"] or "",
            agent_id=row["agent_id"] or "",
            loop_id=row["loop_id"] or "",
            iteration=row["iteration"] if row["iteration"] is not None else -1,
            server_name=row["server_name"] or "",
            tool_name=row["tool_name"] or "",
            targets=json.loads(row["targets"] or "[]"),
            args_digest=row["args_digest"] or "",
            before_hash=row["before_hash"] or "",
            after_hash=row["after_hash"] or "",
            shadow_dir=row["shadow_dir"] or "",
            reversible=row["reversible"] or 0,
            inverse=json.loads(row["inverse"] or "{}"),
            parent_id=row["parent_id"] or "",
            depends_on=json.loads(row["depends_on"] or "[]"),
            duration_ms=row["duration_ms"] or 0.0,
            bytes_before=row["bytes_before"] if row["bytes_before"] is not None else -1,
            bytes_after=row["bytes_after"] if row["bytes_after"] is not None else -1,
            error=row["error"] or "",
            meta=json.loads(row["meta"] or "{}"),
            payload_path=row["payload_path"] or "",
        )
        if payload:
            ev.args = payload.get("args", {})
            ev.result = payload.get("result")
        elif not ev.args:
            # args из SQLite (args_json) — быстрый доступ без JSONL
            try:
                raw = row["args_json"] if "args_json" in row.keys() else ""
                if raw:
                    ev.args = json.loads(raw)
            except Exception:
                pass
        return ev


# ─── Нормализация целей ───────────────────────────────────────
import re as _re

_WIN_DRIVE = _re.compile(r"^[A-Za-z]:[/\\]")


def normalize_target(path: str, project_root: str = "") -> str:
    """Нормализует путь к файлу относительно корня проекта (для сравнимости).

    Windows-пути приводятся к forward-slash: 'src\\core\\x.py' → 'src/core/x.py'.
    Абсолютные пути (в т.ч. с диском 'D:/...') сводятся к пути от корня
    проекта, если они под корнем. Логика чисто строковая — работает
    одинаково на Windows и Linux и не требует существования пути.
    """
    if not path:
        return ""
    p = str(path).replace("\\", "/").strip()
    while p.startswith("./"):
        p = p[2:]
    root = str(project_root or "").replace("\\", "/").rstrip("/")
    if root:
        pl, rl = p.lower(), root.lower()
        if pl.startswith(rl + "/"):
            p = p[len(root) + 1:]
        elif pl == rl:
            p = ""
    return p

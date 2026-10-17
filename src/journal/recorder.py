"""Журнал-рекордер: центральная точка записи всех действий системы.

Использование в коде проекта — две строки в MCPManager.call_tool():

    pending = await journal.before_tool_call(server, tool, args, ...)
    ... выполнение ...
    await journal.after_tool_call(pending, result=result, error=err)

Контекст (кто и зачем) передаётся через contextvars — его выставляет
Orchestrator.handle(), и все вложенные вызовы (агенты, loop'ы, MCP)
наследуют его автоматически. Ничего не ускользает:

  * tool_call  — каждый вызов MCP-инструмента (перехват в MCPManager)
  * task       — каждая задача оркестратора
  * session    — каждая сессия пользователя (WS)
  * file_change— внешние изменения файлов (watcher, опционально)
  * system     — старт/стоп, ретенция, внутренние ошибки
"""
from __future__ import annotations

import asyncio
import contextvars
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import JournalConfig
from .schema import (
    Event, EventKind, EventStatus,
    REVERSIBLE_FULL, REVERSIBLE_NONE, REVERSIBLE_PARTIAL,
    normalize_target, new_event_id,
)
from .shadows import ShadowStore
from .store import JournalStore
from .utils import redact, sha256_dict, sha256_file

# ─── Контекст выполнения (наследуется во все asyncio-задачи) ──
_cv_session: contextvars.ContextVar[str] = contextvars.ContextVar("jr_session", default="")
_cv_task: contextvars.ContextVar[str] = contextvars.ContextVar("jr_task", default="")
_cv_trace: contextvars.ContextVar[str] = contextvars.ContextVar("jr_trace", default="")
_cv_agent: contextvars.ContextVar[str] = contextvars.ContextVar("jr_agent", default="")
_cv_loop: contextvars.ContextVar[str] = contextvars.ContextVar("jr_loop", default="")
_cv_iter: contextvars.ContextVar[int] = contextvars.ContextVar("jr_iter", default=-1)


@dataclass
class PendingToolCall:
    """Состояние вызова инструмента между before и after."""
    event_id: str
    server: str
    tool: str
    args_redacted: dict
    started: float
    targets: dict[str, Path] = field(default_factory=dict)
    before_hashes: dict[str, str] = field(default_factory=dict)
    before_manifest: dict | None = None
    shadow_dir_rel: str = ""
    record: bool = True
    reversible: int = REVERSIBLE_NONE
    inverse: dict = field(default_factory=dict)
    file_tool: bool = False


class JournalRecorder:
    def __init__(
        self,
        cfg: JournalConfig | None = None,
        project_root: str | Path = "",
    ):
        self.cfg = cfg or JournalConfig.from_env()
        self.project_root = Path(project_root) if project_root else Path.cwd()
        self.store = JournalStore(self.cfg)
        self.shadows = ShadowStore(self.cfg)
        self._parent_lock = threading.Lock()
        self._last_event_by_task: dict[str, str] = {}
        self._watcher: "ProjectWatcher | None" = None
        self._started = time.time()

    # ═════════════════════════════════════════════════════════
    # Базовая запись
    # ═════════════════════════════════════════════════════════
    def _parent_for(self, task_id: str, session_id: str) -> str:
        with self._parent_lock:
            return (self._last_event_by_task.get(task_id)
                    or self._last_event_by_task.get(f"session:{session_id}")
                    or "")

    def _mark(self, ev: Event) -> None:
        with self._parent_lock:
            if ev.task_id:
                self._last_event_by_task[ev.task_id] = ev.event_id
            if ev.session_id:
                self._last_event_by_task[f"session:{ev.session_id}"] = ev.event_id
            # защита от безграничного роста
            if len(self._last_event_by_task) > 1000:
                for k in list(self._last_event_by_task)[:500]:
                    self._last_event_by_task.pop(k, None)

    def record(self, ev: Event, **ctx: Any) -> Event:
        """Синхронная запись события (заполняет контекст по умолчанию)."""
        if not self.cfg.enabled:
            return ev
        ev.session_id = ev.session_id or ctx.get("session_id") or _cv_session.get()
        ev.task_id = ev.task_id or ctx.get("task_id") or _cv_task.get()
        ev.trace_id = ev.trace_id or ctx.get("trace_id") or _cv_trace.get()
        ev.agent_id = ev.agent_id or ctx.get("agent_id") or _cv_agent.get()
        ev.loop_id = ev.loop_id or ctx.get("loop_id") or _cv_loop.get()
        if ev.iteration < 0:
            ev.iteration = ctx.get("iteration", _cv_iter.get())
        if not ev.parent_id:
            ev.parent_id = self._parent_for(ev.task_id, ev.session_id)
        if not ev.ts:
            ev.ts = time.time()
        if not ev.iso_time:
            ev.iso_time = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ev.ts))
        self.store.insert(ev)
        self._mark(ev)
        return ev

    # ═════════════════════════════════════════════════════════
    # Контекстные менеджеры (для интеграции)
    # ═════════════════════════════════════════════════════════
    @contextmanager
    def context(
        self, session_id: str = "", task_id: str = "", trace_id: str = "",
        agent_id: str = "", loop_id: str = "", iteration: int = -1,
    ):
        """Задаёт контекст журнала для блока кода (в т.ч. для вложенных задач)."""
        t_session = _cv_session.set(session_id or _cv_session.get())
        t_task = _cv_task.set(task_id or _cv_task.get())
        t_trace = _cv_trace.set(trace_id or _cv_trace.get())
        t_agent = _cv_agent.set(agent_id or _cv_agent.get())
        t_loop = _cv_loop.set(loop_id or _cv_loop.get())
        t_iter = _cv_iter.set(iteration if iteration >= 0 else _cv_iter.get())
        try:
            yield self
        finally:
            _cv_session.reset(t_session)
            _cv_task.reset(t_task)
            _cv_trace.reset(t_trace)
            _cv_agent.reset(t_agent)
            _cv_loop.reset(t_loop)
            _cv_iter.reset(t_iter)

    # ═════════════════════════════════════════════════════════
    # Сессии и задачи
    # ═════════════════════════════════════════════════════════
    def start_session(self, session_id: str, meta: dict | None = None) -> Event:
        return self.record(Event(
            event_id=new_event_id(), kind=EventKind.SESSION,
            action="session_start", session_id=session_id,
            meta=redact(meta or {}, self.cfg.redact_keys),
        ))

    def end_session(self, session_id: str, reason: str = "done",
                    meta: dict | None = None) -> Event:
        ev = self.record(Event(
            event_id=new_event_id(), kind=EventKind.SESSION,
            action="session_end", session_id=session_id,
            meta={"reason": reason, **(meta or {})},
        ))
        if self.cfg.reports_enabled:
            self._try_report(session_id=session_id)
        return ev

    def start_task(
        self, task_id: str, trace_id: str = "", session_id: str = "",
        query: str = "", agents: list[str] | None = None, model: str = "",
    ) -> Event:
        return self.record(Event(
            event_id=new_event_id(), kind=EventKind.TASK,
            action="task_start", task_id=task_id, trace_id=trace_id,
            session_id=session_id,
            meta={
                "query": (query or "")[:2000],
                "agents": agents or [], "model": model,
            },
        ))

    def end_task(
        self, task_id: str, status: str = EventStatus.OK,
        error: str = "", meta: dict | None = None,
    ) -> Event:
        ev = self.record(Event(
            event_id=new_event_id(), kind=EventKind.TASK,
            action="task_end", task_id=task_id, status=status,
            error=(error or "")[:2000],
            meta=redact(meta or {}, self.cfg.redact_keys),
        ))
        if self.cfg.reports_enabled:
            self._try_report(task_id=task_id)
        return ev

    def _try_report(self, **kwargs: Any) -> None:
        try:
            from .reports import ReportGenerator
            ReportGenerator(self).generate_async(**kwargs)
        except Exception:
            pass

    # ═════════════════════════════════════════════════════════
    # Перехват tool call'ов (интеграция с MCPManager)
    # ═════════════════════════════════════════════════════════
    async def before_tool_call(
        self,
        server: str, tool: str, args: dict,
        session_id: str = "", task_id: str = "", trace_id: str = "",
        agent_id: str = "", loop_id: str = "", iteration: int = -1,
        denied: bool = False,
    ) -> PendingToolCall | None:
        """Готовит запись и тени ДО выполнения инструмента."""
        if not self.cfg.enabled:
            return None
        pending = PendingToolCall(
            event_id=new_event_id(), server=server, tool=tool,
            args_redacted=redact(args, self.cfg.redact_keys),
            started=time.time(),
            record=True,
        )
        full_name = f"{server}.{tool}"

        # read-инструменты можно не писать (шум), кроме явных исключений
        if not self.cfg.record_reads and self._is_read_tool(server, tool):
            pending.record = False
            return pending

        # Файловые цели → тени
        targets = self._extract_targets(server, tool, args)
        if targets and self.cfg.shadows_enabled and not denied:
            try:
                manifest = await asyncio.to_thread(
                    self.shadows.save_before, pending.event_id, targets,
                )
                pending.before_manifest = manifest
                pending.shadow_dir_rel = f"{manifest.get('day', '')}/{pending.event_id}"
                pending.file_tool = True
            except Exception:
                pending.before_manifest = None

        pending.targets = targets
        pending.reversible, pending.inverse = self._inverse_spec(
            full_name, tool, args, targets, pending.before_manifest,
        )
        if denied:
            pending.reversible = REVERSIBLE_NONE
            pending.inverse = {}
        return pending

    async def after_tool_call(
        self,
        pending: PendingToolCall | None,
        result: Any = None,
        error: str = "",
        status: str = "",
        session_id: str = "", task_id: str = "", trace_id: str = "",
        agent_id: str = "", loop_id: str = "", iteration: int = -1,
    ) -> Event | None:
        """Завершает запись: тени после, хэши, событие."""
        if pending is None or not pending.record:
            return None
        duration = (time.time() - pending.started) * 1000.0

        if not status:
            status = (EventStatus.ERROR if error
                      else EventStatus.DENIED if result and "⛔" in str(result)[:5]
                      else EventStatus.OK)

        ev = Event(
            event_id=pending.event_id,
            kind=EventKind.TOOL_CALL,
            action=pending.tool,
            status=status,
            session_id=session_id, task_id=task_id, trace_id=trace_id,
            agent_id=agent_id, loop_id=loop_id, iteration=iteration,
            server_name=pending.server, tool_name=pending.tool,
            targets=list(pending.targets.keys()),
            args=pending.args_redacted,
            result=(truncate_result(result, self.cfg.max_result_chars)
                    if self.cfg.record_results else None),
            error=(error or "")[:2000],
            duration_ms=duration,
            reversible=pending.reversible,
            inverse=pending.inverse,
            shadow_dir=pending.shadow_dir_rel,
            meta={"file_tool": pending.file_tool},
        )

        # Хэши и тени ПОСЛЕ
        if pending.targets:
            before_h: list[str] = []
            after_h: list[str] = []
            try:
                after_manifest = await asyncio.to_thread(
                    self.shadows.save_after, pending.event_id,
                    pending.targets, pending.before_manifest,
                )
                for norm, info in after_manifest.get("files", {}).items():
                    b = (info.get("before") or {})
                    if b.get("sha256"):
                        before_h.append(b["sha256"])
                    if info.get("sha256"):
                        after_h.append(info["sha256"])
                    ev.bytes_before = max(ev.bytes_before, b.get("size", -1))
                    ev.bytes_after = max(ev.bytes_after, info.get("size", -1))
                ev.shadow_dir = ev.shadow_dir or pending.shadow_dir_rel
            except Exception as e:
                ev.meta["shadow_error"] = str(e)
            ev.before_hash = ",".join(sorted(set(before_h)))[:128]
            ev.after_hash = ",".join(sorted(set(after_h)))[:128]

            # Если хэши совпали — файл фактически не изменился
            if (ev.before_hash and ev.after_hash
                    and ev.before_hash == ev.after_hash
                    and status == EventStatus.OK):
                ev.meta["no_change"] = True

        return self.record(ev)

    # ─── Помощники ───────────────────────────────────────────
    def _is_read_tool(self, server: str, tool: str) -> bool:
        full = f"{server}.{tool}"
        for pattern in self.cfg.file_tools:
            if pattern.endswith(".*") and full.startswith(pattern[:-1]):
                return False
        read_prefixes = ("read", "list", "search", "get", "show", "query",
                         "describe", "current", "check", "find", "select")
        return tool.lower().startswith(read_prefixes)

    def _extract_targets(self, server: str, tool: str, args: dict) -> dict[str, Path]:
        """Определяет файловые цели вызова (по конфигу + эвристика путей)."""
        targets: dict[str, Path] = {}
        full = f"{server}.{tool}"
        is_file_tool = self._matches_file_tool(full)

        def consider(value: Any) -> None:
            if not isinstance(value, str) or not value.strip():
                return
            v = value.strip()
            if len(v) > 500 or "\n" in v or "://" in v:
                return
            p = Path(v)
            if not p.is_absolute():
                # относительная строка — только если она ПОХОЖА на путь
                if not _looks_like_path(v):
                    return
                p = self.project_root / v
            try:
                norm = normalize_target(v, str(self.project_root))
                if norm and norm not in targets:
                    targets[norm] = p
            except Exception:
                pass

        # 1. Известные ключи с путями (для файловых инструментов — приоритет)
        if is_file_tool:
            for key in self.cfg.path_arg_keys:
                if key in args:
                    consider(args[key])
        # 2. Неизвестные ключи — эвристика «похоже на путь»
        for v in args.values():
            consider(v)
        return targets

    def _matches_file_tool(self, full: str) -> bool:
        for pattern in self.cfg.file_tools:
            if pattern == full:
                return True
            if pattern.endswith(".*") and full.startswith(pattern[:-1]):
                return True
        return False

    def _inverse_spec(
        self, full_name: str, tool: str, args: dict,
        targets: dict[str, Path], before_manifest: dict | None,
    ) -> tuple[int, dict]:
        """Спецификация обратной операции для отката."""
        shadow = bool(before_manifest and before_manifest.get("files"))
        t = tool.lower()

        if t in ("write_file", "apply_patch", "save_file"):
            if shadow:
                return REVERSIBLE_FULL, {"op": "restore_from_shadow"}
            return REVERSIBLE_PARTIAL, {"op": "unknown_before_state"}

        if t == "delete_file":
            if shadow:
                return REVERSIBLE_FULL, {"op": "restore_from_shadow"}
            return REVERSIBLE_NONE, {}

        if t in ("move_file", "rename_file", "copy_file"):
            if shadow:
                return REVERSIBLE_FULL, {"op": "restore_from_shadow",
                                         "extra_targets": True}
            return REVERSIBLE_PARTIAL, {"op": "unknown_before_state"}

        if t in ("query", "execute", "sql", "execute_query", "run_query"):
            # SQL: обратимость частичная (inverse SQL достраивается при откате
            # только для тривиальных случаев) — честно помечаем
            return REVERSIBLE_PARTIAL, {"op": "sql_manual", "sql": str(
                args.get("query") or args.get("sql") or "")[:1000]}

        if full_name.startswith("git.") or full_name.startswith("shell."):
            return REVERSIBLE_NONE, {}

        if shadow:
            return REVERSIBLE_FULL, {"op": "restore_from_shadow"}
        return REVERSIBLE_NONE, {}

    # ═════════════════════════════════════════════════════════
    # Прочие виды событий
    # ═════════════════════════════════════════════════════════
    def record_agent_run(
        self, agent_id: str, task_id: str = "", trace_id: str = "",
        status: str = EventStatus.OK, error: str = "",
        meta: dict | None = None,
    ) -> Event:
        return self.record(Event(
            kind=EventKind.AGENT_RUN,
            action=f"agent_{meta.get('phase', 'run') if meta else 'run'}",
            agent_id=agent_id, task_id=task_id, trace_id=trace_id,
            status=status, error=(error or "")[:2000],
            meta=redact(meta or {}, self.cfg.redact_keys),
        ))

    def record_system(self, action: str, status: str = EventStatus.OK,
                      error: str = "", meta: dict | None = None) -> Event:
        return self.record(Event(
            kind=EventKind.SYSTEM, action=action, status=status,
            error=(error or "")[:2000],
            meta=redact(meta or {}, self.cfg.redact_keys),
        ))

    def record_external_change(
        self, path: Path, old_hash: str, new_hash: str, size: int,
    ) -> Event | None:
        """Внешнее (вне агентов) изменение файла — watcher."""
        try:
            norm = normalize_target(str(path), str(self.project_root))
            ev = Event(
                kind=EventKind.FILE_CHANGE, action="external_change",
                targets=[norm],
                before_hash=old_hash, after_hash=new_hash,
                bytes_after=size,
                meta={"source": "watcher"},
                reversible=REVERSIBLE_NONE,
            )
            return self.record(ev)
        except Exception:
            return None

    # ═════════════════════════════════════════════════════════
    # Watcher внешних изменений (poll, без зависимостей)
    # ═════════════════════════════════════════════════════════
    def start_watcher(self) -> None:
        if self._watcher is None:
            self._watcher = ProjectWatcher(self)
            self._watcher.start()

    def stop_watcher(self) -> None:
        if self._watcher:
            self._watcher.stop()
            self._watcher = None

    # ═════════════════════════════════════════════════════════
    # Жизненный цикл
    # ═════════════════════════════════════════════════════════
    def startup(self) -> Event:
        from .utils import disk_usage
        return self.record_system("startup", meta={
            "project_root": str(self.project_root),
            "config": {
                "min_free_gb": self.cfg.min_free_gb,
                "shadows": self.cfg.shadows_enabled,
                "watch_project": self.cfg.watch_project,
                "record_reads": self.cfg.record_reads,
            },
            "disk": disk_usage(self.cfg.base_dir),
        })

    def shutdown(self) -> Event:
        self.stop_watcher()
        ev = self.record_system("shutdown", meta={
            "uptime_seconds": round(time.time() - self._started, 1),
        })
        self.store.close()
        return ev


def _looks_like_path(v: str) -> bool:
    """Эвристика: строка похожа на относительный путь к файлу."""
    if not v or len(v) > 300 or "\n" in v or "://" in v:
        return False
    if v.startswith(("{", "[", "<")):
        return False
    from .config import JournalConfig
    exts = JournalConfig().shadow_binary_exts + (
        ".py", ".js", ".ts", ".yaml", ".yml", ".json", ".md", ".txt",
        ".html", ".css", ".sql", ".bat", ".cmd", ".ps1", ".sh", ".xml",
        ".ini", ".cfg", ".env", ".csv", ".log", ".1c", ".os", ".erp",
    )
    low = v.lower()
    if any(low.endswith(ext) for ext in exts):
        return True
    return ("/" in v or "\\" in v) and "." in v.split("/")[-1].split("\\")[-1]


def truncate_result(result: Any, limit: int) -> Any:
    if isinstance(result, str):
        from .utils import truncate
        return truncate(result, limit)
    return result


# ═════════════════════════════════════════════════════════════
# Watcher: poll-сканер внешних изменений проекта
# ═════════════════════════════════════════════════════════════
class ProjectWatcher:
    """Периодически сканирует корень проекта и фиксирует изменения,
    сделанные ВНЕ агентов (руками, другой программой)."""

    def __init__(self, recorder: JournalRecorder):
        self.rec = recorder
        self.cfg = recorder.cfg
        self._state: dict[str, tuple[float, int, str]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first = True

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="journal-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.cfg.watch_interval_seconds):
            try:
                self._scan()
            except Exception:
                continue

    def _scan(self) -> None:
        root = self.rec.project_root
        if not root.exists():
            return
        exclude = set(self.cfg.watch_exclude_dirs) | {"journal"}
        current: dict[str, tuple[float, int]] = {}
        for p in root.rglob("*"):
            try:
                if not p.is_file():
                    continue
                rel = p.relative_to(root)
                if any(part in exclude for part in rel.parts):
                    continue
                st = p.stat()
                current[str(rel)] = (st.st_mtime, st.st_size)
            except OSError:
                continue

        if self._first:
            self._state = {
                k: (m, s, "") for k, (m, s) in current.items()
            }
            self._first = False
            return

        changed = []
        for rel, (m, s) in current.items():
            old = self._state.get(rel)
            if old is None:
                changed.append(rel)          # новый файл
            elif old[0] != m or old[1] != s:
                changed.append(rel)          # изменился
        removed = [rel for rel in self._state if rel not in current]

        for rel in changed[:100]:  # предохранитель от лавины
            p = root / rel
            try:
                new_hash = sha256_file(p)
                old_hash = self._state.get(rel, (0, 0, ""))[2]
                if new_hash != old_hash:
                    self.rec.record_external_change(
                        p, old_hash, new_hash, current[rel][1])
                self._state[rel] = (current[rel][0], current[rel][1], new_hash)
            except OSError:
                continue
        for rel in removed[:100]:
            ev = Event(
                kind=EventKind.FILE_CHANGE, action="external_delete",
                targets=[rel.replace("\\", "/")],
                before_hash=self._state.get(rel, (0, 0, ""))[2],
                meta={"source": "watcher"},
            )
            self.rec.record(ev)
            self._state.pop(rel, None)

    # служебное (для stats)
    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tracked_files": len(self._state),
            "interval": self.cfg.watch_interval_seconds,
        }

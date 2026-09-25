"""Markdown-отчёты по сессиям и задачам — человеко- И AI-читаемый слой журнала.

Отчёты лежат в data/journal/reports/ и являются полноценным интерфейсом
доступа для LLM: модель может просто прочитать файл (filesystem.read_file)
и понять всю хронологию действий, правки и зависимости.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

from .recorder import JournalRecorder
from .store import JournalStore
from .utils import human_size, write_text_safe


class ReportGenerator:
    def __init__(self, recorder: JournalRecorder):
        self.rec = recorder
        self.store: JournalStore = recorder.store

    # ─── Асинхронная генерация (не блокирует основной поток) ──
    def generate_async(self, **kwargs) -> threading.Thread:
        t = threading.Thread(
            target=self._safe_generate, kwargs=kwargs,
            name="journal-report", daemon=True)
        t.start()
        return t

    def _safe_generate(self, **kwargs) -> None:
        try:
            self.generate(**kwargs)
        except Exception:
            pass

    # ─── Генерация ───────────────────────────────────────────
    def generate(self, session_id: str = "", task_id: str = "") -> Path | None:
        if task_id:
            return self._generate_task(task_id)
        if session_id:
            return self._generate_session(session_id)
        return None

    def _generate_session(self, session_id: str) -> Path | None:
        events = self.store.query(
            session_id=session_id, limit=5000, order_desc=False)
        if not events:
            return None
        tasks = sorted({e.task_id for e in events if e.task_id})
        parts = [self._header(
            f"Отчёт по сессии {session_id}",
            events,
            extra=[f"**Задач в сессии:** {len(tasks)}"],
        )]
        for tid in tasks:
            t_events = [e for e in events if e.task_id == tid]
            parts.append(self._task_section(tid, t_events))
        parts.append(self._footer(events))
        text = "\n\n".join(parts)
        name = f"session-{session_id[:12]}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path = self.rec.cfg.reports_dir / name
        write_text_safe(path, text)
        return path

    def _generate_task(self, task_id: str) -> Path | None:
        events = self.store.query(task_id=task_id, limit=5000, order_desc=False)
        if not events:
            return None
        parts = [self._header(f"Отчёт по задаче {task_id}", events)]
        parts.append(self._task_section(task_id, events))
        parts.append(self._footer(events))
        text = "\n\n".join(parts)
        name = f"task-{task_id[:12]}-{time.strftime('%Y%m%d-%H%M%S')}.md"
        path = self.rec.cfg.reports_dir / name
        write_text_safe(path, text)
        return path

    # ─── Секции ──────────────────────────────────────────────
    def _header(self, title: str, events: list, extra: list[str] | None = None) -> str:
        first = events[0]
        last = events[-1]
        mutating = [e for e in events if e.reversible > 0]
        failed = [e for e in events if e.status in ("error", "conflict")]
        lines = [
            f"# {title}",
            "",
            f"**Период:** {first.iso_time} → {last.iso_time}  ",
            f"**Событий:** {len(events)}  ",
            f"**Изменяющих действий:** {len(mutating)}  ",
            f"**Ошибок:** {len(failed)}  ",
            f"**Размер журнала:** {human_size(self._journal_size())}",
        ]
        if extra:
            lines.extend(extra)
        if mutating:
            lines += [
                "",
                "## Изменённые цели",
                "",
            ]
            seen: set[str] = set()
            for e in mutating:
                for t in e.targets:
                    if t in seen:
                        continue
                    seen.add(t)
                    lines.append(f"- `{t}` — последний раз: {e.tool_name} "
                                 f"({e.agent_id or '—'}), x-{e.seq}")
        return "\n".join(lines)

    def _task_section(self, task_id: str, events: list) -> str:
        lines = [f"## Задача `{task_id}`"]
        q = next((e.meta.get("query") for e in events
                  if e.meta.get("query")), "")
        if q:
            lines += ["", f"> **Запрос:** {q[:500]}"]
        lines += ["", "| # | Время | Агент | Действие | Цели | Статус | мс |", "|---|---|---|---|---|---|---|"]
        for i, e in enumerate(events, 1):
            targets = ", ".join(f"`{t}`" for t in e.targets[:3])
            if len(e.targets) > 3:
                targets += f" +{len(e.targets) - 3}"
            status = e.status if e.status == "ok" else f"**{e.status}**"
            lines.append(
                f"| {i} | {e.iso_time[-8:]} | {e.agent_id or '—'} "
                f"| {e.tool_name or e.action or e.kind} | {targets or '—'} "
                f"| {status} | {e.duration_ms:.0f} |")
        return "\n".join(lines)

    def _footer(self, events: list) -> str:
        mutating = [e for e in events if e.reversible > 0]
        lines = ["## Возможности отката", ""]
        if not mutating:
            lines.append("Изменяющих действий нет — откат не требуется.")
        else:
            last_shadow = next(
                (e for e in reversed(mutating) if e.shadow_dir), None)
            lines += [
                f"- Изменяющих событий: **{len(mutating)}**",
                f"- Полностью обратимых: **{sum(1 for e in mutating if e.reversible == 1)}**",
                f"- Частично обратимых: **{sum(1 for e in mutating if e.reversible == 2)}**",
                f"- Точка полного отката: `{last_shadow.event_id if last_shadow else '—'}`",
                "",
                "Откат: `POST /api/journal/rollback` {\"event_id\": ..., \"mode\": \"dry_run\"}",
            ]
        return "\n".join(lines)

    def _journal_size(self) -> int:
        from .utils import dir_size
        return dir_size(self.rec.cfg.base_dir)

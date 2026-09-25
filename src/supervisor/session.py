"""ConversationSession — состояние диалога чата (Этап 1–2, V2 §3.4).

Хранит последние сообщения диалога, агентов прошлого ответа и
состояние активного плана Supervisor (Этап 2), чтобы роутинг,
Intent Layer и планировщик работали в контексте разговора
(«а теперь…», «продолжай»), а не в вакууме.

Дамп — в data/sessions/<id>.jsonl (перезапуск сервера не теряет чат).
Формат аддитивный: строки {"role": ...} — сообщения (Этап 1),
строки {"type": "plan"|"step", ...} — состояние плана (Этап 2);
старые дампы читаются без изменений.
Только stdlib; падение дампа никогда не ломает диалог.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Msg:
    """Одно сообщение диалога."""

    role: str                    # "user" | "assistant"
    content: str
    agent: str = ""              # id агента, если ответ от агента
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "content": self.content,
            "agent": self.agent,
            "ts": self.ts,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Msg":
        return cls(
            role=str(data.get("role", "user")),
            content=str(data.get("content", "")),
            agent=str(data.get("agent", "")),
            ts=float(data.get("ts", 0) or 0),
        )


class ConversationSession:
    """Контекст одного чата: история, прошлые агенты, дамп на диск."""

    #: сколько последних сообщений отдаём роутеру/планировщику
    MAX_CONTEXT_MESSAGES = 12
    #: обрезка длинных сообщений при форматировании контекста
    MAX_CHARS_PER_MSG = 400

    def __init__(self, session_id: str = "", dump_dir: str | Path | None = None):
        self.id = session_id or uuid.uuid4().hex[:16]
        self.history: list[Msg] = []
        #: агенты, участвовавшие в последнем ответе (для «продолжай»)
        self.last_agents: list[str] = []
        #: заголовок чата (Task 24-c): генерируется микрозадачей локальной
        #: модели; manual=True — задан пользователем и не перезаписывается
        self.title: str = ""
        self.title_manual: bool = False
        #: активный план Supervisor (Этап 2): {intent, steps: [{id, agent, task, status}]}
        self.active_plan: dict | None = None
        #: сжатые результаты шагов активного/последнего плана: {step_id: {...}}
        self.step_results: dict[str, dict] = {}
        self.dump_dir = Path(dump_dir) if dump_dir else None
        self.created_at = time.time()

    # ═══════════════════════════════════════════════════════
    # Заголовок чата (Task 24-c)
    # ═══════════════════════════════════════════════════════
    def set_title(self, title: str, manual: bool = False) -> None:
        """Сохраняет заголовок (дамп-запись {type: title}); ручной — приоритетный."""
        self.title = (title or "").strip()[:80]
        self.title_manual = bool(manual) or self.title_manual
        self._dump_raw({
            "type": "title", "title": self.title,
            "manual": manual or self.title_manual,
            "ts": time.time(),
        })

    # ═══════════════════════════════════════════════════════
    # Активный план (Этап 2)
    # ═══════════════════════════════════════════════════════
    def set_active_plan(self, plan: dict | None) -> None:
        """Фиксирует план Supervisor (словарь с шагами status=pending)."""
        self.active_plan = plan
        self._dump_raw({"type": "plan", "plan": plan})

    def record_step_result(self, step_id: str, entry: dict) -> None:
        """Сохраняет сжатый результат шага (для observe/re-plan и контекста)."""
        compact = {
            "step_id": step_id,
            "agent": entry.get("agent", ""),
            "task": entry.get("task", ""),
            "success": bool(entry.get("success")),
            "summary": (entry.get("content", "") or "")[:300],
            "error": (entry.get("error", "") or "")[:200],
            "tool_calls": int(entry.get("tool_calls", 0) or 0),
        }
        self.step_results[step_id] = compact
        if self.active_plan:
            for st in self.active_plan.get("steps", []):
                if str(st.get("id")) == str(step_id):
                    st["status"] = "done" if compact["success"] else "error"
        self._dump_raw({"type": "step", **compact})

    def plan_context_for_planner(self) -> str:
        """Сводка активного плана для планировщика (выполнено/упало/ожидает)."""
        if not self.active_plan:
            return ""
        lines: list[str] = []
        for st in self.active_plan.get("steps", []):
            status = st.get("status", "pending")
            mark = {"done": "готово", "error": "упал", "pending": "ожидает"}.get(
                status, status)
            line = f"Шаг {st.get('id')} [{mark}] {st.get('agent', '')} — {st.get('task', '')}"
            res = self.step_results.get(str(st.get("id")))
            if res:
                tail = res["summary"] if res["success"] else res["error"]
                if tail:
                    line += f": {tail[:150]}"
            lines.append(line)
        return "\n".join(lines) if lines else ""

    def plan_is_open(self) -> bool:
        """Есть ли незавершённые шаги в активном плане."""
        if not self.active_plan:
            return False
        return any(st.get("status", "pending") not in ("done", "error")
                   for st in self.active_plan.get("steps", []))

    # ═══════════════════════════════════════════════════════
    # Наполнение
    # ═══════════════════════════════════════════════════════
    def add_user(self, text: str) -> None:
        self._append(Msg(role="user", content=text or ""))

    def add_assistant(self, text: str, agent: str = "") -> None:
        self._append(Msg(role="assistant", content=text or "", agent=agent))
        if agent:
            # авто-отслеживание: «продолжай» вернётся к этим агентам
            if agent not in self.last_agents:
                self.last_agents.append(agent)

    def set_last_agents(self, agents: list[str]) -> None:
        self.last_agents = [a for a in (agents or []) if a]

    # ═══════════════════════════════════════════════════════
    # Правка сообщения + откат (Task 27)
    # ═══════════════════════════════════════════════════════
    def edit_user(self, index: int, text: str) -> int:
        """Правит сообщение пользователя и ОТКАТЫВАЕТ всё после него.

        История после правленого сообщения (ответы, планы, шаги) удаляется —
        как «карандаш» в ChatGPT: диалог продолжается с этой точки.
        Возвращает фактический индекс или -1 (не найдено/не user/пустой текст).
        """
        if not (text or "").strip():
            return -1
        if index < 0 or index >= len(self.history):
            return -1
        if self.history[index].role != "user":
            return -1
        self.history[index].content = (text or "").strip()
        self.history[index].ts = time.time()
        del self.history[index + 1:]
        # план и результаты шагов относились к откаченной части диалога
        self.active_plan = None
        self.step_results = {}
        self.last_agents = []
        self._rewrite_dump()
        return index

    def _rewrite_dump(self) -> None:
        """Полная перезапись дампа после правки (jsonl — append-only больше не вариант)."""
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            path = self.dump_dir / f"{self.id}.jsonl"
            tmp = path.with_suffix(".jsonl.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                if self.title:
                    f.write(json.dumps(
                        {"type": "title", "title": self.title,
                         "manual": self.title_manual, "ts": time.time()},
                        ensure_ascii=False) + "\n")
                for m in self.history:
                    f.write(json.dumps(
                        m.to_dict(), ensure_ascii=False) + "\n")
            tmp.replace(path)
        except Exception:
            logger.debug("Session rewrite failed (пропускаю)", exc_info=True)

    def _append(self, msg: Msg) -> None:
        self.history.append(msg)
        # держим хвост разумной длины (память процесса)
        if len(self.history) > self.MAX_CONTEXT_MESSAGES * 4:
            self.history = self.history[-self.MAX_CONTEXT_MESSAGES:]
        self._dump(msg)

    def _dump_raw(self, record: dict) -> None:
        """Дамп произвольной записи (план/шаг) — мягкий, как и сообщения."""
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            path = self.dump_dir / f"{self.id}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("Session dump failed (пропускаю)", exc_info=True)

    # ═══════════════════════════════════════════════════════
    # Контекст для роутера / планировщика
    # ═══════════════════════════════════════════════════════
    def history_for_router(self, n: int = MAX_CONTEXT_MESSAGES) -> str:
        """Последние n сообщений в компактном текстовом виде.

        Пустая строка, если диалог только начался.
        """
        tail = self.history[-n:]
        if not tail:
            return ""
        lines: list[str] = []
        for m in tail:
            content = (m.content or "").strip().replace("\n", " ")
            if len(content) > self.MAX_CHARS_PER_MSG:
                content = content[: self.MAX_CHARS_PER_MSG] + "…"
            if m.role == "user":
                lines.append(f"Пользователь: {content}")
            elif m.agent:
                lines.append(f"Агент [{m.agent}]: {content}")
            else:
                lines.append(f"Ассистент: {content}")
        return "\n".join(lines)

    def summary_for_router(self) -> str:
        """Краткая сводка состояния сессии (агенты прошлого шага)."""
        if self.last_agents:
            return ", ".join(self.last_agents)
        return ""

    # ═══════════════════════════════════════════════════════
    # Дамп (jsonl, мягкий)
    # ═══════════════════════════════════════════════════════
    def _dump(self, msg: Msg) -> None:
        if self.dump_dir is None:
            return
        try:
            self.dump_dir.mkdir(parents=True, exist_ok=True)
            path = self.dump_dir / f"{self.id}.jsonl"
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(msg.to_dict(), ensure_ascii=False) + "\n")
        except Exception:
            logger.debug("Session dump failed (пропускаю)", exc_info=True)

    def load(self) -> int:
        """Подхватывает дамп с диска (после перезапуска сервера)."""
        if self.dump_dir is None:
            return 0
        path = self.dump_dir / f"{self.id}.jsonl"
        if not path.exists():
            return 0
        loaded = 0
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except Exception:
                    continue
                # Этап 2: записи плана/шагов (аддитивный формат)
                if isinstance(data, dict) and "role" not in data and "type" in data:
                    if data["type"] == "plan" and isinstance(data.get("plan"), dict):
                        self.active_plan = data["plan"]
                    elif data["type"] == "step" and data.get("step_id"):
                        self.step_results[str(data["step_id"])] = data
                    elif data["type"] == "title":
                        # Task 24-c: ручной заголовок сильнее автогенерации
                        if not self.title_manual:
                            self.title = str(data.get("title", ""))
                            self.title_manual = bool(data.get("manual"))
                    continue
                try:
                    self.history.append(Msg.from_dict(data))
                    loaded += 1
                except Exception:
                    continue
        except Exception:
            logger.debug("Session load failed", exc_info=True)
        # восстановить last_agents из хвоста
        for m in reversed(self.history):
            if m.agent:
                self.last_agents = [m.agent]
                break
        return loaded

    def __len__(self) -> int:
        return len(self.history)

    def messages_view(self, limit: int = 100) -> list[dict]:
        """Срез истории для восстановления UI (Task 24-c: переключение чатов).

        Task 27: каждое сообщение получает "index" — позицию в self.history,
        по нему UI правит/откатывает сообщения (edit_user).
        """
        tail = self.history[-max(1, limit):]
        base = len(self.history) - len(tail)
        out: list[dict] = []
        for i, m in enumerate(tail):
            d = m.to_dict()
            d["index"] = base + i
            out.append(d)
        return out

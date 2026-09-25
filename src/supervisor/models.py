"""Контракты Supervisor (Этап 2, ARCHITECTURE-V2 §3.5).

Plan / PlanStep / ObserveDecision — pydantic-модели в стиле
src/core/schema.py. Парсинг ответа LLM переиспользует отлаженную
схему route(): срез markdown-ограждений → json.loads → regex-fallback
первого JSON-объекта → валидация pydantic.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ═════════════════════════════════════════════════════════════
# Контракты плана
# ═════════════════════════════════════════════════════════════
class PlanStep(BaseModel):
    """Один шаг плана: подзадача конкретному агенту."""

    id: str
    agent: str            # должен быть в известных агентах (фильтр как в route())
    task: str             # конкретная подзадача (НЕ сырой запрос)
    depends_on: list[str] = Field(default_factory=list)
    why: str = ""


class Plan(BaseModel):
    """План: последовательность шагов + прямой ответ (если агенты не нужны)."""

    intent: str = ""
    steps: list[PlanStep] = Field(default_factory=list)
    reply: str = ""               # что сказать пользователю сразу (пустой план)
    needs_approval: bool = False  # показать план перед выполнением


class ObserveDecision(BaseModel):
    """Решение Supervisor после наблюдения за результатом шага."""

    action: Literal["continue", "replan", "finish", "ask_user"] = "continue"
    updated_plan: Plan | None = None
    message_to_user: str = ""


# ═════════════════════════════════════════════════════════════
# Извлечение JSON из ответа LLM (та же схема, что в route())
# ═════════════════════════════════════════════════════════════
def extract_json(raw: str) -> dict | None:
    """Срез ограждений → json.loads → regex-fallback первого объекта."""
    text = (raw or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    data = None
    try:
        data = json.loads(text)
    except Exception:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                data = json.loads(match.group(0))
            except Exception:
                data = None
    return data if isinstance(data, dict) else None


def _plan_from_data(data: dict) -> Plan:
    """План из словаря с ПОЛНОЙ валидацией и мягким парсингом шагов.

    Шаги валидируются по одному: один битый шаг отбрасывается,
    а не рушит весь план.
    """
    payload = {k: v for k, v in data.items() if k != "steps"}
    plan = Plan.model_validate(payload)
    steps: list[PlanStep] = []
    for i, s in enumerate(data.get("steps") or [], 1):
        try:
            if not isinstance(s, dict):
                continue
            s = dict(s)
            s.setdefault("id", str(i))
            s["id"] = str(s["id"])
            steps.append(PlanStep.model_validate(s))
        except Exception:
            continue
    plan.steps = steps
    return plan


def parse_plan(raw: str) -> Plan | None:
    """План из ответа LLM; None, если распарсить не удалось."""
    data = extract_json(raw)
    if data is None:
        return None
    try:
        return _plan_from_data(data)
    except Exception:
        logger.debug("parse_plan: невалидный план", exc_info=True)
        return None


def parse_observe(raw: str) -> ObserveDecision | None:
    """ObserveDecision из ответа LLM; None при мусоре."""
    data = extract_json(raw)
    if data is None:
        return None
    # вложенный updated_plan парсим мягко: его шаги валидируются отдельно
    upd = data.get("updated_plan")
    if isinstance(upd, dict):
        try:
            data["updated_plan"] = _plan_from_data(upd)
        except Exception:
            data["updated_plan"] = None
    try:
        return ObserveDecision.model_validate(data)
    except Exception:
        logger.debug("parse_observe: невалидное решение", exc_info=True)
        return None

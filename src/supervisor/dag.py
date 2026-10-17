"""DAG-исполнение плана (Этап 3, ARCHITECTURE-V2 §3.10 «Параллелизм»).

Планировщик размечает зависимости шагов через depends_on. Здесь —
детерминированная математика поверх этого: разбивка плана на волны
(шаги одной волны не зависят друг от друга и могут выполняться
параллельно), проверка корректности ссылок и эвристика режима.

Совместимость (важно для регрессии Этапа 2): если НИ один шаг плана
не объявил depends_on явно — план исполняется последовательно, как
раньше. Это страховка от слабой модели-планировщика: конвейер из двух
шагов без размеченных зависимостей не должен запуститься параллельно.
Плюс общий выключатель SUPERVISOR_PARALLEL=0.
"""
from __future__ import annotations

import logging
import os

from src.supervisor.models import Plan, PlanStep

logger = logging.getLogger(__name__)

#: общий выключатель параллелизма (env SUPERVISOR_PARALLEL=0 — как в Этапе 2)
PARALLEL_ENABLED = os.getenv("SUPERVISOR_PARALLEL", "1").strip().lower() not in (
    "0", "false", "no", "off",
)


def is_dag_plan(steps: list[PlanStep]) -> bool:
    """DAG-режим: параллелизм включён И есть хоть одна явная зависимость."""
    if not PARALLEL_ENABLED:
        return False
    return any(s.depends_on for s in steps)


def normalize_deps(steps: list[PlanStep]) -> dict[str, list[str]]:
    """Чистит depends_on: неизвестные id и само-ссылки отбрасываются.

    Возвращает {step_id: [зависимости, которые реально существуют]}.
    """
    known = {s.id for s in steps}
    out: dict[str, list[str]] = {}
    for s in steps:
        deps = []
        for d in s.depends_on or []:
            d = str(d)
            if d == s.id:
                logger.warning("DAG: шаг %s зависит от себя — игнорирую", s.id)
            elif d not in known:
                logger.warning("DAG: шаг %s ссылается на неизвестный '%s' — "
                               "игнорирую", s.id, d)
            else:
                deps.append(d)
        out[s.id] = deps
    return out


def plan_waves(steps: list[PlanStep]) -> list[list[PlanStep]]:
    """Разбивает шаги на волны topологической сортировки.

    - волна k содержит шаги, у которых все зависимости в волнах < k;
    - порядок шагов внутри волны сохраняется как в плане (стабильно для UI);
    - цикл зависимостей не бросает исключений: оставшиеся шаги попадают
      в последнюю волну по одному (их выполнение заблокирует планировщик
      цикла — см. Supervisor._blocked_entry), а не рушат всю задачу.
    """
    deps = normalize_deps(steps)
    by_id = {s.id: s for s in steps}
    done: set[str] = set()
    waves: list[list[PlanStep]] = []
    remaining = [s.id for s in steps]

    while remaining:
        ready = [sid for sid in remaining
                 if all(d in done for d in deps[sid])]
        if not ready:
            # цикл зависимостей: берём первый оставшийся шаг как «готовый»,
            # чтобы гарантировать завершение алгоритма
            logger.warning("DAG: цикл в depends_on среди %s — разрываю принудительно",
                           remaining)
            ready = [remaining[0]]
        wave = [by_id[sid] for sid in ready if sid in by_id]
        waves.append(wave)
        done.update(ready)
        remaining = [sid for sid in remaining if sid not in done]
    return waves


def deps_satisfied(step: PlanStep, results_by_step: dict[str, dict],
                   deps_map: dict[str, list[str]] | None = None) -> bool:
    """Все ли зависимости шага успешно выполнены.

    deps_map — карта зависимостей ВСЕГО плана (normalize_deps(plan.steps));
    если не передана, считается по step.depends_on как есть. Зависимость
    считается выполненной, только если success=True (упавшая зависимость
    блокирует шаг).
    """
    deps = (deps_map or {}).get(step.id, step.depends_on or [])
    for d in deps:
        entry = results_by_step.get(d)
        if entry is None or not entry.get("success"):
            return False
    return True


def wave_index_of(steps: list[PlanStep]) -> dict[str, int]:
    """{step_id: номер волны} — для событий/UI (шаги одной волны параллельны)."""
    idx: dict[str, int] = {}
    for i, wave in enumerate(plan_waves(steps), 1):
        for s in wave:
            idx[s.id] = i
    return idx

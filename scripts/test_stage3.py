#!/usr/bin/env python3
"""Тесты Этапа 3: DAG-параллелизм + долгая память + реестр планов.

Запуск: python scripts/test_stage3.py
Фейки — как в test_stage2.py; дополнительно проверяются:
- волны DAG (план/исполнение/блокировки/re-plan);
- PlanRegistry (память + диск, REST-срез view_for_client);
- долгая память (поиск событий в планировщик, итог плана в Memory);
- шина событий (plan.created/step.started/step.finished/plan.finished).
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import events as bus  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.supervisor import dag  # noqa: E402
from src.supervisor.models import PlanStep, parse_plan  # noqa: E402
from src.supervisor.plans import PlanRegistry, view_for_client  # noqa: E402
from src.supervisor.session import ConversationSession  # noqa: E402
from src.supervisor.supervisor import Supervisor  # noqa: E402

PASS, FAIL = "✓", "✗"
ok_count = fail_count = 0


def check(name: str, cond: bool) -> None:
    global ok_count, fail_count
    print(f"  {PASS if cond else FAIL} {name}")
    ok_count += bool(cond)
    fail_count += (not cond)


# ═══════════════════════════════════════════════════════════
# Фейки (как в test_stage2)
# ═══════════════════════════════════════════════════════════
def make_schema(agent_id: str, mode: str = "read", icon: str = "🤖"):
    return SimpleNamespace(
        id=agent_id, title=f"Агент {agent_id}",
        description=f"описание {agent_id}",
        mode=SimpleNamespace(value=mode),
        routing_hints=SimpleNamespace(
            keywords=[], negative_keywords=[],
            description_for_router=f"{agent_id} умеет всё про {agent_id}"),
        ui=SimpleNamespace(icon=icon),
        dangerous_tools=[], mcp_servers=[],
        runtime=SimpleNamespace(max_result_chars=30000, max_steps=5,
                                timeout_seconds=60),
    )


class FakeAgent:
    def __init__(self, agent_id: str, reply: str = "сделано",
                 ok: bool = True, error: str = "", delay: float = 0.0):
        self.schema = make_schema(agent_id)
        self.reply = reply
        self.ok = ok
        self.error = error
        self.delay = delay
        self.last_query = ""
        self.last_context = ""
        self.run_calls = 0
        self.started_at = 0.0
        self.finished_at = 0.0

    async def run(self, query, *, context_text="", **kwargs):
        self.run_calls += 1
        self.started_at = time.monotonic()
        self.last_query = query
        self.last_context = context_text
        if self.delay:
            await asyncio.sleep(self.delay)
        self.finished_at = time.monotonic()
        return SimpleNamespace(
            response=self.reply, tool_calls_count=1,
            success=self.ok, exit_reason="done" if self.ok else "error",
            error=self.error, history=[], tokens_input=0, tokens_output=0,
            duration_ms=100,
        )


class FakeRuntime:
    def __init__(self, agents: dict[str, FakeAgent]):
        self.agents = agents

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def __len__(self):
        return len(self.agents)


MARK_PLAN = "составляешь план"
MARK_OBSERVE = "наблюдающий за результатом шага"
MARK_SYNTH = "Собери результаты шагов"


class ScriptedLLM:
    """Отвечает по маркеру; observe-ответы можно задать очередью (по волнам)."""

    def __init__(self, plan: str = "", observe="", synth: str = "",
                 default: str = ""):
        self.plan_reply = plan
        self.observe_replies = ([observe] if isinstance(observe, str)
                                else list(observe))
        self.synth_reply = synth
        self.default = default
        self.user_prompts: list[str] = []
        self.system_prompts: list[str] = []

    async def chat(self, messages, **kwargs):
        sys_content = messages[0]["content"] if messages else ""
        user_content = messages[1]["content"] if len(messages) > 1 else ""
        self.system_prompts.append(sys_content)
        self.user_prompts.append(user_content)
        if MARK_PLAN in sys_content and self.plan_reply:
            return {"content": self.plan_reply}
        if MARK_OBSERVE in sys_content:
            if self.observe_replies:
                return {"content": self.observe_replies.pop(0)}
            return {"content": self.default or OBSERVE_FINISH}
        if MARK_SYNTH in sys_content and self.synth_reply:
            return {"content": self.synth_reply}
        return {"content": self.default}


PLAN_CHAIN = (
    '{"intent": "конвейер", "steps": ['
    '{"id": "1", "agent": "alpha", "task": "Прочитать данные", '
    '"depends_on": [], "why": "источник"},'
    '{"id": "2", "agent": "beta", "task": "Обработать результат шага 1", '
    '"depends_on": ["1"], "why": "конвейер"}], "reply": "", '
    '"needs_approval": false}'
)

PLAN_PARALLEL = (
    '{"intent": "параллельно", "steps": ['
    '{"id": "1", "agent": "alpha", "task": "Задача A", "depends_on": []},'
    '{"id": "2", "agent": "beta", "task": "Задача B", "depends_on": []},'
    '{"id": "3", "agent": "gamma", "task": "Свести A и B", '
    '"depends_on": ["1", "2"]}], "reply": "", "needs_approval": false}'
)

PLAN_BAD_DEPS = (
    '{"intent": "битые зависимости", "steps": ['
    '{"id": "1", "agent": "alpha", "task": "A", "depends_on": ["nope", "1"]},'
    '{"id": "2", "agent": "beta", "task": "B", "depends_on": []}], '
    '"reply": "", "needs_approval": false}'
)

OBSERVE_FINISH = ('{"action": "finish", "updated_plan": null, '
                  '"message_to_user": "исправить нельзя"}')

OBSERVE_CONTINUE = ('{"action": "continue", "updated_plan": null, '
                    '"message_to_user": ""}')

OBSERVE_REPLAN = (
    '{"action": "replan", "updated_plan": {"intent": "обход", "steps": ['
    '{"id": "1", "agent": "gamma", "task": "Сделать другим способом"}]},'
    ' "message_to_user": "меняю стратегию"}'
)


def make_sup(llm: ScriptedLLM, agents: dict, **kw) -> Supervisor:
    sup = Supervisor(Orchestrator(llm, FakeRuntime(agents)))  # type: ignore
    for k, v in kw.items():
        setattr(sup, k, v)
    return sup


async def collect_run(sup: Supervisor, query: str, **kw):
    evs: list[dict] = []

    async def emit(e: dict) -> None:
        evs.append(e)

    res = await sup.run(query, emit=emit, **kw)
    return res, evs


# ═══════════════════════════════════════════════════════════
# Тесты
# ═══════════════════════════════════════════════════════════
def test_dag_module():
    print("== dag: волны и зависимости ==")
    steps = [PlanStep(id="1", agent="a", task="t"),
             PlanStep(id="2", agent="b", task="t", depends_on=["1"]),
             PlanStep(id="3", agent="c", task="t", depends_on=["2"])]
    waves = dag.plan_waves(steps)
    check("линейная цепочка → 3 волны",
          len(waves) == 3 and [w[0].id for w in waves] == ["1", "2", "3"])

    steps = [PlanStep(id="1", agent="a", task="t"),
             PlanStep(id="2", agent="b", task="t")]
    waves = dag.plan_waves(steps)
    check("без зависимостей → одна волна (параллельно)",
          len(waves) == 1 and {s.id for s in waves[0]} == {"1", "2"})

    steps = [PlanStep(id="1", agent="a", task="t"),
             PlanStep(id="2", agent="b", task="t", depends_on=["1"]),
             PlanStep(id="3", agent="c", task="t")]
    waves = dag.plan_waves(steps)
    check("смешанный план → [[1,3],[2]]",
          len(waves) == 2 and {s.id for s in waves[0]} == {"1", "3"}
          and [s.id for s in waves[1]] == ["2"])

    steps = [PlanStep(id="1", agent="a", task="t", depends_on=["nope", "1"]),
             PlanStep(id="2", agent="b", task="t")]
    waves = dag.plan_waves(steps)
    check("неизвестные/само-зависимости отброшены",
          len(waves) == 1 and len(waves[0]) == 2)

    steps = [PlanStep(id="1", agent="a", task="t", depends_on=["2"]),
             PlanStep(id="2", agent="b", task="t", depends_on=["1"])]
    waves = dag.plan_waves(steps)
    check("цикл не зависает: все шаги в волнах",
          sum(len(w) for w in waves) == 2)

    check("is_dag_plan: пустые deps → False",
          dag.is_dag_plan([PlanStep(id="1", agent="a", task="t")]) is False)
    check("is_dag_plan: есть deps → True",
          dag.is_dag_plan(parse_plan(PLAN_CHAIN).steps) is True)

    old = dag.PARALLEL_ENABLED
    try:
        dag.PARALLEL_ENABLED = False
        check("is_dag_plan при SUPERVISOR_PARALLEL=0 → False",
              dag.is_dag_plan(parse_plan(PLAN_CHAIN).steps) is False)
    finally:
        dag.PARALLEL_ENABLED = old

    res: dict[str, dict] = {"1": {"success": False}}
    check("deps_satisfied: упавшая зависимость блокирует",
          dag.deps_satisfied(PlanStep(id="2", agent="b", task="t",
                                      depends_on=["1"]), res) is False)


async def test_chain_pipeline():
    print("== supervisor DAG: конвейер (результат 1-го — вход 2-го) ==")
    llm = ScriptedLLM(plan=PLAN_CHAIN, synth="итог конвейера")
    a1 = FakeAgent("alpha", reply="данные готовы")
    a2 = FakeAgent("beta", reply="обработано")
    sup = make_sup(llm, {"alpha": a1, "beta": a2})
    res, evs = await collect_run(sup, "прочитай и обработай")
    check("план выполнен", res["success"] is True)
    check("оба шага запускались", a1.run_calls == 1 and a2.run_calls == 1)
    check("порядок: шаг 1 до шага 2",
          a1.finished_at <= a2.started_at + 1e-6)
    check("шаг 2 получил результат шага 1 в контексте",
          "данные готовы" in a2.last_context)
    check("итог — синтез", res["message"] == "итог конвейера")
    types = [e["type"] for e in evs]
    check("события: plan + 2×step_start + plan_done",
          types.count("step_start") == 2 and "plan" in types
          and "plan_done" in types)


async def test_parallel_wave():
    print("== supervisor DAG: независимые шаги — параллельно ==")
    llm = ScriptedLLM(plan=PLAN_PARALLEL, synth="сводка")
    a1 = FakeAgent("alpha", reply="A", delay=0.08)
    a2 = FakeAgent("beta", reply="B", delay=0.08)
    a3 = FakeAgent("gamma", reply="C")
    sup = make_sup(llm, {"alpha": a1, "beta": a2, "gamma": a3})
    res, evs = await collect_run(sup, "сделай A и B, потом сведи")
    check("план выполнен", res["success"] is True)
    check("все три агента отработали",
          a1.run_calls == 1 and a2.run_calls == 1 and a3.run_calls == 1)
    # A и B стартуют до завершения друг друга → параллельность
    check("A и B выполнялись одновременно",
          a1.started_at < a2.finished_at and a2.started_at < a1.finished_at)
    check("C стартовал после A и B",
          a3.started_at >= max(a1.finished_at, a2.finished_at) - 1e-6)
    check("C получил результаты обеих зависимостей",
          "A" in a3.last_context and "B" in a3.last_context)
    statuses = [e["text"] for e in evs if e["type"] == "status"]
    check("UI-статус волны упомянул параллельность",
          any("параллельно" in s for s in statuses))
    waves = [e.get("wave") for e in evs if e["type"] == "step_start"]
    check("wave-номера в step_start (1,1,2)",
          waves == [1, 1, 2])


async def test_blocked_step():
    print("== supervisor DAG: блокировка по упавшей зависимости ==")
    # волна 1: alpha упал → наблюдатель говорит continue;
    # волна 2: gamma заблокирован → наблюдатель говорит finish
    llm = ScriptedLLM(plan=PLAN_PARALLEL,
                      observe=[OBSERVE_CONTINUE, OBSERVE_FINISH])
    a1 = FakeAgent("alpha", ok=False, error="источник недоступен")
    a2 = FakeAgent("beta", reply="B")
    a3 = FakeAgent("gamma", reply="C")
    sup = make_sup(llm, {"alpha": a1, "beta": a2, "gamma": a3})
    res, evs = await collect_run(sup, "сделай")
    check("план не успешен", res["success"] is False)
    check("beta отработал (независим)", a2.run_calls == 1)
    check("gamma заблокирован (не запускался)", a3.run_calls == 0)
    skipped = [r for r in res["results"] if r.get("skipped")]
    check("запись о пропуске с причиной", skipped
          and "зависимость" in skipped[0]["error"]
          and "1" in skipped[0]["error"])
    check("наблюдатель решил finish", "исправить нельзя" in res["message"])
    done_types = [(e["type"], e.get("step_id")) for e in evs]
    check("шаг 3 получил step_start+step_done (skipped в UI)",
          ("step_start", "3") in done_types and ("step_done", "3") in done_types)


async def test_dag_replan():
    print("== supervisor DAG: re-plan по провалу ==")
    llm = ScriptedLLM(plan=PLAN_CHAIN, observe=OBSERVE_REPLAN, synth="итог")
    a1 = FakeAgent("alpha", ok=False, error="падение шага 1")
    g = FakeAgent("gamma", reply="новым способом")
    sup = make_sup(llm, {"alpha": a1, "beta": FakeAgent("beta"),
                         "gamma": g})
    res, evs = await collect_run(sup, "сделай")
    check("re-plan вернул успех через нового агента",
          res["success"] is True and g.run_calls == 1)
    check("новые шаги переименованы r1-…",
          any(r.get("step_id") == "r1-1" for r in res["results"]))
    check("событие replan ушло в UI",
          any(e["type"] == "replan" and e["attempt"] == 1 for e in evs))


async def test_parallel_off_sequential():
    print("== выключатель: SUPERVISOR_PARALLEL=0 → прежний последовательный путь ==")
    old = dag.PARALLEL_ENABLED
    dag.PARALLEL_ENABLED = False
    try:
        llm = ScriptedLLM(plan=PLAN_PARALLEL, synth="итог")
        a1 = FakeAgent("alpha", reply="A")
        a2 = FakeAgent("beta", reply="B", delay=0.0)
        a3 = FakeAgent("gamma", reply="C")
        sup = make_sup(llm, {"alpha": a1, "beta": a2, "gamma": a3})
        res, _ = await collect_run(sup, "сделай всё")
        check("последовательно: результат шагов передан дальше",
              res["success"] is True and "A" in a2.last_context
              and "B" in a3.last_context)
    finally:
        dag.PARALLEL_ENABLED = old


def test_plan_registry():
    print("== PlanRegistry: память + диск ==")
    tmp = Path(tempfile.mkdtemp(prefix="plans_test_"))
    try:
        reg = PlanRegistry(tmp)
        rec = reg.create("p1", session_id="s1", query="тест", intent="цель",
                         mode="dag",
                         steps=[{"id": "1", "agent": "alpha", "task": "A",
                                 "status": "pending"}])
        check("create: статус running", rec["status"] == "running")
        reg.set_step("p1", "1", status="done", summary="готово")
        reg.update("p1", status="done", success=True, message="всё",
                   replans=1)
        got = reg.get("p1")
        check("update/set_step применились",
              got["steps"][0]["status"] == "done" and got["success"] is True
              and got["replans"] == 1)
        check("set_step создаёт отсутствующий шаг",
              reg.set_step("p1", "9", status="error") is not None)

        # рестарт: новый реестр читает с диска
        reg2 = PlanRegistry(tmp)
        got2 = reg2.get("p1")
        check("после 'перезапуска' план читается с диска",
              got2 is not None and got2["intent"] == "цель")
        check("list подмешивает с диска",
              len(reg2.list(limit=10)) == 1)
        check("фильтр по session_id",
              reg2.list(limit=10, session_id="s1") and
              not reg2.list(limit=10, session_id="other"))
        check("for_session возвращает последний план сессии",
              reg2.for_session("s1")["plan_id"] == "p1")
        check("get неизвестного → None", reg2.get("nope") is None)

        v = view_for_client(got2)
        check("view_for_client: срез для клиента",
              v["plan_id"] == "p1" and v["mode"] == "dag"
              and v["steps"][0]["status"] == "done"
              and "success" in v and "message" in v)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_supervisor_with_registry_and_events():
    print("== Supervisor + реестр планов + шина событий ==")
    tmp = Path(tempfile.mkdtemp(prefix="plans_sup_"))
    bus.clear()
    seen: list[dict] = []
    bus.subscribe("*", lambda e: seen.append(e) or asyncio.sleep(0))
    try:
        llm = ScriptedLLM(plan=PLAN_CHAIN, synth="итог")
        sup = make_sup(llm, {"alpha": FakeAgent("alpha", reply="A"),
                             "beta": FakeAgent("beta", reply="B")})
        reg = PlanRegistry(tmp)
        session = ConversationSession()
        res, _ = await collect_run(sup, "прочитай и обработай",
                                   session=session, plan_registry=reg)
        rec = reg.get(res["trace_id"])
        check("план зарегистрирован", rec is not None)
        check("статус финальный done + success",
              rec["status"] == "done" and rec["success"] is True)
        check("шаги в реестре обновлены",
              all(s["status"] == "done" for s in rec["steps"]))
        check("session_id привязан к плану",
              rec["session_id"] == session.id)
        check("mode=dag (есть depends_on)", rec["mode"] == "dag")
        kinds = [e["kind"] for e in seen]
        check("шина: plan.created и plan.finished",
              "plan.created" in kinds and "plan.finished" in kinds)
        check("шина: step.started/step.finished",
              kinds.count("step.started") == 2
              and kinds.count("step.finished") == 2)
        await asyncio.sleep(0.01)
    finally:
        bus.clear()
        shutil.rmtree(tmp, ignore_errors=True)


class FakeMemory:
    enabled = True

    def __init__(self, found=None):
        self.found = found or []
        self.logged: list[dict] = []
        self.searched: list[str] = []

    async def search_events_async(self, query, limit=10, **kw):
        self.searched.append(query)
        return self.found[:limit]

    async def log_event_async(self, type_, summary, **kw):
        self.logged.append({"type": type_, "summary": summary, **kw})
        return 1


async def test_long_memory():
    print("== долгая память: планировщик + итог плана ==")
    mem = FakeMemory(found=[{"summary": "прошлый план: чинили сборку"}])
    llm = ScriptedLLM(plan=PLAN_CHAIN, synth="итог")
    sup = make_sup(llm, {"alpha": FakeAgent("alpha"), "beta": FakeAgent("beta")})
    res, _ = await collect_run(sup, "прочитай и обработай", memory=mem)
    check("план выполнен", res["success"] is True)
    plan_prompt = next(p for p, s in zip(llm.user_prompts, llm.system_prompts)
                       if MARK_PLAN in s)
    check("прошлые события попали в промпт планировщика",
          "долгая память" in plan_prompt
          and "чинили сборку" in plan_prompt)
    check("поиск по запросу пользователя", mem.searched
          and "прочитай" in mem.searched[0])
    check("итог плана ушёл в память",
          mem.logged and mem.logged[0]["type"] == "plan_finished"
          and "итог" in mem.logged[0]["summary"])

    llm2 = ScriptedLLM(plan=PLAN_CHAIN)
    sup2 = make_sup(llm2, {"alpha": FakeAgent("alpha"),
                           "beta": FakeAgent("beta")})
    await collect_run(sup2, "без памяти")
    plan_prompt2 = next(p for p, s in zip(llm2.user_prompts, llm2.system_prompts)
                        if MARK_PLAN in s)
    check("без памяти промпт не ломается (нет секции)",
          "долгая память" not in plan_prompt2)


async def test_budget_replan_dag():
    print("== бюджеты DAG: лимит re-plan ==")
    llm = ScriptedLLM(plan=PLAN_CHAIN, observe=[OBSERVE_REPLAN] * 3)
    a1 = FakeAgent("alpha", ok=False, error="вечно падаю")
    g = FakeAgent("gamma", ok=False, error="и я падаю")
    sup = make_sup(llm, {"alpha": a1, "beta": FakeAgent("beta"), "gamma": g})
    res, _ = await collect_run(sup, "сделай")
    check("останов по лимиту перепланировок",
          res["success"] is False and "лимит перепланировок" in res["message"])
    check("агент вызывался ограниченно (1+2 re-plan)",
          a1.run_calls + g.run_calls <= 4)


# ═══════════════════════════════════════════════════════════
async def main() -> int:
    test_dag_module()
    await test_chain_pipeline()
    await test_parallel_wave()
    await test_blocked_step()
    await test_dag_replan()
    await test_parallel_off_sequential()
    test_plan_registry()
    await test_supervisor_with_registry_and_events()
    await test_long_memory()
    await test_budget_replan_dag()
    print(f"\nИтог: {ok_count} ок, {fail_count} падений")
    return 1 if fail_count else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

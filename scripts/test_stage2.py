#!/usr/bin/env python3
"""Тесты Этапа 2: Supervisor (Plan → Execute → Observe → Re-plan).

Запуск: python scripts/test_stage2.py
Фейковый LLM отвечает по маркерам системного промпта (план/наблюдение/
синтез), агенты — объекты с run(), совместимым с BaseAgent.run().
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import Orchestrator  # noqa: E402
from src.supervisor.models import (  # noqa: E402
    parse_observe,
    parse_plan,
)
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
# Фейки
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
                 ok: bool = True, error: str = "", mode: str = "read",
                 icon: str = "🤖"):
        self.schema = make_schema(agent_id, mode=mode, icon=icon)
        self.reply = reply
        self.ok = ok
        self.error = error
        self.last_query = ""
        self.last_context = ""
        self.run_calls = 0

    async def run(self, query, *, context_text="", **kwargs):
        self.run_calls += 1
        self.last_query = query
        self.last_context = context_text
        # имитация стрима: токены через on_token (как LLMClient)
        on_token = kwargs.get("on_token")
        if on_token:
            for part in ("готовлю ", "результат ", "шага"):
                try:
                    await on_token(part)
                except Exception:
                    pass
        return SimpleNamespace(
            response=self.reply, tool_calls_count=2,
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


# маркеры системных промптов Supervisor → сценарий ответа
MARK_PLAN = "составляешь план"
MARK_OBSERVE = "наблюдающий за результатом шага"
MARK_SYNTH = "Собери результаты шагов"


class ScriptedLLM:
    """Отвечает по маркеру в системном промпте; иначе — default."""

    def __init__(self, plan: str = "", observe: str = "", synth: str = "",
                 default: str = ""):
        self.plan_reply = plan
        self.observe_reply = observe
        self.synth_reply = synth
        self.default = default
        self.calls: list[list[dict]] = []
        self.fail_on: str | None = None  # маркер, при котором кидаем ошибку

    async def chat(self, messages, **kwargs):
        sys_content = messages[0]["content"] if messages else ""
        self.calls.append(messages)
        marker = None
        if MARK_PLAN in sys_content:
            marker = MARK_PLAN
        elif MARK_OBSERVE in sys_content:
            marker = MARK_OBSERVE
        elif MARK_SYNTH in sys_content:
            marker = MARK_SYNTH
        if marker and marker == self.fail_on:
            raise RuntimeError("connection refused by fake provider")
        for m, reply in ((MARK_PLAN, self.plan_reply),
                         (MARK_OBSERVE, self.observe_reply),
                         (MARK_SYNTH, self.synth_reply)):
            if marker == m and reply:
                return {"content": reply}
        return {"content": self.default}


PLAN_2STEPS = (
    '{"intent": "проверить и собрать", "steps": ['
    '{"id": "1", "agent": "file", "task": "Проверить синтаксис в src/", '
    '"depends_on": [], "why": "сначала код"},'
    '{"id": "2", "agent": "build", "task": "Собрать пакет из исправленного кода", '
    '"depends_on": ["1"], "why": "потом сборка"}], "reply": "", '
    '"needs_approval": false}'
)

PLAN_EMPTY = ('{"intent": "болтовня", "steps": [], '
              '"reply": "Привет! Чем помочь?", "needs_approval": false}')

PLAN_DESTRUCTIVE = (
    '{"intent": "опасное", "steps": ['
    '{"id": "1", "agent": "deploy", "task": "Задеплоить", "depends_on": []}],'
    ' "reply": "", "needs_approval": true}'
)

PLAN_UNKNOWN_AGENT = (
    '{"intent": "x", "steps": ['
    '{"id": "1", "agent": "nosuch", "task": "что-то"},'
    '{"id": "2", "agent": "file", "task": "что-то рабочее"}], '
    '"reply": "", "needs_approval": false}'
)

OBSERVE_REPLAN = (
    '{"action": "replan", "updated_plan": {"intent": "обход", "steps": ['
    '{"id": "1", "agent": "shell", "task": "Собрать через shell в обход сбоя"}]},'
    ' "message_to_user": "сборка упала — меняю стратегию"}'
)

OBSERVE_REPLAN_BUILD = (
    '{"action": "replan", "updated_plan": {"intent": "повтор", "steps": ['
    '{"id": "1", "agent": "build", "task": "Повторить сборку с чистого кэша"}]},'
    ' "message_to_user": "повторяю сборку"}'
)

OBSERVE_FINISH = ('{"action": "finish", "updated_plan": null, '
                  '"message_to_user": "исправить нельзя"}')

OBSERVE_CONTINUE = '{"action": "continue", "updated_plan": null, "message_to_user": ""}'


def make_orch(llm, agents: dict[str, FakeAgent]) -> Orchestrator:
    return Orchestrator(llm, FakeRuntime(agents))  # type: ignore[arg-type]


async def run_sup(sup: Supervisor, query: str, **kw) -> tuple[dict, list[dict]]:
    res = await sup.run(query, **kw)
    return res, list(sup.events)


# ═══════════════════════════════════════════════════════════
# Тесты
# ═══════════════════════════════════════════════════════════
async def test_models_parsing():
    print("== models: парсинг планов и решений ==")
    p = parse_plan("```json\n" + PLAN_2STEPS + "\n```")
    check("план из markdown-ограждения", p is not None and len(p.steps) == 2
          and p.steps[1].depends_on == ["1"])
    p = parse_plan("мусор до JSON " + PLAN_EMPTY + " мусор после")
    check("regex-fallback первого JSON-объекта",
          p is not None and p.reply == "Привет! Чем помочь?")
    check("мусор без JSON → None", parse_plan("совсем нет json") is None)
    p = parse_plan('{"intent":"x","steps":[{"id":"1","agent":"file"}]}')
    check("шаг без task → отброшен, план остался", p is not None
          and len(p.steps) == 0)

    d = parse_observe(OBSERVE_REPLAN)
    check("observe-replan распарсен", d is not None
          and d.action == "replan"
          and d.updated_plan is not None
          and d.updated_plan.steps[0].agent == "shell")
    d = parse_observe("ерунда")
    check("мусор observe → None", d is None)

    # мусорный шаг внутри updated_plan выживает частично
    d = parse_observe(
        '{"action": "replan", "updated_plan": {"steps": ['
        '{"id": "1", "agent": "nosuch", "task": "x"}, '
        '{"id": "2", "agent": "file", "task": "y"}]}}')
    check("updated_plan: валидные шаги выживают (фильтр агентов — у Supervisor)",
          d is not None and d.updated_plan is not None
          and len(d.updated_plan.steps) == 2
          and d.updated_plan.steps[1].agent == "file")


async def test_direct_plan_mention():
    print("== @упоминание: план без LLM ==")
    llm = ScriptedLLM()
    agents = {"file": FakeAgent("file", reply="проверил, всё чисто")}
    sup = Supervisor(make_orch(llm, agents))
    res, ev = await run_sup(
        sup, "@file проверь синтаксис",
        session=None, suggested_agents=["file"],
        intent_source="mention", intent_reason="явное @упоминание")

    check("1 шаг исполнен", len(res["results"]) == 1
          and res["results"][0]["agent"] == "file")
    check("LLM не вызывался вовсе", len(llm.calls) == 0)
    check("шаг получил сырой запрос как task",
          agents["file"].last_query == "проверь синтаксис")
    kinds = [e["type"] for e in ev]
    tok_ev = next(e for e in ev if e["type"] == "token")
    check("порядок plan→step_start→token→step_done→plan_done",
          "plan" in kinds and "route" in kinds
          and kinds.index("plan") < kinds.index("step_start")
          < kinds.index("token") < kinds.index("step_done")
          < kinds.index("plan_done"))
    check("token привязан к шагу", tok_ev.get("step_id") == "1")
    check("plan_done success", ev[-1]["success"] is True)
    check("итог = ответ агента", res["message"] == "проверил, всё чисто")


async def test_planner_multistep():
    print("== LLM-планировщик: 2 шага + пайплайн контекста + синтез ==")
    llm = ScriptedLLM(plan=PLAN_2STEPS,
                      synth="Проверил синтаксис и собрал пакет — всё зелёное.")
    agents = {"file": FakeAgent("file", reply="синтаксис чист"),
              "build": FakeAgent("build", reply="wheel собран")}
    sup = Supervisor(make_orch(llm, agents))
    session = ConversationSession()
    res, ev = await run_sup(
        sup, "проверь синтаксис и собери пакет",
        session=session, intent_source="keywords",
        suggested_agents=["file"])

    check("оба шага исполнены по порядку",
          [r["agent"] for r in res["results"]] == ["file", "build"])
    check("планировщик получил каталог агентов",
          len(llm.calls) > 0
          and "file умеет всё про file" in llm.calls[0][1]["content"])
    check("2-й шаг видел результат 1-го",
          "синтаксис чист" in agents["build"].last_context)
    check("задача шага ≠ сырой запрос",
          agents["build"].last_query == "Собрать пакет из исправленного кода")
    check("синтез LLM использован",
          res["message"] == "Проверил синтаксис и собрал пакет — всё зелёное.")
    check("токены размечены step_id 2",
          any(e["type"] == "token" and e.get("step_id") == "2"
              for e in ev))
    kinds = [e["type"] for e in ev]
    check("события идут парами step_start/step_done",
          kinds.count("step_start") == 2 and kinds.count("step_done") == 2)
    check("route-событие для совместимости",
          any(e["type"] == "route" and e["agents"] == ["file", "build"]
              for e in ev))
    check("сессия: активный план со статусами done",
          session.active_plan is not None
          and all(s["status"] == "done"
                  for s in session.active_plan["steps"]))
    check("сессия: last_agents", session.last_agents == ["file", "build"])
    check("сессия: итог в истории",
          session.history[-1].content == res["message"])
    check("step_results сжаты в сессии",
          "1" in session.step_results
          and session.step_results["1"]["success"] is True)


async def test_empty_plan():
    print("== Пустой план: прямой ответ без агентов ==")
    llm = ScriptedLLM(plan=PLAN_EMPTY)
    agents = {"file": FakeAgent("file")}
    sup = Supervisor(make_orch(llm, agents))
    session = ConversationSession()
    res, ev = await run_sup(sup, "расскажи что умеешь", session=session)

    check("агенты не запускались", all(a.run_calls == 0
                                       for a in agents.values()))
    check("ответ = reply из плана", res["message"] == "Привет! Чем помочь?")
    check("success=True", res["success"] is True)
    check("план-событие с 0 шагов", any(
        e["type"] == "plan" and e["steps"] == [] for e in ev))
    check("ответ попал в сессию", session.history[-1].content
          == "Привет! Чем помочь?")


async def test_replan_on_failure():
    print("== Observe: провал шага → re-plan → успех ==")
    llm = ScriptedLLM(
        plan='{"intent":"собрать","steps":[{"id":"1","agent":"build",'
             '"task":"Собрать пакет"}],"reply":"","needs_approval":false}',
        observe=OBSERVE_REPLAN,
        synth="Собрал через shell после обхода сбоя.")
    agents = {"build": FakeAgent("build", ok=False, error="npm ERR! timeout"),
              "shell": FakeAgent("shell", reply="docker образ собран")}
    sup = Supervisor(make_orch(llm, agents))
    session = ConversationSession()
    res, ev = await run_sup(sup, "собери проект", session=session)

    check("первый шаг упал, второй — shell",
          [r["agent"] for r in res["results"]] == ["build", "shell"])
    check("replan-событие с новым шагом", any(
        e["type"] == "replan" and e["attempt"] == 1
        and e["steps"][0]["agent"] == "shell" for e in ev))
    check("наблюдатель получил ошибку шага",
          "npm ERR! timeout" in llm.calls[1][1]["content"])
    check("новый шаг из updated_plan исполнен (id с префиксом r1-)",
          res["results"][1]["step_id"] == "r1-1"
          and res["results"][1]["success"] is True)
    check("итоговый success", res["success"] is True
          and res["replans"] == 1)
    check("сессия: активный план дополнен шагом re-plan",
          any(s["agent"] == "shell" for s in session.active_plan["steps"]))
    check("сессия: статус упавшего шага = error",
          session.step_results["1"]["success"] is False)


async def test_replan_budget_and_finish():
    print("== Бюджет re-plan и finish от наблюдателя ==")
    llm = ScriptedLLM(
        plan='{"intent":"собрать","steps":[{"id":"1","agent":"build",'
             '"task":"Собрать"}],"reply":"","needs_approval":false}',
        observe=OBSERVE_REPLAN_BUILD)
    agents = {"build": FakeAgent("build", ok=False, error="npm ERR!"),
              "shell": FakeAgent("shell", ok=False, error="docker dead")}
    sup = Supervisor(make_orch(llm, agents))
    res, ev = await run_sup(sup, "собери проект")

    check("ровно MAX_REPLANS перепланировок", res["replans"] == 2)
    check("после лимита — стоп с сообщением",
          "перепланировок" in res["message"] and res["success"] is False)
    check("не бесконечный цикл (агент build вызван 3 раза)",
          agents["build"].run_calls == 3)

    # finish от наблюдателя
    llm2 = ScriptedLLM(
        plan='{"intent":"собрать","steps":[{"id":"1","agent":"build",'
             '"task":"Собрать"}],"reply":"","needs_approval":false}',
        observe=OBSERVE_FINISH)
    sup2 = Supervisor(make_orch(llm2, dict(agents)))
    res2, ev2 = await run_sup(sup2, "собери проект")
    check("finish: message_to_user наблюдателя",
          res2["message"] == "исправить нельзя" and not res2["success"])
    check("finish: plan_done success=False", any(
        e["type"] == "plan_done" and e["success"] is False for e in ev2))

    # наблюдатель недоступен (LLM упал) → finish с дружелюбной ошибкой
    llm3 = ScriptedLLM(
        plan='{"intent":"собрать","steps":[{"id":"1","agent":"build",'
             '"task":"Собрать"}],"reply":"","needs_approval":false}',
        observe=OBSERVE_FINISH)
    llm3.fail_on = MARK_OBSERVE
    sup3 = Supervisor(make_orch(llm3, {"build": FakeAgent(
        "build", ok=False, error="npm ERR!")}))
    res3, _ = await run_sup(sup3, "собери проект")
    check("наблюдатель упал → finish с ошибкой шага",
          "npm ERR!" in res3["message"] and not res3["success"])


async def test_plan_approval():
    print("== Утверждение плана (needs_approval / destructive) ==")
    llm = ScriptedLLM(plan=PLAN_DESTRUCTIVE)
    agents = {"deploy": FakeAgent("deploy", mode="destructive",
                                  reply="задеплоено")}
    approved_flag = {"v": None}

    async def approver(plan_dict) -> bool:
        approved_flag["v"] = plan_dict
        return False  # отказ

    sup = Supervisor(make_orch(llm, agents))
    res, ev = await run_sup(sup, "задеплой", plan_approval_handler=approver)

    check("апрувер вызван", approved_flag["v"] is not None
          and approved_flag["v"]["steps"][0]["agent"] == "deploy")
    check("после отказа агенты не запускались",
          agents["deploy"].run_calls == 0)
    check("сообщение об остановке", "не утвержд" in res["message"])
    check("plan_done success=False", ev[-1]["success"] is False)

    # апрув → исполняется
    async def approver_ok(plan_dict) -> bool:
        return True

    sup2 = Supervisor(make_orch(ScriptedLLM(plan=PLAN_DESTRUCTIVE), agents))
    res2, _ = await run_sup(sup2, "задеплой",
                            plan_approval_handler=approver_ok)
    check("после апрува шаг исполнен", res2["success"] is True
          and agents["deploy"].run_calls == 1)

    # destructive-эвристика без needs_approval от LLM
    llm3 = ScriptedLLM(plan=(
        '{"intent":"x","steps":[{"id":"1","agent":"deploy",'
        '"task":"Почистить"}],"reply":"","needs_approval":false}'))
    calls = {"n": 0}

    async def approver_count(plan_dict) -> bool:
        calls["n"] += 1
        return True

    sup3 = Supervisor(make_orch(llm3, agents))
    await run_sup(sup3, "почисти", plan_approval_handler=approver_count)
    check("эвристика destructive → апрув без LLM-флага", calls["n"] == 1)


async def test_budgets_and_validation():
    print("== Бюджеты шагов и валидация агентов ==")
    # бесконечные re-plan упираются в лимит шагов плана (бюджет в цикле)
    llm = ScriptedLLM(
        plan='{"intent":"много","steps":['
             + ",".join(f'{{"id":"{i}","agent":"build",'
                        f'"task":"Собрать часть {i}"}}' for i in range(1, 5))
             + '],"reply":"","needs_approval":false}',
        observe=OBSERVE_REPLAN_BUILD)
    agents = {"build": FakeAgent("build", ok=False, error="npm ERR!")}
    sup = Supervisor(make_orch(llm, agents))
    sup.MAX_STEPS = 5
    sup.MAX_REPLANS = 10
    res, ev = await run_sup(sup, "сделай всё")

    check("выполнено ровно MAX_STEPS шагов", len(res["results"]) == 5)
    check("сообщение о лимите шагов", "лимит шагов" in res["message"])
    check("успех = False при остановке по бюджету", res["success"] is False)
    check("id шагов re-plan уникальны",
          len({r["step_id"] for r in res["results"]}) == 5)

    # план из 15 шагов обрезается на этапе планирования
    llm_t = ScriptedLLM(plan=(
        '{"intent":"много","steps":['
        + ",".join(f'{{"id":"{i}","agent":"file","task":"шаг {i}"}}'
                   for i in range(1, 16))
        + '],"reply":"","needs_approval":false}'))
    sup_t = Supervisor(make_orch(llm_t, {"file": FakeAgent("file")}))
    res_t, _ = await run_sup(sup_t, "сделай всё")
    check("план обрезан до MAX_STEPS при планировании",
          len(res_t["results"]) == sup_t.MAX_STEPS
          and res_t["success"] is True)

    # неизвестный агент отброшен, рабочий остался
    llm2 = ScriptedLLM(plan=PLAN_UNKNOWN_AGENT)
    agents2 = {"file": FakeAgent("file", reply="ок")}
    sup2 = Supervisor(make_orch(llm2, agents2))
    res2, ev2 = await run_sup(sup2, "что-то сделай")
    check("шаг с неизвестным агентом отброшен",
          [r["agent"] for r in res2["results"]] == ["file"])

    # планировщик вернул мусор → деградация с понятным ответом
    llm3 = ScriptedLLM(plan="я не понял запрос, вот текст вместо json")
    sup3 = Supervisor(make_orch(llm3, {"file": FakeAgent("file")}))
    res3, _ = await run_sup(sup3, "что-то сделай")
    check("мусор от планировщика → деградация без падения",
          res3["success"] is False and "переформулировать" in res3["message"])

    # планировщик недоступен → сообщение с подсказкой
    llm4 = ScriptedLLM(plan="x")
    llm4.fail_on = MARK_PLAN
    sup4 = Supervisor(make_orch(llm4, {"file": FakeAgent("file")}))
    res4, _ = await run_sup(sup4, "что-то сделай")
    check("упавший планировщик → подсказка про провайдера",
          "провайдер LLM недоступен" in res4["message"])


async def test_session_plan_context():
    print("== Сессия: план-контекст для следующего планирования ==")
    llm = ScriptedLLM(plan=PLAN_2STEPS, synth="итог")
    agents = {"file": FakeAgent("file", reply="синтаксис чист"),
              "build": FakeAgent("build", reply="wheel собран")}
    sup = Supervisor(make_orch(llm, agents))
    session = ConversationSession()
    await run_sup(sup, "проверь и собери", session=session)

    ctx = session.plan_context_for_planner()
    check("план-контекст содержит статусы",
          "[готово]" in ctx and "Шаг 2" in ctx)
    s2 = ConversationSession()
    s2.set_active_plan({"intent": "x", "steps": [
        {"id": "1", "agent": "file", "task": "a", "status": "done"},
        {"id": "2", "agent": "build", "task": "b", "status": "pending"}]})
    s2.record_step_result("2", {"agent": "build", "task": "b",
                                "success": False, "content": "",
                                "error": "падение", "tool_calls": 1})
    check("record_step_result меняет статус на error",
          s2.active_plan["steps"][1]["status"] == "error")
    check("план закрыт (done+error) → план не открыт", not s2.plan_is_open())
    ctx2 = s2.plan_context_for_planner()
    check("ошибка шага видна в контексте", "падение" in ctx2)

    # dump/load плана (аддитивный формат)
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        s3 = ConversationSession(session_id="p1", dump_dir=td)
        s3.set_active_plan({"intent": "x", "steps": [
            {"id": "1", "agent": "file", "task": "a", "status": "pending"}]})
        s3.add_user("привет")
        s3.record_step_result("1", {"agent": "file", "task": "a",
                                    "success": True, "content": "ок",
                                    "error": "", "tool_calls": 0})
        s4 = ConversationSession(session_id="p1", dump_dir=td)
        loaded = s4.load()
        check("дамп плана/шага загружен", s4.active_plan is not None
              and "1" in s4.step_results and loaded == 1)
        check("старый формат (msg) совместим", s4.history[0].role == "user")


async def main():
    await test_models_parsing()
    await test_direct_plan_mention()
    await test_planner_multistep()
    await test_empty_plan()
    await test_replan_on_failure()
    await test_replan_budget_and_finish()
    await test_plan_approval()
    await test_budgets_and_validation()
    await test_session_plan_context()
    print(f"\nИтого: {ok_count} OK, {fail_count} FAIL")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

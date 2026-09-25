#!/usr/bin/env python3
"""Тесты Этапа 1: ConversationSession + Intent Layer + route(history).

Запуск: python scripts/test_stage1.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.orchestrator import Orchestrator  # noqa: E402
from src.supervisor import (  # noqa: E402
    ConversationSession,
    build_agent_cards,
    classify,
)

PASS, FAIL = "✓", "✗"
ok_count = fail_count = 0


def check(name: str, cond: bool) -> None:
    global ok_count, fail_count
    print(f"  {PASS if cond else FAIL} {name}")
    ok_count += bool(cond)
    fail_count += (not cond)


# Тестовые карточки агентов (как в реальном routing_hints)
CARDS = [
    {"id": "file", "keywords": ["файл", "файлы", "директория", "копир"],
     "negative_keywords": []},
    {"id": "build", "keywords": ["сборка", "build", "npm install", "docker"],
     "negative_keywords": ["1с"]},
    {"id": "testing", "keywords": ["тест", "тесты", "pytest", "прогони"],
     "negative_keywords": []},
    {"id": "onec", "keywords": ["1с", "конфигурация", "обработка"],
     "negative_keywords": ["linux"]},
]


class FakeRuntime:
    def __init__(self, agents):
        self.agents = {a: object() for a in agents}

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def __len__(self):
        return len(self.agents)


class FakeLLM:
    def __init__(self, reply='{"agents": ["testing"], "reason": "r"}'):
        self.reply = reply
        self.last_messages = None
        self.chat_calls = 0

    async def chat(self, messages, **kwargs):
        self.chat_calls += 1
        self.last_messages = messages
        return {"content": self.reply}


async def test_session():
    print("== Session: история и контекст роутера ==")
    s = ConversationSession()
    s.add_user("проверь синтаксис в src")
    s.add_assistant("проверил, 2 ошибки", agent="file")
    s.add_user("исправь их")
    h = s.history_for_router()
    check("история содержит все 3 сообщения",
          "проверь синтаксис" in h and "Агент [file]" in h and "исправь" in h)
    check("last_agents из add_assistant(агент=...)", s.last_agents == ["file"])
    s.set_last_agents(["file", "build"])
    check("set_last_agents", s.last_agents == ["file", "build"])
    check("пустая сессия → пустая история",
          ConversationSession().history_for_router() == "")

    # обрезка длинных сообщений
    s2 = ConversationSession()
    s2.add_user("x" * 1000)
    h2 = s2.history_for_router()
    check("длинное сообщение обрезано", len(h2) < 500 and "…" in h2)

    # дамп/загрузка
    with tempfile.TemporaryDirectory() as td:
        s3 = ConversationSession(session_id="test_sess", dump_dir=td)
        s3.add_user("сохранись")
        s3.add_assistant("ок", agent="build")
        s4 = ConversationSession(session_id="test_sess", dump_dir=td)
        loaded = s4.load()
        check("дамп загружен (2 сообщения)", loaded == 2
              and s4.history[1].agent == "build")
        check("last_agents восстановлены", s4.last_agents == ["build"])


async def test_intents():
    print("== Intent Layer: 5 путей ==")

    # 1. @упоминание
    d = classify("@file скопируй конфиг в бэкап", None, CARDS)
    check("mention → file, 0.95",
          d.agents == ["file"] and d.source == "mention"
          and not d.needs_llm_routing)

    # 1b. неизвестное @упоминание → не считается
    d = classify("@nobody привет", None, CARDS)
    check("неизвестный @ → не mention", d.source != "mention")

    # 2. болтовня
    for phrase in ("привет", "спасибо большое", "ок"):
        d = classify(phrase, None, CARDS)
        check(f"smalltalk «{phrase}»", d.source == "smalltalk"
              and d.agents == [] and not d.needs_llm_routing)

    # 2b. длинное сообщение со словом «привет» — НЕ болтовня
    d = classify("привет, проверь сборку проекта и запусти npm install",
                 None, CARDS)
    check("длинное с «привет» → не smalltalk", d.source != "smalltalk")

    # 3. продолжение
    s = ConversationSession()
    s.add_user("проверь файлы")
    s.add_assistant("готово", agent="file")
    s.set_last_agents(["file"])
    d = classify("продолжай", s, CARDS)
    check("«продолжай» → file (continuation)",
          d.agents == ["file"] and d.source == "continuation")
    d = classify("а теперь исправь", s, CARDS)
    check("«а теперь» → file", d.agents == ["file"]
          and d.source == "continuation")

    # 3b. «продолжай» без истории → не continuation
    d = classify("продолжай", ConversationSession(), CARDS)
    check("«продолжай» без истории → low_confidence",
          d.source == "low_confidence" and d.needs_llm_routing)

    # 4. ключевые слова
    d = classify("запусти сборку проекта", None, CARDS)
    check("keywords «сборку» → build", d.agents == ["build"]
          and d.source == "keywords")
    d = classify("прогони тесты пожалуйста", None, CARDS)
    check("keywords «тесты» → testing", d.agents == ["testing"])
    d = classify("docker compose up", None, CARDS)
    check("keywords docker → build", d.agents == ["build"])

    # 4b. негативные ключевые слова
    d = classify("проверь конфигурацию 1с на linux", None, CARDS)
    check("негатив «linux» гасит onec",
          d.agents != ["onec"] or d.source == "low_confidence")

    # 5. low confidence
    d = classify("расскажи что умеешь", None, CARDS)
    check("неизвестное → low_confidence (LLM-роутинг)",
          d.source == "low_confidence" and d.needs_llm_routing)

    # карточки из snapshot-подобного объекта
    class _St:
        class schema:  # noqa: N801
            routing_hints = type("H", (), {
                "keywords": ["тест"], "negative_keywords": [],
            })()
    class _Snap:
        agents = {"testing": _St()}
    cards = build_agent_cards(_Snap(), active_ids={"testing"})
    check("build_agent_cards из snapshot", cards
          and cards[0]["id"] == "testing" and cards[0]["keywords"] == ["тест"])


async def test_route_history():
    print("== route(): история попадает в промпт ==")
    llm = FakeLLM()
    orch = Orchestrator(llm, FakeRuntime(["testing", "file", "build"]))
    history = "Пользователь: проверь файлы\nАгент [file]: готово"
    await orch.route("исправь их", history=history)
    sent = llm.last_messages[-1]["content"]
    check("история в промпте роутера", "История диалога" in sent
          and "Агент [file]: готово" in sent and "исправь их" in sent)

    await orch.route("просто запрос")
    sent2 = llm.last_messages[-1]["content"]
    check("без истории промпт как раньше", "История диалога" not in sent2)


async def main():
    await test_session()
    await test_intents()
    await test_route_history()
    print(f"\nИтого: {ok_count} OK, {fail_count} FAIL")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

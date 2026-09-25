"""Intent Layer — быстрый путь маршрутизации до LLM (Этап 1, V2 §3.3).

Детерминированные эвристики (миллисекунды, ноль токенов):

  1. @упоминание агента   → прямое назначение
  2. болтовня (привет…)   → пустой выбор (прямой ответ без агентов)
  3. продолжение диалога  → агенты прошлого шага
  4. скоринг keywords из routing_hints деклараций (уже в agent.yaml)
  5. иначе low_confidence → LLM-роутинг с историей (как раньше)

Бонус: при недоступном LLM-провайдере шаги 1–4 продолжают работать.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ── Словари эвристик (рус + латиница, нижний регистр) ──────────
CONTINUATION_PATTERNS: tuple[str, ...] = (
    "продолжай", "продолжить", "продолжи", "дальше", "ещё раз", "еще раз",
    "повтори", "снова", "а теперь", "и теперь", "теперь ", "продолжение",
    "go on", "continue", "next step",
)

SMALLTALK_PATTERNS: tuple[str, ...] = (
    "привет", "здравствуй", "здаров", "добрый день", "добрый вечер",
    "доброе утро", "спасибо", "благодарю", "спс", "отлично", "супер",
    "круто", "класс", "хорошо", "ок", "окей", "ага", "угу", "пока",
    "до свидания", "hello", "hi ", "hey", "thanks", "thank you", "bye",
)

#: длиннее этого — точно не болтовня
SMALLTALK_MAX_LEN = 40

MENTION_RE = re.compile(r"@([a-zA-Z0-9_а-яё-]+)")


@dataclass
class IntentDecision:
    """Решение Intent Layer: кого запускать (или кого спросить дальше)."""

    agents: list[str] = field(default_factory=list)
    source: str = "low_confidence"   # mention|smalltalk|continuation|keywords|low_confidence
    confidence: float = 0.0
    reason: str = ""

    @property
    def needs_llm_routing(self) -> bool:
        """True — Intent Layer не уверен, нужен LLM-роутинг."""
        return self.source == "low_confidence"


def build_agent_cards(
    snapshot: Any,
    active_ids: set[str] | list[str] | None = None,
) -> list[dict]:
    """Карточки агентов для скоринга из snapshot реестра.

    card = {id, keywords, negative_keywords}; только активные агенты,
    если active_ids передан.
    """
    cards: list[dict] = []
    if snapshot is None:
        return cards
    for aid, st in getattr(snapshot, "agents", {}).items():
        if active_ids is not None and aid not in set(active_ids):
            continue
        hints = getattr(getattr(st, "schema", None), "routing_hints", None)
        hints = hints or {}
        cards.append({
            "id": aid,
            "keywords": [k.lower() for k in (hints.keywords or [])],
            "negative_keywords": [k.lower() for k in (hints.negative_keywords or [])],
        })
    return cards


def classify(
    query: str,
    session: Any = None,
    agent_cards: list[dict] | None = None,
) -> IntentDecision:
    """Классифицирует запрос пользователя (Этап 1: без LLM)."""
    q = (query or "").strip()
    ql = q.lower()
    cards = agent_cards or []

    # ── 1. @упоминания ──────────────────────────────────────
    mentioned = [m for m in MENTION_RE.findall(q) if _known(m, cards)]
    if mentioned:
        return IntentDecision(
            agents=mentioned,
            source="mention",
            confidence=0.95,
            reason="явное @упоминание агента",
        )

    # ── 2. Болтовня: короткое сообщение-вежливость ──────────
    if len(q) <= SMALLTALK_MAX_LEN and _matches_any(ql, SMALLTALK_PATTERNS):
        return IntentDecision(
            agents=[],
            source="smalltalk",
            confidence=0.9,
            reason="приветствие/вежливость — агенты не нужны",
        )

    # ── 3. Продолжение диалога ──────────────────────────────
    last_agents = list(getattr(session, "last_agents", []) or [])
    if last_agents and _matches_any(ql, CONTINUATION_PATTERNS):
        return IntentDecision(
            agents=last_agents,
            source="continuation",
            confidence=0.8,
            reason=f"продолжение работы с агентами: {', '.join(last_agents)}",
        )

    # ── 4. Скоринг ключевых слов из routing_hints ───────────
    scored = _score_keywords(ql, cards)
    if scored:
        best_id, best_score, hits = scored[0]
        return IntentDecision(
            agents=[best_id],
            source="keywords",
            confidence=min(0.85, 0.55 + 0.1 * hits),
            reason=f"ключевые слова ({hits}) указывают на агента {best_id}",
        )

    # ── 5. Не уверен — зовём LLM-роутер ─────────────────────
    return IntentDecision(
        agents=[],
        source="low_confidence",
        confidence=0.0,
        reason="явных сигналов нет — нужен LLM-роутинг",
    )


# ═════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════
def _known(name: str, cards: list[dict]) -> bool:
    return any(c["id"] == name for c in cards)


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(p in text for p in patterns)


def _score_keywords(
    query: str, cards: list[dict],
) -> list[tuple[str, int, int]]:
    """Скоринг агентов по ключевым словам.

    Возвращает отсортированный [(id, score, hits)]; пусто, если ничего
    не набрало положительного балла. Положительное совпадение — подстрока
    запроса в ключе или ключа в запросе (для устойчивости к падежам
    обрезаем русские окончания только у длинных слов запроса).
    """
    results: list[tuple[str, int, int]] = []
    for card in cards:
        # negative_keywords = вето (семантика routing_hints:
        # «НЕ использовать, если упомянуто»)
        if any(nk and _hit(query, nk) for nk in card["negative_keywords"]):
            continue
        score = 0
        hits = 0
        for kw in card["keywords"]:
            if not kw:
                continue
            if _hit(query, kw):
                hits += 1
                score += 2 + (1 if len(kw) >= 8 else 0)
        if hits > 0 and score > 0:
            results.append((card["id"], score, hits))
    results.sort(key=lambda x: (-x[1], x[0]))
    return results


def _hit(text: str, keyword: str) -> bool:
    """Совпадение ключа с запросом: подстрока в обе стороны."""
    if keyword in text or text in keyword:
        return True
    # грубая устойчивость к русским окончаниям: ключ >= 5 символов,
    # сравниваем первые len-1..-2 символа
    if len(keyword) >= 5:
        stem = keyword[: len(keyword) - 1]
        if stem in text:
            return True
        stem = keyword[: len(keyword) - 2]
        if len(stem) >= 4 and stem in text:
            return True
    return False

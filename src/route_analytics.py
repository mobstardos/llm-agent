"""Route-аналитика — «обучение на своих данных» (Этап 5, V2 §3.10).

Что делает:
1. log_decision()  — журнал решений роутинга: КОГО выбрала система
   (Intent Layer / LLM-роутер / планировщик Supervisor) и почему.
2. log_outcome()   — журнал итога: какие агенты реально выполнялись,
   успех шагов, число re-plan.
3. report()        — агрегат: какой агент был нужен «на самом деле»,
   кто не выбирается никогда, какие ключевые слова routing_hints не
   срабатывают, где Intent Layer часто пасует (low_confidence).
4. suggestions()   — конкретные подсказки тюнинга routing_hints
   в agents/<id>/agent.yaml.

Хранилище: data/routing/decisions.jsonl (append-only, JSONL, stdlib).
События публикуются в шину (src/events, Этап 4): route.decision /
route.outcome — подписчики UI/MCP могут реагировать вживую.

Гигиена импортов: модуль stdlib-only, события — мягкий импорт;
при выключенной аналитике (env ROUTE_ANALYTICS=0) хуки — no-op.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

analytics_enabled = os.getenv("ROUTE_ANALYTICS", "1") != "0"

_DIR = Path("data") / "routing"
_FILE = _DIR / "decisions.jsonl"
_MAX_BYTES = 10 * 1024 * 1024   # ротация файла решений
_KEEP = 3


# ═════════════════════════════════════════════════════════════
# Запись
# ═════════════════════════════════════════════════════════════
def _record(entry: dict) -> None:
    """Аппендит JSONL-строку; никогда не бросает (аналитика ≠ работа)."""
    if not analytics_enabled:
        return
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _rotate_if_needed()
        entry = {"ts": time.time(), **entry}
        with _FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False,
                               default=str) + "\n")
    except Exception:
        pass
    try:
        from src import events   # Этап 4: шина (мягко)
        events.publish_soon(
            "route.decision" if entry.get("type") == "decision"
            else "route.outcome", entry)
    except Exception:
        pass


def _rotate_if_needed() -> None:
    try:
        if not _FILE.exists() or _FILE.stat().st_size < _MAX_BYTES:
            return
        stamp = time.strftime("%Y%m%d-%H%M%S")
        archived = _DIR / f"decisions-{stamp}.jsonl"
        _FILE.rename(archived)
        # держим последние _KEEP архивов
        archives = sorted(_DIR.glob("decisions-*.jsonl"))
        for old in archives[:-_KEEP]:
            old.unlink(missing_ok=True)
    except Exception:
        pass


def log_decision(
    *,
    query: str,
    source: str,
    agents: list[str] | None,
    reason: str = "",
    session_id: str = "",
    confidence: float = 0.0,
    suggested: list[str] | None = None,
    duration_ms: float = 0.0,
) -> None:
    """Реальный выбор агентов системой.

    source: intent.mention | intent.smalltalk | intent.continuation |
            intent.keywords | llm.route | supervisor.plan
    suggested — подсказка Intent Layer планировщику (для supervisor.plan).
    """
    _record({
        "type": "decision",
        "query": (query or "")[:300],
        "source": source,
        "agents": [str(a) for a in (agents or [])],
        "suggested": [str(a) for a in (suggested or [])],
        "reason": (reason or "")[:200],
        "session_id": session_id or "",
        "confidence": round(float(confidence or 0.0), 3),
        "duration_ms": round(float(duration_ms or 0.0), 1),
    })


def log_outcome(
    *,
    session_id: str = "",
    agents: list[dict] | None = None,
    success: bool = False,
    replans: int = 0,
    plan_id: str = "",
) -> None:
    """Итог выполнения: какие агенты работали и с каким статусом.

    agents: [{"agent": id, "success": bool, "error": str?}, ...]
    """
    _record({
        "type": "outcome",
        "session_id": session_id or "",
        "plan_id": plan_id or "",
        "success": bool(success),
        "replans": int(replans or 0),
        "agents": [
            {"agent": str(a.get("agent", "")),
             "success": bool(a.get("success")),
             **({"error": str(a.get("error"))[:120]}
                if a.get("error") else {})}
            for a in (agents or [])
        ],
    })


# ═════════════════════════════════════════════════════════════
# Чтение и агрегация
# ═════════════════════════════════════════════════════════════
def _iter_records(days: float = 30.0):
    if not _FILE.exists():
        return
    cutoff = time.time() - max(0.0, days) * 86400
    with _FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts", 0) < cutoff:
                continue
            yield rec


def report(days: float = 30.0) -> dict:
    """Агрегат по решениям и итогам за последние days дней."""
    decisions: list[dict] = []
    outcomes: list[dict] = []
    for rec in _iter_records(days):
        if rec.get("type") == "decision":
            decisions.append(rec)
        elif rec.get("type") == "outcome":
            outcomes.append(rec)

    by_source = Counter(d.get("source", "?") for d in decisions)
    picked: Counter = Counter()
    picked_by_source: dict[str, Counter] = defaultdict(Counter)
    for d in decisions:
        for a in d.get("agents", []):
            picked[a] += 1
            picked_by_source[a][d.get("source", "?")] += 1

    # Итоги: запуски и провалы по агентам
    runs: Counter = Counter()
    fails: Counter = Counter()
    fail_errors: dict[str, Counter] = defaultdict(Counter)
    for o in outcomes:
        for a in o.get("agents", []):
            aid = a.get("agent", "?")
            runs[aid] += 1
            if not a.get("success"):
                fails[aid] += 1
                if a.get("error"):
                    fail_errors[aid][a["error"][:80]] += 1

    # Сессии: решение → итог (по session_id, последняя пара)
    outcome_by_session = {o.get("session_id", ""): o for o in outcomes}
    decided_failed: Counter = Counter()   # выбран, но итог сессии — провал
    for d in decisions:
        sid = d.get("session_id", "")
        o = outcome_by_session.get(sid)
        if o and not o.get("success") and o.get("replans", 0) > 0:
            # первый агент исходного решения — главный подозреваемый
            if d.get("agents"):
                decided_failed[d["agents"][0]] += 1

    all_agents = _declared_agents()
    never_picked = sorted(set(all_agents) - set(picked))

    return {
        "days": days,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "decisions_total": len(decisions),
        "outcomes_total": len(outcomes),
        "success_rate": (
            round(sum(1 for o in outcomes if o.get("success"))
                  / len(outcomes), 2)
            if outcomes else None),
        "by_source": dict(by_source.most_common()),
        "picked": dict(picked.most_common()),
        "picked_by_source": {
            a: dict(c.most_common()) for a, c in picked_by_source.items()},
        "runs": dict(runs.most_common()),
        "fails": dict(fails.most_common()),
        "fail_errors": {
            a: dict(c.most_common(3)) for a, c in fail_errors.items()},
        "decided_failed": dict(decided_failed.most_common()),
        "never_picked": never_picked,
        "declared_agents": len(all_agents),
        "recent_empty_llm_routes": [
            {"query": d.get("query", ""), "reason": d.get("reason", "")}
            for d in decisions
            if d.get("source") == "llm.route" and not d.get("agents")
        ][-10:],
    }


def _declared_agents(agents_dir: str | Path = "agents") -> list[str]:
    """id агентов из деклараций agents/*/agent.yaml (мягко)."""
    try:
        import yaml
        out = []
        for mf in sorted(Path(agents_dir).glob("*/agent.yaml")):
            try:
                data = yaml.safe_load(
                    mf.read_text(encoding="utf-8")) or {}
                if data.get("id"):
                    out.append(str(data["id"]))
            except Exception:
                continue
        return out
    except Exception:
        return []


def _declared_keywords(
    agents_dir: str | Path = "agents",
) -> dict[str, list[str]]:
    """routing_hints.keywords из деклараций (для отчёта «что не бьёт»)."""
    try:
        import yaml
        out: dict[str, list[str]] = {}
        for mf in sorted(Path(agents_dir).glob("*/agent.yaml")):
            try:
                data = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
                aid = str(data.get("id") or mf.parent.name)
                hints = (data.get("routing_hints") or {})
                out[aid] = [str(k).lower()
                            for k in (hints.get("keywords") or [])]
            except Exception:
                continue
        return out
    except Exception:
        return {}


# ═════════════════════════════════════════════════════════════
# Подсказки тюнинга routing_hints
# ═════════════════════════════════════════════════════════════
def suggestions(rep: dict) -> list[str]:
    """Человеческие подсказки по данным отчёта."""
    out: list[str] = []
    by_source = rep.get("by_source", {})
    total = rep.get("decisions_total", 0)

    low = by_source.get("intent.low_confidence", 0)
    if total and low / total > 0.4:
        out.append(
            f"{low}/{total} решений ушло в LLM-роутинг (low_confidence "
            f"{round(100 * low / total)}%) — пополните routing_hints.keywords "
            "у профильных агентов типичными формулировками пользователей.")

    kw = by_source.get("intent.keywords", 0)
    if total and kw / total > 0.7:
        out.append(
            f"keywords покрывают {round(100 * kw / total)}% решений — "
            " Intent Layer работает, можно не усиливать промпт роутера.")

    for aid in rep.get("never_picked", []):
        kws = _declared_keywords().get(aid, [])
        out.append(
            f"Агент '{aid}' не выбирался ни разу за период"
            + (f" (keywords: {', '.join(kws[:5])}…)" if kws else "")
            + " — проверьте description_for_router/routing_hints "
              "или спросите себя, нужен ли он в реестре.")

    for aid, cnt in rep.get("fails", {}).items():
        runs = rep.get("runs", {}).get(aid, 0)
        if runs and cnt / runs >= 0.5 and runs >= 2:
            top_err = next(iter(rep.get("fail_errors", {}).get(aid, {})), "")
            out.append(
                f"Агент '{aid}' падает в {cnt}/{runs} запусков"
                + (f" — частая ошибка: {top_err}" if top_err else "")
                + " — чините агента, а не роутинг.")

    for item in rep.get("recent_empty_llm_routes", [])[:5]:
        out.append(
            f"LLM не выбрал агента для: «{item.get('query', '')}» "
            "— если запрос профильный, добавьте его формулировки "
            "в routing_hints.")

    if rep.get("success_rate") is not None and rep["success_rate"] < 0.7:
        out.append(
            f"Итоговый успех сессий {rep['success_rate']:.0%} — "
            "проверьте re-plan причины в supervisor-логах.")
    if not out:
        out.append("Заметных перекосов не найдено — routing_hints в порядке.")
    return out


def render_markdown(rep: dict) -> str:
    """Markdown-отчёт «какой агент был нужен на самом деле»."""
    lines: list[str] = []
    lines.append("# Route-отчёт")
    lines.append("")
    lines.append(f"*Сгенерирован: {rep.get('generated_at', '')}, "
                 f"период: {rep.get('days', 0)} дн.*")
    lines.append("")
    sr = rep.get("success_rate")
    lines.append(f"- Решений роутинга: **{rep.get('decisions_total', 0)}**")
    lines.append(f"- Итогов сессий: **{rep.get('outcomes_total', 0)}**"
                 + (f" (успех **{sr:.0%}**)" if sr is not None else ""))
    lines.append(f"- Агентов в реестре: {rep.get('declared_agents', 0)}, "
                 f"не выбирался: {len(rep.get('never_picked', []))}")
    lines.append("")
    lines.append("## Откуда берутся решения")
    lines.append("")
    lines.append("| Источник | Решений |")
    lines.append("|---|---|")
    for src, cnt in rep.get("by_source", {}).items():
        lines.append(f"| `{src}` | {cnt} |")
    lines.append("")
    lines.append("## Кого выбирают (решения → итоги)")
    lines.append("")
    lines.append("| Агент | Выбран | из них intent | из них LLM/план | "
                 "Запусков | Падений |")
    lines.append("|---|---|---|---|---|---|")
    pbs = rep.get("picked_by_source", {})
    for aid, cnt in rep.get("picked", {}).items():
        srcs = pbs.get(aid, {})
        intent_n = sum(v for k, v in srcs.items()
                       if k.startswith("intent."))
        llm_n = cnt - intent_n
        runs = rep.get("runs", {}).get(aid, 0)
        fails = rep.get("fails", {}).get(aid, 0)
        lines.append(f"| `{aid}` | {cnt} | {intent_n} | {llm_n} | "
                     f"{runs} | {fails} |")
    lines.append("")
    if rep.get("fail_errors"):
        lines.append("## Частые ошибки выбранных агентов")
        lines.append("")
        for aid, errs in rep["fail_errors"].items():
            for err, cnt in errs.items():
                lines.append(f"- `{aid}`: {err} ×{cnt}")
        lines.append("")
    empty = rep.get("recent_empty_llm_routes", [])
    if empty:
        lines.append("## Запросы, где LLM не выбрал никого")
        lines.append("")
        for it in empty:
            lines.append(f"- «{it.get('query', '')}» "
                         f"— {it.get('reason', '')}")
        lines.append("")
    lines.append("## Подсказки тюнинга routing_hints")
    lines.append("")
    for s in suggestions(rep):
        lines.append(f"- {s}")
    lines.append("")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════
# CLI: python -m src.route_analytics [--days N] [--out report.md]
# ═════════════════════════════════════════════════════════════
def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        description="Отчёт по решениям роутинга (Этап 5)")
    p.add_argument("--days", type=float, default=30.0)
    p.add_argument("--out", default="", help="записать markdown в файл")
    args = p.parse_args(argv)

    rep = report(days=args.days)
    md = render_markdown(rep)
    print(md)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md, encoding="utf-8")
        print(f"Отчёт записан: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

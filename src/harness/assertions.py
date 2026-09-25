"""Assertions: проверки результата сценария."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from src.core.schema import AssertionSpec
from src.loop.state import LoopResult

logger = logging.getLogger(__name__)


async def run_assertion(
    spec: AssertionSpec,
    project_root: Path,
    result: LoopResult,
    tool_calls: list[tuple[str, dict]],
    response_text: str,
) -> dict:
    """Возвращает {'type', 'passed', 'severity', 'message'}."""
    t = spec.type
    params = spec.params or {}
    severity = spec.severity

    try:
        passed, msg = await _dispatch(
            t, params, project_root, result, tool_calls, response_text,
        )
    except Exception as e:
        passed = False
        msg = f"Ошибка проверки: {e}"

    return {
        "type": t,
        "passed": passed,
        "severity": severity,
        "message": msg,
    }


async def _dispatch(
    t: str, p: dict, root: Path,
    result: LoopResult,
    tool_calls: list[tuple[str, dict]],
    response_text: str,
) -> tuple[bool, str]:
    # ─── File assertions ───────────────────────────
    if t == "file_exists":
        path = root / p["path"]
        return path.exists(), f"file_exists({p['path']})"

    if t == "file_not_exists":
        path = root / p["path"]
        return (not path.exists()), f"file_not_exists({p['path']})"

    if t == "file_contains":
        path = root / p["path"]
        if not path.exists():
            return False, f"file not found: {p['path']}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        return (p["pattern"] in text), f"file_contains({p['path']}, {p['pattern']})"

    if t == "file_matches_regex":
        path = root / p["path"]
        if not path.exists():
            return False, f"file not found: {p['path']}"
        text = path.read_text(encoding="utf-8", errors="ignore")
        return bool(re.search(p["pattern"], text)), f"regex({p['pattern']})"

    if t == "file_hash":
        import hashlib
        path = root / p["path"]
        if not path.exists():
            return False, f"file not found: {p['path']}"
        h = hashlib.sha256(path.read_bytes()).hexdigest()
        return h == p["hash"], f"hash mismatch"

    # ─── Tool call assertions ──────────────────────
    if t == "tool_called":
        target = p["tool"]
        args_contains = p.get("args_contains", {})
        for name, args in tool_calls:
            if name != target:
                continue
            if all(args.get(k) == v for k, v in args_contains.items()):
                return True, f"tool {target} called with {args_contains}"
        return False, f"tool {target} not called with {args_contains}"

    if t == "tool_not_called":
        target = p["tool"]
        for name, _ in tool_calls:
            if name == target:
                return False, f"tool {target} was called"
        return True, f"tool {target} not called"

    if t == "tool_call_count":
        target = p["tool"]
        count = sum(1 for n, _ in tool_calls if n == target)
        if "exact" in p:
            return count == p["exact"], f"count={count}, expected {p['exact']}"
        if "min" in p:
            return count >= p["min"], f"count={count}, min {p['min']}"
        if "max" in p:
            return count <= p["max"], f"count={count}, max {p['max']}"
        return True, f"count={count}"

    # ─── Response assertions ───────────────────────
    if t == "response_contains":
        return (p["pattern"] in (response_text or "")), \
            f"response_contains({p['pattern']})"

    if t == "response_matches_regex":
        return bool(re.search(p["pattern"], response_text or "")), \
            f"response_regex({p['pattern']})"

    # ─── No errors ─────────────────────────────────
    if t == "no_errors":
        return (result.exit_reason not in ("exception", "failure")), \
            f"exit_reason={result.exit_reason}"

    # ─── Budgets ───────────────────────────────────
    if t == "steps_less_than":
        v = p["value"]
        return result.iterations < v, f"steps={result.iterations}, < {v}"

    if t == "tokens_less_than":
        v = p["value"]
        total = result.tokens_input + result.tokens_output
        return total < v, f"tokens={total}, < {v}"

    if t == "duration_less_than_ms":
        v = p["value"]
        return result.duration_ms < v, f"duration={result.duration_ms:.0f}ms, < {v}"

    # ─── Unknown ───────────────────────────────────
    return False, f"неизвестный тип assertion: {t}"

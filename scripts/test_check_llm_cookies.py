#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 32: --check-llm знает куки-путь «браузер/куки» (in-process).

Сценарий пользователя: куки DeepSeek в .env, второстепенный API пропущен
(7936 мёртв), qwenproxy выключен → rc=0, без красного ✗, «НЕ блокер».
Контроль: без кук и с мёртвым 7936 → rc=2 c подсказкой перенастройки.
Запуск: python scripts/test_check_llm_cookies.py (из корня проекта).
"""
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import first_run  # noqa: E402 — проверяемый модуль

TMP = Path(tempfile.mkdtemp(prefix="llmagent-checkllm-"))
first_run.PROJECT_ROOT = TMP          # изолируем от реального .env проекта
first_run.ENV_FILE = TMP / ".env"
first_run.MARKER = TMP / "data" / ".initialized"

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    mark = "[✓]" if cond else "[✗]"
    print(f"  {mark} {name}" + (f" — {extra}" if extra and not cond else ""))
    passed += int(cond)
    failed += int(not cond)


def scenario(env_lines: list[str]) -> tuple[int, str]:
    first_run.ENV_FILE.write_text(
        "\n".join(env_lines) + "\n", encoding="utf-8")
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = first_run.check_llm_command(None, None, None)
    return rc, buf.getvalue()


# ── 1. Сценарий пользователя: куки есть, API пропущен, qwenproxy off ──
rc, out = scenario([
    "PROJECT_ROOT=" + str(TMP),
    "DEEPSEEK_DS_SESSION_ID=0123456789abcdef0123456789abcdef",
    "DEEPSEEK_SMIDV2=20250101120000abcdef0123456789abcdef0123456789",
    "DEEPSEEK_THUMBCACHE=Zm9vQmFyQmF6U3BhbUVnZ0VnZzJTcGFtQmF6U3BhbUVnZzJTcGFt%3D%3D",
    "LLM_BASE_URL=http://127.0.0.1:7936/v1",
    "LLM_MODEL=qwen3.8-max",
    "LLM_API_KEY=",
    "QWENPROXY_ENABLED=false",
    "QWENPROXY_AUTO_START=false",
])
check("rc=0 (куки-путь основной, 7936 не блокер)", rc == 0, f"rc={rc}")
check("строка «Основной путь — браузер/куки: DeepSeek (deepseek_web)»",
      "Основной путь — браузер/куки" in out and "deepseek_web" in out)
check("7936 показан как info (без красного ✗)", "✗" not in out)
check("подсказка «НЕ блокер» присутствует", "НЕ блокер" in out)
check("профиль упомянут", "127.0.0.1:7936/v1" in out)

# ── 2. Без кук и мёртвый 7936 → честный ✗ и rc=2 (как раньше) ──
rc2, out2 = scenario([
    "PROJECT_ROOT=" + str(TMP),
    "LLM_BASE_URL=http://127.0.0.1:7936/v1",
    "LLM_MODEL=qwen3.8-max",
    "LLM_API_KEY=",
    "QWENPROXY_ENABLED=false",
])
check("rc=2 без кук (перенастройка нужна)", rc2 == 2, f"rc={rc2}")
check("красный ✗ без кук остаётся", "✗" in out2)
check("подсказка --reconfigure-ai", "reconfigure-ai" in out2)

# ── 3. Qwen-куки тоже распознаются ──
rc3, out3 = scenario([
    "PROJECT_ROOT=" + str(TMP),
    "QWEN_WEB_COOKIES=token_abc=1; token=xyz",
    "LLM_BASE_URL=http://127.0.0.1:7936/v1",
    "LLM_MODEL=qwen3.8-max",
    "QWENPROXY_ENABLED=false",
])
check("rc=0 с куками Qwen", rc3 == 0, f"rc={rc3}")
check("строка «Qwen (qwen_web)»", "Qwen (qwen_web)" in out3)

print(f"\n═══ Итог: {passed}/{passed + failed} ═══")
sys.exit(1 if failed else 0)

#!/usr/bin/env bash
# Полный регресс LLM Agent. Запуск из любого места: bash scripts/run_regression.sh
# Каждый набор обязан завершиться rc=0 (наборы печатают свой «═══ Итог: N/N ═══»).
set -u
cd "$(dirname "$0")/.." || exit 1
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null 2>&1 || PY=python
LOG="${TMPDIR:-/tmp}/llmagent-regress-last.log"

SETS=(
  "scripts/test_onboarding.py"
  "scripts/test_check_llm_cookies.py"
  "scripts/test_run_port_busy.py"
  "scripts/test_chat_features_27.py"
  "scripts/test_web_chat_30.py"
  "scripts/test_bridge.py"
  "scripts/test_llm_errors.py"
  "scripts/test_history_feature.py"
  "scripts/test_orchestrator_handle.py"
  "scripts/test_stage1.py"
  "scripts/test_stage2.py"
  "scripts/test_stage3.py"
  "scripts/test_stage4.py"
  "scripts/test_stage5.py"
  "tests/test_models_micro.py"
  "tests/test_providers_24d.py"
  "tests/test_cookies_24a.py"
  "tests/test_pg_autodetect_24b.py"
  "tests/test_journal.py"
)

fail=0
for s in "${SETS[@]}"; do
  printf '── %s\n' "$s"
  if ! "$PY" "$s" >"$LOG" 2>&1; then
    echo "   ✗ ПРОВАЛ (полный лог: $LOG)"
    tail -25 "$LOG" | sed 's/^/   /'
    fail=$((fail+1))
  else
    tail -1 "$LOG" | sed 's/^/   /'
  fi
done

if [ "$fail" -eq 0 ]; then
  echo "═══ Регресс зелёный: ${#SETS[@]}/${#SETS[@]} наборов rc=0 ═══"
  exit 0
fi
echo "═══ Регресс: провалов наборов: $fail/${#SETS[@]} ═══"
exit 1

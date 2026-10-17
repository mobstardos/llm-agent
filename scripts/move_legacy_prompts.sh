#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# move_legacy_prompts.sh — перенос orphaned prompt-файлов в attic/
# Рекомендация 9 (Sprint 4, P2)
# ═══════════════════════════════════════════════════════════════════════
#
# Контекст:
#   В src/prompts/ лежат 37 файлов *_agent.py (FILE_AGENT_SYSTEM,
#   FILE_AGENT_USER_TEMPLATE и т. д.). Они НЕ используются продакшен-кодом —
#   только 5 из них импортируются из attic/agents-legacy/*.py (которые
#   сами уже в архиве). Реальные промпты для агентов лежат в
#   agents/<id>/prompt.md (декларативная система).
#
#   Этот скрипт:
#     1. Создаёт attic/prompts-legacy/ (если ещё нет)
#     2. Делает git mv src/prompts/*_agent.py → attic/prompts-legacy/
#     3. Обновляет imports в attic/agents-legacy/*.py
#     4. Обновляет импорты в attic/agents-legacy/__init__.py (если есть)
#
# Запуск:
#   cd /path/to/llm-agent
#   bash scripts/move_legacy_prompts.sh
#   # или dry-run:
#   bash scripts/move_legacy_prompts.sh --dry-run
#
# После: git diff --stat — увидеть, что изменилось. Закоммитить.
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true && echo "🔍 DRY RUN — изменения не применяются"

# Переходим в корень проекта (там, где README.md)
cd "$(dirname "$0")/.."
[[ ! -f README.md ]] && echo "❌ Не найден README.md — запускайте из корня llm-agent" && exit 1

PROMPTS_DIR="src/prompts"
LEGACY_DIR="attic/prompts-legacy"
AGENTS_LEGACY_DIR="attic/agents-legacy"

mkdir -p "$LEGACY_DIR"

# ─── 1. Найти все *_agent.py в src/prompts/ ─────────────────────────
mapfile -t FILES < <(find "$PROMPTS_DIR" -maxdepth 1 -name "*_agent.py" -type f | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "✓ Нет файлов *_agent.py в $PROMPTS_DIR — уже перенесены"
  exit 0
fi

echo "Найдено ${#FILES[@]} legacy-файлов для переноса:"
for f in "${FILES[@]}"; do basename "$f"; done
echo ""

# ─── 2. git mv в attic/prompts-legacy/ ─────────────────────────────
for src_file in "${FILES[@]}"; do
  fname=$(basename "$src_file")
  dest="$LEGACY_DIR/$fname"
  if $DRY_RUN; then
    echo "  [DRY] git mv $src_file → $dest"
  else
    git mv "$src_file" "$dest"
    echo "  ✓ git mv $src_file → $dest"
  fi
done

echo ""

# ─── 3. Обновить imports в attic/agents-legacy/*.py ────────────────
# 原来: from src.prompts.file_agent import FILE_AGENT_SYSTEM, ...
# Стало: from attic.prompts-legacy.file_agent import FILE_AGENT_SYSTEM, ...
# (через __init__.py для attic/prompts-legacy/)

# Создать __init__.py для нового каталога
if ! $DRY_RUN; then
  cat > "$LEGACY_DIR/__init__.py" << 'PYEOF'
"""Legacy prompt templates (moved from src/prompts/ in Sprint 4).

These *_agent.py files contain SYSTEM prompt templates that were used
in the pre-declarative (pre-YAML) architecture. They are kept here
for reference; production agents use agents/<id>/prompt.md instead.

DO NOT import these in production code.
"""
PYEOF
  echo "✓ Создан $LEGACY_DIR/__init__.py"
fi

# Проверить и обновить импорты в attic/agents-legacy/*.py
if [[ -d "$AGENTS_LEGACY_DIR" ]]; then
  echo ""
  echo "Обновление imports в $AGENTS_LEGACY_DIR/:"
  for agent_file in "$AGENTS_LEGACY_DIR"/*.py; do
    [[ -f "$agent_file" ]] || continue
    fname=$(basename "$agent_file")
    if grep -q "from src.prompts." "$agent_file"; then
      if $DRY_RUN; then
        echo "  [DRY] sed -i 's|from src.prompts.|from attic.prompts-legacy.|g' $fname"
      else
        # Заменить путь импорта
        sed -i 's|from src\.prompts\.|from attic.prompts-legacy.|g' "$agent_file"
        echo "  ✓ $fname: import path updated"
      fi
    fi
  done
fi

echo ""
echo "═══════════════════════════════════════════════════════════════════"
echo "✅ Перенос завершён. Проверьте изменения:"
echo "   git diff --stat"
echo ""
echo "Перед коммитом прогоните smoke-тест:"
echo "   python -m src.main  # или python run.py"
echo "   # Сервер должен стартовать, агенты должны работать"
echo ""
echo "Если всё ОК:"
echo "   git add -A && git commit -m 'refactor: move 37 orphan prompt files to attic/prompts-legacy/'"
echo ""
echo "⚠  После этого запустите pytest:"
echo "   pytest tests/ -v"
echo "   # Тестов на prompts/*_agent нет, но проверьте что-то не упало"
